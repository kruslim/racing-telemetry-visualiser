"""Session lifecycle: where a practice, qualifying or race session is, from telemetry.

The engine feeds :class:`SessionLifecycle` every frame from channels it already
reads (SessionState, the flag phase, the player's lap) plus ``SessionNum``, and
the session type from the session-info YAML (``Sessions[SessionNum].SessionType``).
It is a pure state machine with no clock and no LLM: the same frames give the
same transitions, so a replay produces the same lifecycle events twice.

States::

    idle -> practice | qualify | race | unknown     segment opened (session_start)
         -> pre_green                               SessionState before racing
         -> green                                   racing + flag phase green (session_green)
         -> checkered                               SessionState checkered
         -> cool_down                               SessionState cool_down
         -> ended                                   segment closed (session_end)

``session_checkered`` fires when the *player* takes the flag: the first lap
crossing after SessionState went checkered (the leader's flag), or at cool_down
if the player never crossed. Green uses the engine's flag-phase logic, which
treats ``start_hidden`` after the formation lap as green, because the real Spa
``.ibt`` never shows a plain green bit after the start. A transition the catalog
cannot back (no SessionState, no SessionFlags) leaves the state ``unknown``; the
engine's ``lifecycle`` capability says which channel is missing.

A *segment* is one session of an event: a SessionNum change, a new RTV session
id or the session clock going backwards starts the next one. The engine resets
its accumulators at each boundary; :mod:`rtv.services` cuts the journal run and
resets the agent layer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from rtv.racestate.models import EventType, FlagPhase, LifecycleSummary


class LifecycleState(StrEnum):
    IDLE = "idle"
    PRACTICE = "practice"
    QUALIFY = "qualify"
    RACE = "race"
    PRE_GREEN = "pre_green"
    GREEN = "green"
    CHECKERED = "checkered"
    COOL_DOWN = "cool_down"
    ENDED = "ended"
    UNKNOWN = "unknown"


#: Normalised session types. iRacing's own strings vary ("Open Qualify",
#: "Lone Qualify", "Offline Testing", "Warmup"); everything maps onto these.
SESSION_TYPES = ("practice", "qualify", "race")

#: iRacing SessionState labels (``rtv.catalog.enums.SESSION_STATE``) before the start.
_PRE_GREEN = {"invalid", "get_in_car", "warmup", "parade_laps"}
#: A backwards step in SessionTime larger than this is a new session clock.
CLOCK_RESET_S = 0.5


def normalise_session_type(raw: Any) -> str | None:
    """``"Open Qualify"`` -> ``"qualify"``; None for anything unrecognised."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().lower()
    if "race" in text:
        return "race"
    if "qual" in text:
        return "qualify"
    if "practice" in text or "warmup" in text or "testing" in text:
        return "practice"
    return None


def session_type_from_info(
    info: Mapping[str, Any] | None, session_num: int | None
) -> tuple[str | None, str | None, str | None]:
    """``(type, raw_type, note)`` for ``Sessions[session_num]`` of a session-info doc.

    ``note`` says why the type is unknown, when it is.
    """
    if session_num is None:
        return None, None, "SessionNum is not in this catalog, so the session type is unknown."
    sessions = ((info or {}).get("SessionInfo") or {}).get("Sessions") or []
    if not sessions:
        return None, None, "No session-info document lists the sessions, so the type is unknown."
    for entry in sessions:
        try:
            num = int(entry.get("SessionNum"))
        except (TypeError, ValueError, AttributeError):
            continue
        if num == session_num:
            raw = entry.get("SessionType")
            kind = normalise_session_type(raw)
            note = None if kind else f"Unrecognised SessionType {raw!r}."
            return kind, (str(raw) if raw is not None else None), note
    return None, None, f"Session {session_num} is not in the session-info document."


@dataclass
class Signal:
    """A lifecycle transition the engine turns into a :class:`RaceEvent`."""

    event_type: EventType
    payload: dict[str, Any] = field(default_factory=dict)


