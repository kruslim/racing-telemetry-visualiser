"""The post-race debrief analyst (pitwall stage 09 part B).

Runs once per race segment, after the session ends, over that segment's journal run. It is
not a radio agent: it never speaks, has no trigger, no cooldown and no place in the agent
registry, and it does not count against a session's model-call budget (the session is
over). See ``docs/PITWALL.md`` ("Post-race debrief").

* :mod:`rtv.debrief.inputs` -- what it reads (journal, stored summary, Layer-1 findings).
* :mod:`rtv.debrief.contract` -- the structured answer the model is forced to give.
* :mod:`rtv.debrief.checks` -- every figure, lap, corner and priority held to the inputs.
* :mod:`rtv.debrief.service` -- generate, store (``<run_id>.debrief.json`` plus a journal
  record), load, and the session-end hook.
"""

from __future__ import annotations

from rtv.debrief.contract import SECTIONS, DebriefAnswer
from rtv.debrief.service import DebriefService, deterministic_half

__all__ = ["SECTIONS", "DebriefAnswer", "DebriefService", "deterministic_half"]
