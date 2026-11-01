"""Race-control detectors: compliance and consequence, computed before any agent wakes.

Stage 09 part A. Four things a steward would see about the player, each aggregated
here so the race-control agent is woken once per consequence, not once per frame:

=====================  =====================================  ==========================
event                  fires when                             backed by
=====================  =====================================  ==========================
``incident_limit``     the incident count crosses a stage     PlayerCarMyIncidentCount +
                       against the session's limit            ``WeekendOptions.IncidentLimit``
``track_limits``       ``track_limit_offs`` off-tracks inside  the offtrack detector
                       ``track_limit_window_laps`` laps        (PlayerTrackSurface)
``penalty``            a driver flag on the player is shown   SessionFlags black, furled,
                       or cleared                             repair, disqualify bits
``blue_flag_ignored``  a car lapping us stayed within the     CarIdx standings (never on
                       blue-flag gap for N corners            a replayed ``.ibt``)
=====================  =====================================  ==========================

No channel is invented. iRacing publishes no track-limit warning counter and no
penalty-served counter, so neither is modelled: ``track_limits`` counts offs (each
is a 1x in iRacing), and a penalty is the flag bit, shown or cleared. A detector the
catalog or the session-info document cannot back has its capability flag False and
a ``missing`` entry that says why (:attr:`RaceControlState.missing`).

Stateful, like :class:`~rtv.racestate.detectors.CornerRecurrence`: it returns
payloads and never constructs an event; the engine does that. Its thresholds are
:class:`~rtv.racestate.detectors.DetectorConfig` fields (``incident_*``,
``track_limit_*``, ``blue_flag_corners``, ``corner_buckets``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from rtv.racestate.detectors import DEFAULT_CONFIG, DetectorConfig
from rtv.racestate.models import RaceControlState, TrackOff

#: SessionFlags bits that are a driver flag on the player car.
PENALTY_FLAGS: tuple[str, ...] = ("black", "furled", "repair", "disqualify")
#: Flags that are served (or cleared) in the pit lane: a drive-through or stop-and-go
#: for the black flag, a repair stop for the meatball.
SERVE_IN_PIT = frozenset({"black", "repair"})

#: The session-info key the limit comes from.
INCIDENT_LIMIT_KEY = "WeekendInfo.WeekendOptions.IncidentLimit"

#: Incident stages, least to most urgent.
STAGES: tuple[str, ...] = ("approaching", "final", "reached")




def incident_limit_from(info: Mapping[str, Any] | None) -> tuple[int | None, str]:
    """The session's incident limit and where it came from.

    ``(17, "session_info")``, ``(None, "unlimited")``, ``(None, "absent")`` (no
    document or no key) or ``(None, "unreadable")`` (a value that is not a count).
    """
    if not info:
        return None, "absent"
    options = (info.get("WeekendInfo") or {}).get("WeekendOptions") or {}
    if "IncidentLimit" not in options:
        return None, "absent"
    raw = options.get("IncidentLimit")
    if isinstance(raw, bool):
        return None, "unreadable"
    if isinstance(raw, int | float):
        value = int(raw)
        return (value, "session_info") if value > 0 else (None, "unlimited")
    text = str(raw).strip().lower()
    if text in ("", "unlimited", "none", "off", "0"):
        return None, "unlimited"
    try:
        value = int(float(text.split()[0]))
    except (ValueError, IndexError):
        return None, "unreadable"
    return (value, "session_info") if value > 0 else (None, "unlimited")


def stage_for(remaining: int | None, cfg: DetectorConfig = DEFAULT_CONFIG) -> str | None:
    """The incident stage for ``remaining`` incidents left, or ``None`` when clear."""
    if remaining is None:
        return None
    if remaining <= 0:
        return "reached"
    if remaining <= cfg.incident_final_margin:
        return "final"
    if remaining <= cfg.incident_approach_margin:
        return "approaching"
    return None


def penalty_flags(active: Sequence[str]) -> list[str]:
    """The driver flags in a decoded SessionFlags list, in a fixed order."""
    on = set(active)
    return [name for name in PENALTY_FLAGS if name in on]


class RaceControlMonitor:
    """Per-segment race-control aggregates. The engine feeds it; it returns payloads."""

    def __init__(self, cfg: DetectorConfig = DEFAULT_CONFIG) -> None:
        self.cfg = cfg
        self.limit: int | None = None
        self.limit_source = "absent"
        self.reset()

    def reset(self) -> None:
        """Forget the segment: counts, latches and the blue-flag episode."""
        self.incidents: int | None = None
        self.stage: str | None = None
        self.offs: list[TrackOff] = []
        self._offs_latched = False
        self.penalty: list[str] = []
        self.penalty_since: float | None = None
        self._penalty_seen = False
        self._blue: dict[str, Any] | None = None
        self._blue_reported: set[int] = set()

    # ---- incidents -----------------------------------------------------
    def set_limit(self, limit: int | None, source: str) -> None:
        self.limit, self.limit_source = limit, source

    def remaining(self) -> int | None:
        if self.limit is None or self.incidents is None:
            return None
        # Zero once reached: "minus one to the limit" is not a sentence anyone says.
        return max(0, self.limit - self.incidents)

    def observe_incidents(self, count: int | None) -> dict | None:
        """Track the counter; a payload when it crosses into a new, worse stage.

        Only the worst stage crossed is reported, so a 4x that jumps two stages wakes
        the agent once. Stages never go back down inside a segment (incidents only rise).
        """
        if count is None:
            return None
        previous = self.incidents
        self.incidents = count
        stage = stage_for(self.remaining(), self.cfg)
        if stage is None:
            return None
        if self.stage is not None and STAGES.index(stage) <= STAGES.index(self.stage):
            return None
        self.stage = stage
        return {
            "stage": stage,
            "incidents": count,
            "incident_limit": self.limit,
            "incidents_remaining": self.remaining(),
            "delta": count - previous if previous is not None else None,
            "limit_source": self.limit_source,
            "final_margin": self.cfg.incident_final_margin,
        }

    # ---- track limits --------------------------------------------------
    def _window(self, lap: int | None) -> list[TrackOff]:
        if lap is None:
            return list(self.offs)
        first = lap - self.cfg.track_limit_window_laps + 1
        return [o for o in self.offs if o.lap is None or o.lap >= first]

    def observe_off(
        self, *, lap: int | None, session_time: float, lap_dist_pct: float | None
    ) -> dict | None:
        """Count one off-track; a payload when the window reaches the threshold.

        Latched per episode: it re-arms once the window drops below the threshold
        again, so a fourth off in the same three laps does not wake anyone twice.
        """
        self.offs.append(TrackOff(lap=lap, session_time=session_time, lap_dist_pct=lap_dist_pct))
        self.offs = self._window(lap)[-32:]
        count = len(self.offs)
        if count < self.cfg.track_limit_offs:
            self._offs_latched = False
            return None
        if self._offs_latched:
            return None
        self._offs_latched = True
        laps = sorted({o.lap for o in self.offs if o.lap is not None})
        return {
            "offs": count,
            "window_laps": self.cfg.track_limit_window_laps,
            "threshold": self.cfg.track_limit_offs,
            "laps": laps,
            "incidents": self.incidents,
            "incident_limit": self.limit,
            "incidents_remaining": self.remaining(),
            "approach_margin": self.cfg.incident_approach_margin,
        }

    def roll_lap(self, lap: int | None) -> None:
        """At a lap line: offs that left the window stop counting (and re-arm)."""
        self.offs = self._window(lap)
        if len(self.offs) < self.cfg.track_limit_offs:
            self._offs_latched = False

    # ---- penalties -----------------------------------------------------
    def observe_flags(self, active: Sequence[str], session_time: float) -> list[dict]:
        """One payload per driver flag shown or cleared since the last frame."""
        now = penalty_flags(active)
        before = self.penalty
        out: list[dict] = []
        if now == before:
            self._penalty_seen = True
            return out
        for flag in now:
            if flag not in before:
                out.append(self._penalty_payload(flag, "shown", now, before))
        if self._penalty_seen:
            # A flag that was never seen shown cannot be "cleared" on the first frame.
            for flag in before:
                if flag not in now:
                    out.append(self._penalty_payload(flag, "cleared", now, before))
        self._penalty_seen = True
        self.penalty = now
        self.penalty_since = session_time if now else None
        return out

    def _penalty_payload(
        self, flag: str, state: str, now: list[str], before: list[str]
    ) -> dict:
        return {
            "flag": flag,
            "state": state,
            "active": list(now),
            "previous": list(before),
            "serve_in_pit": flag in SERVE_IN_PIT and state == "shown",
            "incidents": self.incidents,
            "incident_limit": self.limit,
        }

    # ---- blue-flag compliance -------------------------------------------
    def observe_blue(
        self,
        blue: Mapping[str, Any] | None,
        player_progress: float | None,
        session_time: float,
    ) -> dict | None:
        """Hold a blue-flag episode; a payload once we have held the car up.

        ``blue`` is :func:`~rtv.racestate.detectors.detect_blue_flag`'s payload (the
        nearest car lapping us inside the blue-flag gap), ``player_progress`` our laps
        completed (lap + lap fraction). Held up means the same car stayed within
        ``blue_flag_held_gap_s`` behind us for ``blue_flag_corners`` corners (buckets of
        the lap we covered) *and* ``blue_flag_held_s`` seconds: both, so neither a
        short lap nor a slow corner alone makes a car that is simply passing "held up".
        """
        gap = blue.get("gap") if blue else None
        close = isinstance(gap, int | float) and gap <= self.cfg.blue_flag_held_gap_s
        if not close or player_progress is None:
            self._blue = None
            return None
        idx = blue.get("car_idx")
        if self._blue is None or self._blue.get("car_idx") != idx:
            self._blue = {"car_idx": idx, "since": player_progress, "since_t": session_time}
            return None
        held = player_progress - float(self._blue["since"])
        held_s = session_time - float(self._blue["since_t"])
        corners = int(held * self.cfg.corner_buckets)
        if (
            corners < self.cfg.blue_flag_corners
            or held_s < self.cfg.blue_flag_held_s
            or idx in self._blue_reported
        ):
            return None
        if isinstance(idx, int):
            self._blue_reported.add(idx)
        return {
            "car_idx": idx,
            "position": blue.get("position"),
            "gap": gap,
            "laps_ahead": blue.get("laps_ahead"),
            "corners": corners,
            "held_s": round(held_s, 3),
            "threshold_corners": self.cfg.blue_flag_corners,
        }

    # ---- state ------------------------------------------------------------
    def summary(self, missing: Mapping[str, list[str]], lap: int | None) -> RaceControlState:
        offs = self._window(lap)
        return RaceControlState(
            incidents=self.incidents,
            incident_limit=self.limit,
            incidents_remaining=self.remaining(),
            limit_source=self.limit_source,
            incident_stage=self.stage,
            offs_in_window=len(offs),
            offs_window_laps=self.cfg.track_limit_window_laps,
            offs=offs[-8:],
            penalty=list(self.penalty),
            penalty_since=self.penalty_since,
            serve_in_pit=any(f in SERVE_IN_PIT for f in self.penalty),
            missing={k: list(v) for k, v in missing.items()},
        )


__all__ = [
    "INCIDENT_LIMIT_KEY",
    "PENALTY_FLAGS",
    "SERVE_IN_PIT",
    "STAGES",
    "RaceControlMonitor",
    "incident_limit_from",
    "penalty_flags",
    "stage_for",
]
