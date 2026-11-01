"""Race director: scripted rehearsals, run from the eval path only.

A race director injects things that did not happen: a full-course yellow on
lap 7, rain arriving at half distance, a mandatory pit window nobody asked for.
It is how you rehearse a strategist against a race that never runs the same way
twice, and how a demo shows a safety car without waiting for one.

Stage 5 shipped the seam and stage 10 the walker:

* :class:`~rtv.director.models.ScenarioScript` -- the Pydantic shape of a scripted
  race, loadable from JSON. Complete and validated.
* :class:`~rtv.director.engine.DirectorEngine` -- the protocol an implementation
  must satisfy, with :class:`~rtv.director.engine.NoopDirector` as the app default.
* :func:`~rtv.director.engine.injected_event` -- the one function that turns a
  script entry into a :class:`~rtv.racestate.models.RaceEvent`.
* :class:`~rtv.director.scheduler.ScriptedDirector` -- the scheduler that walks a
  script against the race state, and the ``injected`` overlay it keeps
  (:mod:`rtv.director.overlay` reads it).

The director **never runs in the app**: a director makes things happen that did
not happen, so only ``evals/run_director.py`` builds a ``ScriptedDirector``, over
the synthetic race or a stored session, and writes its journal outside
``data/sessions``. The app keeps ``NoopDirector``; ``RTV_PITWALL_DIRECTOR_SCRIPT``
only validates a script.

What *is* proven is the seam. ``PitwallOrchestrator(director=...)`` polls a
director and publishes whatever it returns onto the **same**
:class:`~rtv.racestate.bus.EventBus` the detectors publish onto, so an injected
event reaches the agents, ``/ws/pitwall`` and the ring buffer through exactly one
code path. ``tests/test_director.py`` asserts that a scripted full-course yellow
routes, wakes and reads identically to one the sim actually threw.

See ``docs/director_scenario.example.json`` for a worked script and
``docs/PITWALL.md`` (the race director) for the design notes.
"""

from __future__ import annotations

from rtv.director.engine import (
    DIRECTOR_ID_KEY,
    DIRECTOR_KIND_KEY,
    INJECTED_KEY,
    DirectorEngine,
    NoopDirector,
    injected_event,
    is_injected,
)
from rtv.director.models import (
    InjectionKind,
    InjectionTrigger,
    ScenarioScript,
    ScriptedInjection,
)
from rtv.director.scheduler import ScriptedDirector

__all__ = [
    "DIRECTOR_ID_KEY",
    "DIRECTOR_KIND_KEY",
    "INJECTED_KEY",
    "DirectorEngine",
    "InjectionKind",
    "InjectionTrigger",
    "NoopDirector",
    "ScenarioScript",
    "ScriptedDirector",
    "ScriptedInjection",
    "injected_event",
    "is_injected",
]
