"""Test-only: practice, qualifying and race of one event in a single frame stream.

The live poller keeps one RTV session for a whole iRacing subsession, so a race
weekend reaches the engine as one stream in which ``SessionNum`` steps 0 -> 1 -> 2
and the session clock restarts. This builds exactly that out of the scripted
race (:mod:`evals.synthetic_race`): three segments, one catalog (with
``SessionNum``), one session-info document listing the three sessions.

* practice (SessionNum 0): racing under green from the first frame.
* qualify (SessionNum 1): the same, shorter.
* race (SessionNum 2): parade laps (pre-green) for the first 20 % of lap 1, then
  green; the leader's checkered flag from 50 % of the second-last lap, the
  player takes it on the last lap's crossing, cool-down from 30 % of the last lap.

Never served by the app; tests and evals only.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

from evals.synthetic_race import GREEN, ScenarioSpec, scenario_catalog, scenario_frames
from evals.synthetic_race import scenario_session_info as _info
from rtv.catalog.models import Catalog
from rtv.ingest.frame import Frame
from rtv.racestate.replay import ReplaySource

SESSIONS = ("Practice", "Open Qualify", "Race")
CHECKERED = 0x00000001
WEEKEND_SESSION_ID = "weekend"

PRACTICE = ScenarioSpec(laps=4, end_pct=0.5, session_num=0, sessions=SESSIONS)
QUALIFY = ScenarioSpec(laps=2, end_pct=0.5, session_num=1, sessions=SESSIONS)
RACE = ScenarioSpec(laps=6, end_pct=0.6, session_num=2, sessions=SESSIONS)


def weekend_catalog() -> Catalog:
    return scenario_catalog(RACE)


def weekend_session_info() -> dict:
    return _info(RACE)


def _race_phases(frame: Frame, spec: ScenarioSpec) -> Frame:
    lap = frame.values["Lap"]
    pct = frame.values["LapDistPct"]
    values = frame.values
    if lap == 1 and pct < 0.2:
        values["SessionState"] = 3  # parade_laps
        values["SessionFlags"] = 0
    elif (lap == spec.laps - 1 and pct >= 0.5) or lap == spec.laps:
        values["SessionState"] = 6 if lap == spec.laps and pct >= 0.3 else 5
        values["SessionFlags"] = CHECKERED | GREEN
    return frame


def weekend_frames(
    practice: ScenarioSpec = PRACTICE,
    qualify: ScenarioSpec = QUALIFY,
    race: ScenarioSpec = RACE,
) -> Iterator[Frame]:
    """The three sessions back to back; ticks keep counting, the clock restarts."""
    tick0 = 0
    for spec in (practice, qualify, race):
        last = tick0
        for frame in scenario_frames(spec):
            frame = replace(frame, tick=frame.tick + tick0)
            frame.values["SessionTick"] = frame.tick
            if spec is race:
                frame = _race_phases(frame, spec)
            last = frame.tick
            yield frame
        tick0 = last + 1


def weekend_source(**specs: ScenarioSpec) -> ReplaySource:
    return ReplaySource(
        catalog=weekend_catalog(),
        frames=weekend_frames(**specs),
        session_id=WEEKEND_SESSION_ID,
        session_info=weekend_session_info(),
    )
