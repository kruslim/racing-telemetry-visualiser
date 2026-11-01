"""Test-only data for the post-race debrief (stage 09 part B): one scripted race segment.

The ground-truth scenario of :mod:`evals.synthetic_race` has no session type and no corners,
so it gets no debrief and no lap findings. This variant is a single *race* segment (one
SessionNum, race phases as in :mod:`evals.synthetic_weekend`, so autonomy arms at green and
stands down at the flag) with three scripted corners, a recurring lock-up and lap-to-lap
corner depth changes, so the Layer-1 findings have corners that lose time on several laps.

The same frames are stored (``seed_debrief_session``) and replayed (``debrief_source``), so
the replay's journal and the store's lap table describe one race. Nothing here is used at
runtime (CLAUDE.md: no fake data in the app).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

from evals.synthetic_race import (
    ScenarioSpec,
    scenario_catalog,
    scenario_frames,
    scenario_session_info,
    seed_scenario_session,
)
from evals.synthetic_weekend import _race_phases
from rtv.ingest.frame import Frame
from rtv.racestate.replay import ReplaySource

DEBRIEF_SESSION_ID = "debrief-race"

DEBRIEF_RACE = ScenarioSpec(
    laps=7,
    end_pct=0.6,
    race_laps=7,
    session_num=0,
    sessions=("Race",),
    corner_pcts=(0.2, 0.5, 0.75),
    lockup_laps=(2, 3),
    pit_entry_lap=5,
    pit_exit_lap=6,
)


def debrief_frames(spec: ScenarioSpec = DEBRIEF_RACE) -> Iterator[Frame]:
    """The race with its phases (parade, green, checkered) on SessionState."""
    for frame in scenario_frames(spec):
        frame.values["SessionTick"] = frame.tick
        yield _race_phases(frame, spec)


def seed_debrief_session(writer, session_id: str = DEBRIEF_SESSION_ID) -> str:
    return seed_scenario_session(
        writer, session_id, DEBRIEF_RACE, frames=debrief_frames(DEBRIEF_RACE)
    )


def debrief_source(session_id: str = DEBRIEF_SESSION_ID) -> ReplaySource:
    spec = DEBRIEF_RACE
    return ReplaySource(
        catalog=scenario_catalog(spec),
        frames=debrief_frames(spec),
        session_id=session_id,
        session_info=scenario_session_info(spec),
    )


__all__ = [
    "DEBRIEF_RACE",
    "DEBRIEF_SESSION_ID",
    "debrief_frames",
    "debrief_source",
    "replace",
    "seed_debrief_session",
]
