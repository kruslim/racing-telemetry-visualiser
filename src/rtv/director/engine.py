"""The director seam: a protocol, a default that does nothing, and one factory.

There is no scheduler in this module and that is intentional. Stage 5's job was
to prove that an *injected* event and a *detected* one are the same thing to
everything downstream -- so what ships is the interface, the default, and the
function that turns a script entry into a
:class:`~rtv.racestate.models.RaceEvent`. Whoever implements the walker gets a
seam that is already exercised by tests rather than a design sketch.

Provenance, and why injected events are not *quite* anonymous
-------------------------------------------------------------
An injected event is indistinguishable from a detector event in every way that
changes behaviour: same :class:`~rtv.racestate.models.EventType`, same severity,
same :attr:`~rtv.racestate.models.RaceEvent.key`, same bus, same ring buffer,
same agent routing, same WebSocket frame. Nothing downstream branches on where it
came from, and ``tests/test_director.py`` asserts exactly that.

What it *does* carry is three extra payload keys -- ``injected``,
``director_kind`` and ``director_id``. This codebase refuses to let an agent
assert a number it cannot trace; letting the event log confuse "the sim threw a
yellow" with "we made one up" would be the same failure one level down. The keys
are additive, so a consumer that does not care never sees them, and a script
cannot overwrite them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from rtv.director.models import ScenarioScript, ScriptedInjection
from rtv.logging import get_logger
from rtv.racestate.models import RaceEvent, RaceState

log = get_logger("director")

#: Payload key marking an event as the director's work rather than a detector's.
INJECTED_KEY = "injected"
#: Payload key carrying the :class:`~rtv.director.models.InjectionKind`.
DIRECTOR_KIND_KEY = "director_kind"
#: Payload key carrying the script entry id, so a call can be traced to a line.
DIRECTOR_ID_KEY = "director_id"


def injected_event(
    injection: ScriptedInjection,
    state: RaceState,
    *,
    tick: int | None = None,
    session_time: float | None = None,
    lap: int | None = None,
) -> RaceEvent:
    """Build the :class:`RaceEvent` one script entry stands for.

    Stamped from ``state`` rather than from a wall clock, for the same reason the
    detectors are: an event log with no wall-clock field replays byte-identically,
    and a director that broke that would make every determinism test a lie.

    The overrides exist for the one case ``state`` cannot answer -- an
    implementation that decides an injection should be dated to the moment its
    condition became true rather than to the frame it noticed.
    """
    payload: dict[str, Any] = dict(injection.payload)
    payload[INJECTED_KEY] = True
    payload[DIRECTOR_KIND_KEY] = injection.kind.value
    payload[DIRECTOR_ID_KEY] = injection.id
    return RaceEvent(
        event_type=injection.event_type,
        tick=tick if tick is not None else state.tick,
        session_time=session_time if session_time is not None else state.session_time,
        lap=lap if lap is not None else state.player.lap,
        severity=injection.severity,
        payload=payload,
        state_version=state.version,
    )


def is_injected(event: RaceEvent) -> bool:
    """True when the director produced this event. Nothing in the hot path calls
    this -- it is for a UI that wants to badge a scripted yellow, and for tests."""
    return bool(event.payload.get(INJECTED_KEY))


@runtime_checkable
class DirectorEngine(Protocol):
    """What the orchestrator will call. Four methods, no state assumptions.

    :meth:`poll` is the whole contract: given the current race state, return the
    events (if any) that should be injected *now*. It is called from the
    orchestrator's pump loop, so it must be cheap, synchronous and must not
    block -- the same discipline the deterministic detectors work under.

    An implementation is free to be as clever as it likes about *when* to fire;
    it is not free to invent an event that the rest of the system cannot explain.
    Build events with :func:`injected_event` so provenance is stamped consistently.
    """

    #: Short label for logs and the ``/api/v1/pitwall/status`` payload.
    name: str

    def bind(self, script: ScenarioScript | None) -> None:
        """Load (or clear) the script this director will run."""
        ...

    def poll(self, state: RaceState) -> Sequence[RaceEvent]:
        """Events to inject right now. ``()`` is the overwhelmingly common answer."""
        ...

    def reset(self) -> None:
        """Forget what has already fired, between sessions or replays."""
        ...

    def describe(self) -> dict[str, Any]:
        """Status payload: what is loaded, what has fired, what is still pending."""
        ...


class NoopDirector:
    """The default director: accepts a script, never fires anything.

    Not a placeholder to be deleted -- it is what runs in every deployment that
    is not deliberately rehearsing something, and it is what keeps
    ``director`` from needing a ``None`` check on the pump's hot path. A script
    bound to it is still *validated*, which makes ``NoopDirector`` a useful way
    to lint a script without running a race.
    """

    name = "noop"
    #: False until a real walker exists. Reported on /api/v1/pitwall/status so an
    #: operator who loads a script and sees nothing happen is told why.
    implemented = False

    def __init__(self, script: ScenarioScript | None = None) -> None:
        self.script: ScenarioScript | None = None
        self.polls = 0
        self.bind(script)

    def bind(self, script: ScenarioScript | None) -> None:
        self.script = script
        if script is not None:
            log.info(
                "Director script %r loaded and validated (%d injections). The "
                "race-director layer is planned, not implemented: nothing will "
                "be injected.",
                script.name,
                len(script.injections),
            )

    def poll(self, state: RaceState) -> Sequence[RaceEvent]:  # noqa: ARG002 - protocol
        self.polls += 1
        return ()

    def reset(self) -> None:
        self.polls = 0

    def describe(self) -> dict[str, Any]:
        return {
            "director": self.name,
            "implemented": self.implemented,
            "status": "planned",
            "script": self.script.name if self.script is not None else None,
            "injections": len(self.script.injections) if self.script is not None else 0,
            "fired": 0,
            "polls": self.polls,
            "reason": "The race-director layer ships as interfaces only in stage 5; "
            "NoopDirector never injects. See docs/PITWALL.md.",
        }
