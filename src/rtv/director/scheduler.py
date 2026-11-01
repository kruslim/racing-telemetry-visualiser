"""The scheduler: :class:`ScriptedDirector` walks a :class:`ScenarioScript` against the race.

Stage 5 shipped the seam (the protocol, :class:`~rtv.director.engine.NoopDirector`,
:func:`~rtv.director.engine.injected_event`) and left this walker unwritten. It is the
rehearsal tool: a scripted caution, rain arriving or a stop becoming mandatory, fired
into a recorded or synthetic race so the agents can be tested against a race that never
happened. That is also why it **never runs in the app**. The app builds
:class:`NoopDirector` (``services.build_director``); this class is built only by the
eval runner (``evals/run_director.py``), which writes its own journal outside
``data/sessions``.

Firing rules
------------
:meth:`ScriptedDirector.poll` is called on the pump's hot path, so it is synchronous,
does no I/O and never raises (a failure is logged and counted, and costs only that
poll's injections). For each entry, in reading order, the conjunctive
:class:`~rtv.director.models.InjectionTrigger` is evaluated against the state:

* ``at_session_time``: the session clock is at or past it.
* ``at_lap``: the player is on that lap or later; with ``at_lap_dist_pct`` the point
  ``(lap, pct)`` has been reached. ``at_lap_dist_pct`` alone holds once the player is at
  or past that fraction of the current lap. "Or later" so that a poll that missed the
  exact frame still fires (stamped from the state it saw), instead of never.
* ``after`` + ``delay_s``: the named entry has fired and ``delay_s`` **session** seconds
  have passed since it last did. Session seconds, not wall clock: a 4x replay rehearses
  the same race.
* ``while_flag``: the *effective* flag phase is that phase. Effective means an active
  injected flag (from this script) wins over the telemetry's, so "rain while green"
  does not land inside a scripted caution.
* ``once`` (default): fire at most once. ``once: false`` fires again each time the
  conditions become true after having been false.

Firing is edge-triggered and idempotent per entry: a condition that stays true does not
fire again, and an entry already fired with ``once`` never does. Entries fire in script
order, so an ``after`` entry with no delay can fire on the same poll as its parent.

The injected overlay
--------------------
Telemetry is never rewritten: while a scripted yellow is out, ``state.flags`` still says
green. :meth:`ScriptedDirector.overlay` lists the injected *conditions* that are in force
(a flag until a scripted green clears it, the newest weather step, each regulation), and
the orchestrator puts that list in every agent's state slice as ``injected``, so an agent
sees why the radio is talking about a yellow, and the validator can cite the injected
payload's numbers with their provenance (``race_state.injected.*``). Momentary events (a
hazard, a traffic call) are not conditions and are not listed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rtv.director.engine import injected_event
from rtv.director.models import InjectionKind, InjectionTrigger, ScenarioScript, ScriptedInjection
from rtv.logging import get_logger
from rtv.racestate.models import EventType, FlagPhase, RaceEvent, RaceState
from rtv.racestate.plan import MANDATORY_PIT_STOP

log = get_logger("director.scheduler")

#: The overlay key of the injected flag (one at a time, like the sim's own).
FLAG_CONDITION = "flag"
#: The overlay key of the newest injected weather step.
WEATHER_CONDITION = "weather"


class ScriptedDirector:
    """A :class:`~rtv.director.engine.DirectorEngine` that walks a scenario script."""

    name = "scripted"
    #: Reported on the status payload; the rehearsal tool is the real walker.
    implemented = True

    def __init__(self, script: ScenarioScript | None = None) -> None:
        self.script: ScenarioScript | None = None
        self.polls = 0
        self.errors = 0
        self._fired: dict[str, list[float]] = {}
        self._was_true: dict[str, bool] = {}
        self._active: dict[str, dict[str, Any]] = {}
        #: Every firing, in order: id, event key, session time, tick, lap.
        self.log: list[dict[str, Any]] = []
        self.bind(script)

    # ---- the protocol ------------------------------------------------------
    def bind(self, script: ScenarioScript | None) -> None:
        self.script = script
        self.reset()
        if script is not None:
            log.info(
                "Director script %r bound (%d injections).", script.name, len(script.injections)
            )

    def reset(self) -> None:
        """Forget what fired: stage 07 resets the pitwall (and so this) per segment."""
        self.polls = 0
        self._fired = {}
        self._was_true = {}
        self._active = {}
        self.log = []

    def poll(self, state: RaceState) -> Sequence[RaceEvent]:
        try:
            return self._poll(state)
        except Exception:
            self.errors += 1
            log.exception("Scripted director failed while polling; nothing injected")
            return ()

    def describe(self) -> dict[str, Any]:
        script = self.script
        fired = list(self.log)
        pending = [
            i.describe()
            for i in (script.injections if script is not None else [])
            if i.id not in self._fired or not i.when.once
        ]
        return {
            "director": self.name,
            "implemented": self.implemented,
            "status": "loaded" if script is not None else "idle",
            "script": script.name if script is not None else None,
            "injections": len(script.injections) if script is not None else 0,
            "fired": len(fired),
            "fired_log": fired,
            "pending": pending,
            "active": sorted(self._active),
            "polls": self.polls,
            "errors": self.errors,
        }

    # ---- the overlay ---------------------------------------------------------
    def overlay(self, state: RaceState | None = None) -> list[dict[str, Any]]:
        """The injected conditions in force, oldest first (the state slice's ``injected``).

        Each entry is ``{kind, event_type, director_id, since, lap, payload}``; a
        mandatory-stop regulation also carries ``served`` (a pit exit has happened since,
        read from the player's stint counter in ``state``).
        """
        out: list[dict[str, Any]] = []
        for entry in sorted(self._active.values(), key=lambda e: (e["since"], e["director_id"])):
            item = {k: v for k, v in entry.items() if not k.startswith("_")}
            stint = entry.get("_stint")
            if stint is not None:
                current = state.player.stint if state is not None else stint
                item["served"] = current > stint
            out.append(item)
        return out

    def effective_flag(self, state: RaceState) -> FlagPhase:
        """The injected flag while one is out, else the telemetry's."""
        flag = self._active.get(FLAG_CONDITION)
        if flag is not None:
            try:
                return FlagPhase(flag["payload"].get("to"))
            except ValueError:
                pass
        return state.flags.phase

    # ---- the walker ------------------------------------------------------------
    def _poll(self, state: RaceState) -> tuple[RaceEvent, ...]:
        script = self.script
        if script is None or not script.injections:
            return ()
        self.polls += 1
        out: list[RaceEvent] = []
        for injection in script.injections:
            holds = self._holds(injection.when, state)
            was = self._was_true.get(injection.id, False)
            self._was_true[injection.id] = holds
            if not holds or was:
                continue  # edge-triggered: only a false-to-true change fires
            if injection.when.once and injection.id in self._fired:
                continue
            event = injected_event(injection, state)
            self._fired.setdefault(injection.id, []).append(state.session_time)
            self._apply(injection, event, state)
            self.log.append(
                {
                    "id": injection.id,
                    "event_key": event.key,
                    "session_time": event.session_time,
                    "tick": event.tick,
                    "lap": event.lap,
                }
            )
            out.append(event)
        return tuple(out)

    def _holds(self, when: InjectionTrigger, state: RaceState) -> bool:
        lap = state.player.lap
        pct = state.player.lap_dist_pct
        if when.at_session_time is not None and state.session_time < when.at_session_time:
            return False
        if when.at_lap is not None:
            if lap is None or lap < when.at_lap:
                return False
            if when.at_lap_dist_pct is not None and lap == when.at_lap:
                if pct is None or pct < when.at_lap_dist_pct:
                    return False
        elif when.at_lap_dist_pct is not None:
            if pct is None or pct < when.at_lap_dist_pct:
                return False
        if when.after is not None:
            times = self._fired.get(when.after)
            if not times or state.session_time - times[-1] < when.delay_s:
                return False
        if when.while_flag is not None and self.effective_flag(state) is not when.while_flag:
            return False
        return True

    def _apply(self, injection: ScriptedInjection, event: RaceEvent, state: RaceState) -> None:
        """Update the overlay for a fired entry (conditions only, not momentary events)."""
        entry = {
            "kind": injection.kind.value,
            "event_type": injection.event_type.value,
            "director_id": injection.id,
            "since": event.session_time,
            "lap": event.lap,
            "payload": dict(injection.payload),
        }
        if injection.event_type is EventType.FLAG_CHANGE:
            if injection.payload.get("to") == FlagPhase.GREEN.value:
                self._active.pop(FLAG_CONDITION, None)
            else:
                self._active[FLAG_CONDITION] = entry
        elif injection.event_type is EventType.WEATHER_CHANGE or (
            injection.kind is InjectionKind.WEATHER
        ):
            self._active[WEATHER_CONDITION] = entry
        elif injection.event_type is EventType.REGULATION_CHANGE:
            regulation = str(injection.payload.get("regulation") or injection.id)
            if regulation == MANDATORY_PIT_STOP:
                entry["_stint"] = state.player.stint
            self._active[f"regulation:{regulation}"] = entry