class SessionLifecycle:
    """The per-engine lifecycle state machine (not thread-safe; the engine locks)."""

    def __init__(self) -> None:
        self.state = LifecycleState.IDLE
        self.segment = 0
        self.open = False
        self.session_num: int | None = None
        self.session_type: str | None = None
        self.session_type_raw: str | None = None
        self.session_state: str | None = None
        self.green_at: float | None = None
        self.checkered_at: float | None = None
        self.player_finished: bool | None = None
        self.ended_reason: str | None = None
        self.notes: list[str] = []
        self._checkered_lap: int | None = None
        self._last_time: float | None = None
        self._can_green = True
        self._has_state = True

    # ---- segments --------------------------------------------------------
    def boundary(self, session_num: int | None, session_time: float) -> str | None:
        """Why this frame starts a new segment, or None.

        ``"start"`` when no segment is open (the first frame, or after
        :meth:`end`); otherwise a SessionNum change or the clock going back.
        """
        if not self.open:
            return "start"
        if (
            session_num is not None
            and self.session_num is not None
            and session_num != self.session_num
        ):
            return "session_num_changed"
        if self._last_time is not None and session_time < self._last_time - CLOCK_RESET_S:
            return "clock_reset"
        return None

    def begin(
        self,
        session_num: int | None,
        info: Mapping[str, Any] | None,
        *,
        has_state: bool,
        has_flags: bool,
        reason: str = "start",
    ) -> list[Signal]:
        previous = self.state
        self.open = True
        self.segment += 1
        self.session_num = session_num
        kind, raw, note = session_type_from_info(info, session_num)
        self.session_type, self.session_type_raw = kind, raw
        self.notes = [note] if note else []
        if not has_state:
            self.notes.append("SessionState is not in this catalog: the lifecycle is unknown.")
        elif not has_flags:
            self.notes.append("SessionFlags is not in this catalog: green cannot be detected.")
        self._has_state = has_state
        self._can_green = has_flags
        self.session_state = None
        self.green_at = self.checkered_at = None
        self.player_finished = None
        self.ended_reason = None
        self._checkered_lap = None
        self._last_time = None
        self.state = LifecycleState(kind) if kind else LifecycleState.UNKNOWN
        return [self._signal(EventType.SESSION_START, previous, self.state, reason=reason)]

    def end(self, reason: str) -> list[Signal]:
        """Close the open segment. Idempotent: nothing to close returns []."""
        if not self.open:
            return []
        previous = self.state
        self.open = False
        self.state = LifecycleState.ENDED
        self.ended_reason = reason
        return [
            self._signal(
                EventType.SESSION_END,
                previous,
                self.state,
                reason=reason,
                player_finished=self.player_finished,
            )
        ]

    # ---- per frame -------------------------------------------------------
    def observe(
        self,
        *,
        session_state: str | None,
        flag_phase: FlagPhase,
        lap: int | None,
        session_time: float,
    ) -> list[Signal]:
        if not self.open:
            return []
        self._last_time = session_time
        out: list[Signal] = []
        if session_state is not None and session_state != self.session_state:
            previous_label, self.session_state = self.session_state, session_state
            out.append(
                Signal(
                    EventType.SESSION_STATE_CHANGE,
                    {**self._ident(), "from": previous_label, "to": session_state},
                )
            )
        target = self._target(flag_phase)
        if target is not None and target is not self.state:
            previous = self.state
            self.state = target
            if target is LifecycleState.GREEN:
                self.green_at = session_time
                out.append(self._signal(EventType.SESSION_GREEN, previous, target))
            elif target is LifecycleState.CHECKERED:
                self.checkered_at = session_time
                self._checkered_lap = lap
                self.player_finished = False if lap is not None else None
                if lap is None:
                    # No lap counter: the session's flag is the only flag there is.
                    out.append(self._checkered(previous))
            elif target is LifecycleState.COOL_DOWN and self.player_finished is False:
                out.append(self._checkered(previous))
        # The player takes the flag on the first crossing after the leader did.
        if (
            self.state is LifecycleState.CHECKERED
            and self.player_finished is False
            and lap is not None
            and self._checkered_lap is not None
            and lap > self._checkered_lap
        ):
            self.player_finished = True
            out.append(self._checkered(self.state))
        return out

    def _target(self, phase: FlagPhase) -> LifecycleState | None:
        if not self._has_state:
            return LifecycleState.UNKNOWN
        label = self.session_state
        if label is None:
            return None
        order = _ORDER.get(self.state, 0)
        if label == "cool_down":
            return LifecycleState.COOL_DOWN
        if label == "checkered":
            return LifecycleState.CHECKERED if order < _ORDER[LifecycleState.COOL_DOWN] else None
        if order >= _ORDER[LifecycleState.GREEN]:
            return None  # never step back from green within a segment
        if label == "racing":
            if not self._can_green:
                return LifecycleState.UNKNOWN
            if phase is FlagPhase.GREEN:
                return LifecycleState.GREEN
            return LifecycleState.PRE_GREEN
        if label in _PRE_GREEN:
            return LifecycleState.PRE_GREEN
        return LifecycleState.UNKNOWN

    # ---- output ----------------------------------------------------------
    def _ident(self) -> dict[str, Any]:
        return {
            "session_num": self.session_num,
            "session_type": self.session_type,
            "segment": self.segment,
        }

    def _signal(
        self, kind: EventType, previous: LifecycleState, to: LifecycleState, **extra: Any
    ) -> Signal:
        return Signal(kind, {**self._ident(), "from": previous.value, "to": to.value, **extra})

    def _checkered(self, previous: LifecycleState) -> Signal:
        return self._signal(
            EventType.SESSION_CHECKERED,
            previous,
            self.state,
            player_finished=self.player_finished,
            checkered_at=self.checkered_at,
        )

    def summary(self) -> LifecycleSummary:
        return LifecycleSummary(
            state=self.state.value,
            segment=self.segment,
            session_num=self.session_num,
            session_type=self.session_type,
            session_type_raw=self.session_type_raw,
            session_state=self.session_state,
            green_at=self.green_at,
            checkered_at=self.checkered_at,
            player_finished=self.player_finished,
            ended_reason=self.ended_reason,
            notes=list(self.notes),
        )


_ORDER = {
    LifecycleState.IDLE: 0,
    LifecycleState.UNKNOWN: 0,
    LifecycleState.PRACTICE: 1,
    LifecycleState.QUALIFY: 1,
    LifecycleState.RACE: 1,
    LifecycleState.PRE_GREEN: 2,
    LifecycleState.GREEN: 3,
    LifecycleState.CHECKERED: 4,
    LifecycleState.COOL_DOWN: 5,
    LifecycleState.ENDED: 6,
}
