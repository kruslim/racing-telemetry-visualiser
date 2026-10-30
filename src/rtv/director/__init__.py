"""Race director -- **planned**. This package is the seam, not the feature.

A race director injects things that did not happen: a full-course yellow on
lap 7, rain arriving at half distance, a mandatory pit window nobody asked for.
It is how you rehearse a strategist against a race that never runs the same way
twice, and how a demo shows a safety car without waiting for one.

Stage 5 ships the **interfaces only**, deliberately:

* :class:`~rtv.director.models.ScenarioScript` -- the Pydantic shape of a scripted
  race, loadable from JSON. Complete and validated.
* :class:`~rtv.director.engine.DirectorEngine` -- the protocol an implementation
  must satisfy, with :class:`~rtv.director.engine.NoopDirector` as the default.
* :func:`~rtv.director.engine.injected_event` -- the one function that turns a
  script entry into a :class:`~rtv.racestate.models.RaceEvent`.

What is *not* here is the scheduler: the thing that walks a script, evaluates
:class:`~rtv.director.models.InjectionTrigger` conditions against a live
:class:`~rtv.racestate.models.RaceState` and decides that now is the moment. That
is the implementation, and inventing it here would have meant guessing at
questions (does a director own the sim's weather API? may it rewrite fuel
regulations mid-race?) that belong to whoever builds it.

What *is* proven is the seam. ``PitwallOrchestrator(director=...)`` polls a
director and publishes whatever it returns onto the **same**
:class:`~rtv.racestate.bus.EventBus` the detectors publish onto, so an injected
event reaches the agents, ``/ws/pitwall`` and the ring buffer through exactly one
code path. ``tests/test_director.py`` asserts that a scripted full-course yellow
routes, wakes and reads identically to one the sim actually threw.

See ``docs/director_scenario.example.json`` for a worked script and
``docs/PITWALL.md`` (stage 5) for the design notes.
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

__all__ = [
    "DIRECTOR_ID_KEY",
    "DIRECTOR_KIND_KEY",
    "INJECTED_KEY",
    "DirectorEngine",
    "InjectionKind",
    "InjectionTrigger",
    "NoopDirector",
    "ScenarioScript",
    "ScriptedInjection",
    "injected_event",
    "is_injected",
]
