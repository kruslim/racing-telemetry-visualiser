"""The vision contracts: videos, the time-sync map, moments and visual notes.

Every timestamp the rest of the app uses is ``session_time`` (seconds, from the
telemetry). A video has its own clock, ``video_time`` (seconds from the first
frame). :class:`SyncMap` is the only place the two meet.

A :class:`VisualNote` is what the VLM is allowed to contribute: a few short
observations in words, each with a kind and a confidence, pinned to a
:class:`Moment` that a deterministic source chose. It carries no numeric field
the model filled in; the numbers on it (times, lap, corner distance) all come from
the telemetry side.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class SyncAnchor(BaseModel):
    """One point where the two clocks are known to agree."""

    video_time: float = Field(ge=0.0, description="Seconds from the video's first frame.")
    session_time: float = Field(description="Telemetry session time at that frame, s.")
    #: How the anchor was set: ``offset``, ``manual`` or ``lap_start:<lap>``.
    source: str = "manual"


class SyncMap(BaseModel):
    """``session_time = video_time * rate + offset``.

    One anchor fixes the offset with ``rate = 1``. Two or more are fitted by least
    squares, which absorbs a capture whose clock drifts against the sim's (an
    encoder dropping frames, a 59.94 fps recording of a 60 Hz sim).
    """

    anchors: list[SyncAnchor] = Field(default_factory=list)
    rate: float = 1.0
    offset: float = 0.0

    @model_validator(mode="after")
    def _fit(self) -> SyncMap:
        if not self.anchors:
            return self
        if len(self.anchors) == 1:
            a = self.anchors[0]
            self.rate, self.offset = 1.0, a.session_time - a.video_time
            return self
        xs = [a.video_time for a in self.anchors]
        ys = [a.session_time for a in self.anchors]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx <= 1e-9:
            raise ValueError("Sync anchors need distinct video times.")
        rate = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
        # A real capture drifts by well under 1 %; anything wilder is a typo in an
        # anchor, and fitting it would silently put every frame in the wrong corner.
        if not 0.9 <= rate <= 1.1:
            raise ValueError(
                f"Sync anchors imply the video runs at {rate:.3f}x the sim clock; "
                "check the anchor times."
            )
        self.rate, self.offset = rate, my - rate * mx
        return self

    @property
    def synced(self) -> bool:
        return bool(self.anchors)

    def to_session(self, video_time: float) -> float:
        return video_time * self.rate + self.offset

    def to_video(self, session_time: float) -> float:
        return (session_time - self.offset) / self.rate

    def residuals(self) -> list[float]:
        """Per-anchor error of the fit, s (all zero for one or two anchors)."""
        return [round(self.to_session(a.video_time) - a.session_time, 4) for a in self.anchors]


class VideoInfo(BaseModel):
    """A registered onboard video and how it lines up with the session."""

    video_id: str
    session_id: str
    path: str
    label: str | None = None
    #: True when the file was uploaded into the data dir (and so is ours to delete).
    uploaded: bool = False
    duration_s: float | None = None
    fps: float | None = None
    width: int | None = None
    height: int | None = None
    codec: str | None = None
    bytes: int | None = None
    registered_wall_ms: int | None = None
    sync: SyncMap = Field(default_factory=SyncMap)

    def session_span(self) -> tuple[float, float] | None:
        """The session-time interval the video covers, if it is synced and probed."""
        if not self.sync.synced or self.duration_s is None:
            return None
        return self.sync.to_session(0.0), self.sync.to_session(self.duration_s)

    def to_api(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        span = self.session_span()
        data["sync"]["synced"] = self.sync.synced
        data["sync"]["residuals"] = self.sync.residuals()
        data["session_time_start"] = span[0] if span else None
        data["session_time_end"] = span[1] if span else None
        return data


class TriggerKind(StrEnum):
    """What made a moment worth looking at. All deterministic."""

    INCIDENT = "incident"
    OFFTRACK = "offtrack"
    LOCKUP = "lockup"
    WHEELSPIN = "wheelspin"
    TRAFFIC_CLOSE = "traffic_close"
    RECURRING_ISSUE = "recurring_issue"
    #: A Layer-1 corner finding: a top-priority time loss on a lap.
    FINDING = "finding"


#: Lower is looked at first when the frame budget is short.
TRIGGER_RANK: dict[TriggerKind, int] = {
    TriggerKind.INCIDENT: 0,
    TriggerKind.OFFTRACK: 1,
    TriggerKind.LOCKUP: 2,
    TriggerKind.RECURRING_ISSUE: 3,
    TriggerKind.FINDING: 4,
    TriggerKind.TRAFFIC_CLOSE: 5,
    TriggerKind.WHEELSPIN: 6,
}


class TriggerRef(BaseModel):
    """The deterministic source of a moment, enough to find it again."""

    kind: TriggerKind
    session_time: float
    lap: int | None = None
    #: Journal ``seq`` of the race event, for an event trigger.
    journal_seq: int | None = None
    journal_run: str | None = None
    #: Layer-1 corner label (``T7``) and the laps compared, for a finding trigger.
    corner: str | None = None
    ref_lap: int | None = None
    #: Seconds the Layer-1 finding apportions to this corner (telemetry-side).
    gain_s: float | None = None
    #: Number-free summary the VLM is shown ("off track", "time lost at T7").
    label: str = ""
    #: The event payload or finding summary, kept for audit; never sent to a model.
    detail: dict[str, Any] = Field(default_factory=dict)


class Moment(BaseModel):
    """A window of session time to look at, and why."""

    moment_id: str
    session_time: float
    t_start: float
    t_end: float
    lap: int | None = None
    corner: str | None = None
    lap_dist_pct: float | None = None
    triggers: list[TriggerRef]
    #: Deterministic facts the grounding step checks observations against
    #: (``kerb_signal``: True/False/None from vertical acceleration).
    facts: dict[str, Any] = Field(default_factory=dict)

    @property
    def primary(self) -> TriggerRef:
        return min(self.triggers, key=lambda t: TRIGGER_RANK[t.kind])


ObservationKind = Literal[
    "kerb_use", "line", "traffic", "car_behaviour", "damage", "track_state", "other"
]
Confidence = Literal["low", "medium", "high"]


class VisionObservationOut(BaseModel):
    """One observation as the model must return it. Words only."""

    kind: ObservationKind = Field(
        description="kerb_use: kerbs touched or avoided. line: the car's path against "
        "the track edges, apex and exit. traffic: other cars and what they did. "
        "car_behaviour: oversteer, understeer, a slide, tyre smoke. damage: visible "
        "damage to this car. track_state: debris, dust, marbles, wet patches. "
        "other: anything else the driver should know."
    )
    text: str = Field(
        max_length=240,
        description="One short sentence about what the frames show. No numbers, no "
        "distances, no speeds, no times.",
    )
    confidence: Confidence = Field(
        description="high only when the frames show it plainly; low when inferred."
    )


class VisionAnswer(BaseModel):
    """The structured answer of one VLM call (the forced respond tool's input)."""

    trigger_visible: Literal["yes", "no", "unclear"] = Field(
        description="Do the frames show the moment the telemetry flagged? 'no' when the "
        "frames plainly show something else (wrong part of the lap, the pits, a menu)."
    )
    observations: list[VisionObservationOut] = Field(
        default_factory=list,
        max_length=5,
        description="At most five observations, most useful first. Empty is fine.",
    )


ObservationStatus = Literal["corroborated", "uncheckable", "contradicted", "dropped"]


class Observation(BaseModel):
    """An observation after grounding."""

    kind: ObservationKind
    text: str
    confidence: Confidence
    status: ObservationStatus
    #: Why it was corroborated, contradicted or dropped.
    reason: str | None = None

    @property
    def kept(self) -> bool:
        return self.status in ("corroborated", "uncheckable")


class FrameRef(BaseModel):
    name: str
    video_time: float
    session_time: float


class VisualNote(BaseModel):
    """One analysed moment: what the VLM saw, after grounding."""

    note_id: str
    analysis_id: str
    video_id: str
    moment: Moment
    frames: list[FrameRef] = Field(default_factory=list)
    #: ``ok``, ``trigger_not_seen`` (frames did not show the moment: check the
    #: sync), ``no_answer`` (invalid or missing reply) or ``error``.
    status: str = "ok"
    trigger_visible: str | None = None
    observations: list[Observation] = Field(default_factory=list)
    model: str | None = None
    latency_ms: int | None = None
    usage: dict[str, Any] | None = None
    error: str | None = None

    @property
    def kept(self) -> list[Observation]:
        return [o for o in self.observations if o.kept]

    def to_api(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["kept"] = [o.model_dump(mode="json") for o in self.kept]
        return data
