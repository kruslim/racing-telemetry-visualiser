"""The race plan: the pitwall's memory of what it intends to do, and how it changes.

Each agent run is woken by an event, reads a slice, speaks once and forgets. The plan
is the one thing that persists across runs: how many stops are left and on which lap,
what goes in at the stop, the consumption and pace the stint is run at and the tyre
posture. The strategist revises it; the vehicle engineer and the coach read its tyre
posture and pace target; nobody else writes it.

* The schema (:class:`RacePlan`, :class:`PlanElement`, :class:`PlanRevision`) lives in
  :mod:`rtv.racestate.models` because :class:`RaceState` carries it (``state.plan``), so
  ``/ws/pitwall``, the journal's state snapshots and the final snapshot all have it. It
  is re-exported here.
* :func:`refresh_plan` derives the baseline from :func:`~rtv.racestate.strategy_math.stop_plan`
  and :mod:`rtv.racestate.projection` as soon as the data supports an element, and keeps
  derived elements current. It degrades like everything else: no fuel model means no
  fuel plan, not a default one. The engine calls it at each lap line and pit exit of a
  race segment; the segment reset clears the plan.
* :func:`propose_revision` is the only way an agent changes the plan. A proposal is not a
  write: it lands only when every number in it matches a typed fact from the
  *deterministic* data (never an earlier plan value, so the plan cannot launder a guess
  into a citable fact) and :func:`sanity_check` passes. A rejected proposal is recorded
  with its reason and changes nothing.
* :func:`plan_view` is what an agent is shown: values, units in the field names (so the
  typed FactSet reads them as ``race_state.plan.next_stop_lap = 13 lap``), provenance and
  version, and no free text (an agent-written rationale could carry a figure).
* A ``regulation_change`` event for a ``mandatory_pit_stop`` (only the race director
  emits one, in an eval rehearsal) makes a stop required inside its lap window even when
  the tank reaches the flag: :func:`mandatory_stop` reads it from the event history, and
  :func:`derive`, :func:`sanity_check` and :func:`propose_revision` honour it. With no
  such event (every live session) they behave exactly as before.

Pure and model-free. The race-state engine applies it under its lock.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from rtv.racestate import projection as pj
from rtv.racestate import strategy_math as sm
from rtv.racestate.models import (
    EventType,
    PlanElement,
    PlanRevision,
    RaceEvent,
    RacePlan,
    RaceState,
)

__all__ = [
    "ELEMENTS",
    "MANDATORY_PIT_STOP",
    "MandatoryStop",
    "PlanElement",
    "PlanRevision",
    "ProposalOutcome",
    "RacePlan",
    "is_race_segment",
    "mandatory_stop",
    "plan_view",
    "propose_revision",
    "proposal_facts",
    "refresh_plan",
    "sanity_check",
]

#: The plan's elements, in display order, with the unit each is reported in.
ELEMENTS: dict[str, str | None] = {
    "stop_count": None,
    "next_stop_lap": "lap",
    "fuel_to_add_l": "L",
    "fuel_per_lap_target_l": "L/lap",
    "pace_target_s": "s",
    "tyre_posture": None,
}
POSTURES = ("push", "neutral", "conserve")
#: Elements that describe the stop; a pit exit consumes them.
STOP_ELEMENTS = ("stop_count", "next_stop_lap", "fuel_to_add_l", "fuel_per_lap_target_l")
#: Elements that describe the tyre set on the car; a pit exit resets them.
TYRE_ELEMENTS = ("pace_target_s", "tyre_posture")

#: A derived value moves by less than this and the plan keeps the old one (no churn).
REFRESH_TOLERANCE: dict[str, float] = {
    "fuel_to_add_l": 1.0,
    "fuel_per_lap_target_l": 0.02,
    "pace_target_s": 0.3,
}
#: Degradation above this (s per lap) with this many stint laps still to run is "conserve".
CONSERVE_LOSS_S_PER_LAP = 0.10
CONSERVE_MIN_LAPS_LEFT = 5
#: A pace target further than this fraction from the fitted (or median) lap time is
#: not a target, it is a different race.
PACE_BAND = 0.03
#: Planned consumption below this fraction of the learned one is not a reachable save.
MAX_FUEL_SAVE_FRACTION = 0.15
#: Revisions kept on the plan (the journal keeps every one).
MAX_REVISIONS = 40


#: The ``regulation`` of a ``regulation_change`` payload that makes a stop mandatory.
MANDATORY_PIT_STOP = "mandatory_pit_stop"


@dataclass(frozen=True)
class MandatoryStop:
    """A stop a regulation requires, between two laps (inclusive; ``None`` = open)."""

    from_lap: int | None
    to_lap: int | None
    #: Session time the regulation came in; a pit exit after it serves it.
    since: float
    served: bool = False

    def pending(self, state: RaceState) -> bool:
        """Still to be made: not served, and its last lap not yet run."""
        if self.served:
            return False
        lap = state.player.lap
        return self.to_lap is None or lap is None or lap <= self.to_lap

    def allows(self, lap: int) -> bool:
        return (self.from_lap is None or lap >= self.from_lap) and (
            self.to_lap is None or lap <= self.to_lap
        )

    def window(self) -> str:
        lo = "now" if self.from_lap is None else f"lap {self.from_lap}"
        hi = "the flag" if self.to_lap is None else f"lap {self.to_lap}"
        return f"{lo} to {hi}"


def _lap_of(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def regulation_stop(payload: Mapping[str, Any], since: float = 0.0) -> MandatoryStop | None:
    """The mandatory stop a regulation payload declares, or ``None``."""
    if str(payload.get("regulation") or "") != MANDATORY_PIT_STOP:
        return None
    return MandatoryStop(
        from_lap=_lap_of(payload.get("applies_from_lap")),
        to_lap=_lap_of(payload.get("applies_to_lap")),
        since=float(since),
    )


def mandatory_stop(events: Iterable[RaceEvent]) -> MandatoryStop | None:
    """The newest mandatory-stop regulation in ``events``, marked served by a later pit exit.

    Only the race director emits ``regulation_change`` (an eval rehearsal), so on a live
    session this is always ``None``.
    """
    found: MandatoryStop | None = None
    for event in events:
        if event.event_type is EventType.REGULATION_CHANGE:
            stop = regulation_stop(event.payload, event.session_time)
            if stop is not None:
                found = stop
        elif (
            found is not None
            and event.event_type is EventType.PIT_EXIT
            and event.session_time >= found.since
        ):
            found = MandatoryStop(found.from_lap, found.to_lap, found.since, served=True)
    return found


def is_race_segment(state: RaceState) -> bool:
    """The plan exists only in race segments (stage 07's session type)."""
    return state.lifecycle.session_type == "race"


# --------------------------------------------------------------------------
# derivation
# --------------------------------------------------------------------------
def _element(value: Any, name: str, rationale: str, version: int, lap: int | None) -> PlanElement:
    return PlanElement(
        value=value,
        unit=ELEMENTS[name],
        provenance="derived",
        rationale=rationale,
        version=version,
        set_by="engine",
        set_at_lap=lap,
    )


def _value(plan: RacePlan | None, name: str) -> Any:
    element = getattr(plan, name, None) if plan is not None else None
    return None if element is None else element.value


def stops_needed(state: RaceState) -> int | None:
    """Stops the fuel picture needs before the flag, or ``None`` without a fuel model."""
    fuel = state.fuel
    if fuel.margin_laps is None or fuel.margin_l is None:
        return None
    if fuel.margin_laps >= 0:
        return 0
    short_l = -fuel.margin_l
    if fuel.capacity and fuel.capacity > 0:
        return max(1, math.ceil(short_l / fuel.capacity - 1e-6))
    return 1


def derive(
    state: RaceState,
    plan: RacePlan | None,
    *,
    pit_lane_loss_s: float,
    events: Sequence[RaceEvent] = (),
) -> tuple[dict[str, tuple[Any, str]], dict[str, str]]:
    """Every element the deterministic data supports now: ``{name: (value, rationale)}``.

    ``next_stop_lap`` keeps a standing agent or operator choice (the fuel to add is then
    priced for that lap); everything else is what the engine would choose. A pending
    mandatory stop in ``events`` (:func:`mandatory_stop`) means at least one stop.
    """
    out: dict[str, tuple[Any, str]] = {}
    missing: dict[str, str] = {}
    stop = sm.stop_plan(state)
    rule = mandatory_stop(events)
    rule = rule if rule is not None and rule.pending(state) else None
    if stop.grounded:
        needed = stops_needed(state) or 0
        if not stop.stop_required and rule is not None:
            out["stop_count"] = (
                1,
                f"A regulation requires a stop ({rule.window()}); the fuel needs none.",
            )
            for name in ("next_stop_lap", "fuel_to_add_l"):
                missing[name] = (
                    f"The regulation's stop ({rule.window()}) has no lap planned yet."
                )
        elif not stop.stop_required:
            out["stop_count"] = (0, "The tank reaches the flag with fuel to spare.")
            for name in ("next_stop_lap", "fuel_to_add_l"):
                missing[name] = "No stop is needed: the tank reaches the flag."
        else:
            out["stop_count"] = (needed, f"The fuel picture needs {needed} stop(s) to the flag.")
            lap = stop.target_lap
            chosen = plan.next_stop_lap if plan is not None else None
            if chosen is not None and chosen.provenance != "derived" and chosen.value is not None:
                lap = int(chosen.value)
            if lap is not None:
                out["next_stop_lap"] = (
                    stop.target_lap,
                    f"Latest lap that keeps {stop.reserve_laps:.1f} laps of fuel in hand.",
                )
                outcome = sm.simulate_pit_outcome(
                    state, stop_lap=lap, pit_lane_loss_s=pit_lane_loss_s
                )
                if outcome.fuel_to_add_l is not None:
                    out["fuel_to_add_l"] = (
                        outcome.fuel_to_add_l,
                        f"Refuel for a stop on lap {lap}, from simulate_pit_outcome.",
                    )
                else:
                    missing["fuel_to_add_l"] = outcome.reason or "Race distance unknown."
            else:
                missing["next_stop_lap"] = stop.reason or "No stop lap could be planned."
        out["fuel_per_lap_target_l"] = (
            state.fuel.per_lap,
            f"Learned consumption over {state.fuel.samples} green laps.",
        )
    else:
        reason = stop.reason or "Fuel consumption has not been learned yet."
        for name in STOP_ELEMENTS:
            missing[name] = reason

    deg = pj.lap_time_degradation(state)
    if deg.grounded and deg.current_lap_time_s is not None:
        out["pace_target_s"] = (
            deg.current_lap_time_s,
            f"Fitted lap time at stint lap {deg.stint_laps} over {deg.samples} clean laps.",
        )
        laps_left = _stint_laps_left(state, plan, stop)
        slope = deg.loss_per_lap_s or 0.0
        if slope >= CONSERVE_LOSS_S_PER_LAP and (
            laps_left is None or laps_left >= CONSERVE_MIN_LAPS_LEFT
        ):
            out["tyre_posture"] = (
                "conserve",
                f"Losing {slope:.2f} s a lap per lap with the stint still long.",
            )
        else:
            out["tyre_posture"] = ("neutral", f"Degradation {slope:.2f} s a lap per lap.")
    else:
        reason = deg.reason or "Lap-time degradation is not fitted yet."
        missing["pace_target_s"] = reason
        missing["tyre_posture"] = reason
    return out, missing


def _stint_laps_left(state: RaceState, plan: RacePlan | None, stop: sm.StopPlan) -> int | None:
    lap = state.player.lap
    target = _value(plan, "next_stop_lap") or stop.target_lap
    if lap is None:
        return None
    if target is not None:
        return int(target) - lap
    if state.fuel.laps_to_finish is not None:
        return int(state.fuel.laps_to_finish)
    return None


def _differs(name: str, old: Any, new: Any) -> bool:
    if old is None or new is None:
        return old is not new
    tol = REFRESH_TOLERANCE.get(name)
    if tol is not None and isinstance(old, int | float) and isinstance(new, int | float):
        return abs(float(old) - float(new)) >= tol
    return old != new


def refresh_plan(
    state: RaceState,
    *,
    pit_exit: bool = False,
    pit_lane_loss_s: float = sm.DEFAULT_PIT_LANE_LOSS_S,
    segment: int | None = None,
    events: Sequence[RaceEvent] = (),
) -> PlanRevision | None:
    """Create or refresh the plan in ``state.plan`` from the deterministic data.

    Returns the accepted revision when anything changed (the engine turns it into a
    ``plan_revised`` event), else ``None``. Outside a race segment it does nothing.
    A standing agent or operator element is kept while it is still feasible and
    reverted to the derived value, with the reason, once it is not. A pit exit
    consumes the stop elements and resets the tyre ones.
    """
    if not is_race_segment(state):
        return None
    plan = state.plan
    if pit_exit:
        return _consume_stop(state) if plan is not None else None
    if state.player.on_pit_road:
        # The stop is in progress; the fuel model catches up at the next lap line.
        return None
    derived, missing = derive(state, plan, pit_lane_loss_s=pit_lane_loss_s, events=events)
    if plan is None and not derived:
        return None
    if plan is None:
        plan = RacePlan(segment=segment if segment is not None else state.lifecycle.segment)
    lap = state.player.lap
    version = plan.version + 1
    changes: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []
    new = plan.model_copy(deep=True)
    for name in ELEMENTS:
        current: PlanElement | None = getattr(new, name)
        if current is not None and current.provenance != "derived":
            problem = sanity_check(
                state, {name: current.value}, base=new, only=(name,), events=events
            )
            if problem is None:
                continue
            reasons.append(f"{name} reverted: {problem}")
        if name in derived:
            value, why = derived[name]
            if (
                current is None
                or current.provenance != "derived"
                or _differs(name, current.value, value)
            ):
                changes[name] = {"from": current.value if current else None, "to": value}
                setattr(new, name, _element(value, name, why, version, lap))
        elif current is not None:
            changes[name] = {"from": current.value, "to": None}
            setattr(new, name, None)
    new.missing = {k: v for k, v in missing.items() if getattr(new, k) is None}
    if not changes and state.plan is not None:
        state.plan.missing = new.missing
        return None
    new.version = version
    revision = PlanRevision(
        version=version,
        accepted=True,
        provenance="derived",
        agent=None,
        changes=changes,
        rationale="; ".join(reasons)
        or (
            "Baseline plan from the stop plan and projections."
            if state.plan is None
            else "Derived values moved with the data."
        ),
        lap=lap,
        session_time=state.session_time,
        state_version=state.version,
    )
    new.revisions = [*new.revisions, revision][-MAX_REVISIONS:]
    state.plan = new
    return revision


def _consume_stop(state: RaceState) -> PlanRevision:
    """A pit exit: the planned stop happened. Stop and tyre elements wait for the data.

    The fuel model and the new set's lap times only mean something again from the
    next lap line, so the stop elements and the tyre elements are cleared (with the
    reason) rather than re-derived from a tank that is still being read.
    """
    plan = state.plan
    assert plan is not None
    version = plan.version + 1
    new = plan.model_copy(deep=True)
    new.stops_done += 1
    changes: dict[str, dict[str, Any]] = {}
    for name in ("stop_count", "next_stop_lap", "fuel_to_add_l"):
        element = getattr(new, name)
        if element is not None:
            changes[name] = {"from": element.value, "to": None}
            setattr(new, name, None)
        new.missing[name] = "Stop made; the next stop is planned at the next lap line."
    for name in TYRE_ELEMENTS:
        element = getattr(new, name)
        if element is not None:
            changes[name] = {"from": element.value, "to": None}
            setattr(new, name, None)
        new.missing[name] = "New tyres: no clean laps on this set yet."
    element = new.fuel_per_lap_target_l
    if element is not None and element.provenance != "derived" and state.fuel.per_lap:
        changes["fuel_per_lap_target_l"] = {"from": element.value, "to": state.fuel.per_lap}
        new.fuel_per_lap_target_l = _element(
            state.fuel.per_lap,
            "fuel_per_lap_target_l",
            "Learned consumption again after the stop.",
            version,
            state.player.lap,
        )
    new.version = version
    revision = PlanRevision(
        version=version,
        accepted=True,
        provenance="derived",
        changes=changes,
        rationale=f"Pit stop {new.stops_done} made.",
        lap=state.player.lap,
        session_time=state.session_time,
        state_version=state.version,
    )
    new.revisions = [*new.revisions, revision][-MAX_REVISIONS:]
    state.plan = new
    return revision


# --------------------------------------------------------------------------
# proposals
# --------------------------------------------------------------------------
class ProposalOutcome:
    """The result of :func:`propose_revision`: the revision and what the agent sees."""

    def __init__(self, revision: PlanRevision, proposal: Mapping[str, Any]) -> None:
        self.revision = revision
        self.proposal = dict(proposal)

    @property
    def accepted(self) -> bool:
        return self.revision.accepted

    def to_tool(self, state: RaceState) -> dict[str, Any]:
        """The tool result. A rejection never repeats the proposed numbers: whatever a
        tool returns becomes citable, and a bogus figure must not."""
        if self.accepted:
            return {
                "accepted": True,
                "plan_version": self.revision.version,
                "changed": sorted(self.revision.changes),
                "plan": plan_view(state),
            }
        return {
            "accepted": False,
            "plan_version": state.plan.version if state.plan is not None else None,
            "reason": self.revision.reason,
            "note": "Nothing changed. Re-read the stop plan and projections first.",
        }

    def payload(self, old_version: int | None) -> dict[str, Any]:
        """The ``plan_revised`` event payload (accepted) or the activity record (rejected)."""
        rev = self.revision
        out: dict[str, Any] = {
            "old_version": old_version,
            "new_version": rev.version if rev.accepted else old_version,
            "provenance": rev.provenance,
            "agent": rev.agent,
            "changes": rev.changes,
            "rationale": rev.rationale,
            "lap": rev.lap,
        }
        if not rev.accepted:
            out["reason"] = rev.reason
            out["proposal"] = self.proposal
        return out


#: Proposal field -> the (family, unit) its number must match in the FactSet.
_FIELD_UNITS: dict[str, tuple[str, str] | None] = {
    "stop_count": None,
    "next_stop_lap": ("lap", "lap"),
    "fuel_to_add_l": ("litres", "l"),
    "fuel_per_lap_target_l": ("litres", "l"),
    "pace_target_s": ("s", "s"),
}


def proposal_facts(
    state: RaceState,
    events: Sequence[RaceEvent] = (),
    *,
    stop_lap: int | None = None,
    pit_lane_loss_s: float = sm.DEFAULT_PIT_LANE_LOSS_S,
) -> Any:
    """The deterministic facts a proposal may rest on, as a typed FactSet.

    The strategist's own sources (session, flags, player, fuel, tyres, standings, the
    stop plan, the fuel projection, a pit outcome for the proposed lap), the projection
    layer and any regulation payload, and deliberately *not* the plan: an earlier
    agent-proposed value cannot back a new proposal.
    """
    from rtv.pitwall.validator import FactSet  # lazy: racestate must not need pitwall

    facts = FactSet(typed=True)
    facts.add(
        "race_state",
        {
            "session": state.session.model_dump(mode="json"),
            "flags": state.flags.model_dump(mode="json"),
            "player": state.player.model_dump(mode="json"),
            "fuel": state.fuel.model_dump(mode="json"),
            "tyres": state.tyres.model_dump(mode="json"),
            "standings": sm.standings_around_player(state, window=3),
        },
    )
    facts.add("stop_plan", sm.stop_plan(state).to_tool())
    facts.add("get_fuel_projection", sm.fuel_projection(state))
    facts.add(
        "simulate_pit_outcome",
        sm.simulate_pit_outcome(
            state, stop_lap=stop_lap, pit_lane_loss_s=pit_lane_loss_s
        ).to_tool(),
    )
    facts.add("get_lap_time_degradation", pj.lap_time_degradation(state).to_tool())
    facts.add("get_fuel_save_options", pj.fuel_save_options(state).to_tool())
    facts.add("get_traffic_projection", pj.traffic_projection(state).to_tool())
    for event in events:
        if event.event_type is EventType.REGULATION_CHANGE:
            facts.add("regulation", event.payload)
    return facts


def _coerce(proposal: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
    out: dict[str, Any] = {}
    for name in ELEMENTS:
        if name not in proposal or proposal[name] is None:
            continue
        raw = proposal[name]
        try:
            if name in ("stop_count", "next_stop_lap"):
                if isinstance(raw, bool) or float(raw) != int(float(raw)):
                    return {}, f"{name} must be a whole number."
                out[name] = int(float(raw))
            elif name == "tyre_posture":
                value = str(raw).strip().lower()
                if value not in POSTURES:
                    return {}, f"tyre_posture must be one of {', '.join(POSTURES)}."
                out[name] = value
            else:
                if isinstance(raw, bool):
                    return {}, f"{name} must be a number."
                out[name] = float(raw)
        except (TypeError, ValueError):
            return {}, f"{name} must be a number."
    return out, None


def sanity_check(
    state: RaceState,
    values: Mapping[str, Any],
    *,
    base: RacePlan | None = None,
    only: Sequence[str] | None = None,
    pit_lane_loss_s: float = sm.DEFAULT_PIT_LANE_LOSS_S,
    events: Sequence[RaceEvent] = (),
) -> str | None:
    """Why the plan ``base`` with ``values`` applied is not feasible, or ``None``.

    Deterministic bounds only, and the reason quotes only deterministic figures (the
    window, the tank, the reference lap, a regulation's laps), never the value being
    checked. A pending mandatory stop in ``events`` requires a stop inside its window.
    """
    rule = mandatory_stop(events)
    rule = rule if rule is not None and rule.pending(state) else None
    merged = {name: _value(base, name) for name in ELEMENTS}
    merged.update(values)
    names = list(only) if only is not None else list(values)
    stop = sm.stop_plan(state)
    fuel = state.fuel
    lap = state.player.lap
    stops = merged.get("stop_count")

    if "stop_count" in names and stops is not None:
        needed = stops_needed(state)
        if needed is None:
            return "stop_count needs a fuel model, and consumption has not been learned yet."
        if stops < needed:
            return f"stop_count is fewer stops than the fuel needs ({needed})."
        if rule is not None and stops < 1:
            return f"stop_count: a regulation requires a stop ({rule.window()})."
        if fuel.laps_to_finish is not None and stops > max(0, math.ceil(fuel.laps_to_finish)):
            return "stop_count is more stops than laps left to run."
    if "next_stop_lap" in names and merged.get("next_stop_lap") is not None:
        target = int(merged["next_stop_lap"])
        if not stop.grounded:
            return f"next_stop_lap needs the stop plan, which has no data ({stop.reason})"
        if rule is not None and not rule.allows(target):
            return f"next_stop_lap is outside the regulation's stop window ({rule.window()})."
        if not stop.stop_required and rule is None:
            return "next_stop_lap: the tank reaches the flag (no_stop_needed), so no stop to plan."
        if lap is not None and target < lap:
            return f"next_stop_lap is already behind the car (current lap {lap})."
        if stop.latest_lap is not None and target > stop.latest_lap:
            return (
                f"next_stop_lap is after the run-dry lap {stop.latest_lap} (stop_plan.latest_lap)."
            )
        one_stop = stops in (None, 1)
        if one_stop and stop.earliest_lap is not None and target < stop.earliest_lap:
            return (
                f"next_stop_lap is before the one-stop window opens at lap {stop.earliest_lap} "
                "(stop_plan.earliest_lap)."
            )
    if "fuel_to_add_l" in names and merged.get("fuel_to_add_l") is not None:
        add = float(merged["fuel_to_add_l"])
        target = merged.get("next_stop_lap") or stop.target_lap
        outcome = sm.simulate_pit_outcome(state, stop_lap=target, pit_lane_loss_s=pit_lane_loss_s)
        if outcome.fuel_at_stop_l is None:
            return f"fuel_to_add_l needs a fuel model ({outcome.reason})."
        if add < 0:
            return "fuel_to_add_l cannot be negative."
        room = (fuel.capacity - max(0.0, outcome.fuel_at_stop_l)) if fuel.capacity else None
        if room is not None and add > room + 0.05:
            return f"fuel_to_add_l is more than the tank holds at that stop ({room:.1f} L of room)."
        if (
            stops in (None, 1)
            and outcome.laps_after_stop is not None
            and fuel.per_lap
            and max(0.0, outcome.fuel_at_stop_l) + add + 0.05
            < outcome.laps_after_stop * fuel.per_lap
        ):
            need = outcome.laps_after_stop * fuel.per_lap - max(0.0, outcome.fuel_at_stop_l)
            return f"fuel_to_add_l does not reach the flag (it needs {need:.1f} L)."
    if "fuel_per_lap_target_l" in names and merged.get("fuel_per_lap_target_l") is not None:
        target_l = float(merged["fuel_per_lap_target_l"])
        if not fuel.per_lap or fuel.level is None:
            return "fuel_per_lap_target_l needs a fuel model; consumption is not learned yet."
        if target_l <= 0:
            return "fuel_per_lap_target_l must be positive."
        floor = fuel.per_lap * (1 - MAX_FUEL_SAVE_FRACTION)
        if target_l < floor - 1e-6:
            return (
                f"fuel_per_lap_target_l is a bigger save than {MAX_FUEL_SAVE_FRACTION:.0%} of "
                f"the learned {fuel.per_lap:.3f} L/lap."
            )
        stop_at = merged.get("next_stop_lap")
        need = (
            sm.laps_to_end_of(state, int(stop_at)) if stop_at is not None else fuel.laps_to_finish
        )
        if need is not None and fuel.level / target_l + 1e-6 < need:
            where = f"lap {int(stop_at)}" if stop_at is not None else "the flag"
            return f"fuel_per_lap_target_l: the {fuel.level:.1f} L aboard does not reach {where}."
    if "pace_target_s" in names and merged.get("pace_target_s") is not None:
        pace = float(merged["pace_target_s"])
        reference = _reference_lap(state)
        if reference is None:
            return "pace_target_s needs a reference lap time, and no clean lap is timed yet."
        if abs(pace - reference) > PACE_BAND * reference:
            return (
                f"pace_target_s is more than {PACE_BAND:.0%} from the reference lap time "
                f"{reference:.3f} s."
            )
    if "tyre_posture" in names and merged.get("tyre_posture") is not None:
        if merged["tyre_posture"] not in POSTURES:
            return f"tyre_posture must be one of {', '.join(POSTURES)}."
    return None


def _reference_lap(state: RaceState) -> float | None:
    deg = pj.lap_time_degradation(state)
    if deg.grounded and deg.current_lap_time_s:
        return deg.current_lap_time_s
    clean = [lap.lap_time for lap in pj.clean_stint_laps(state) if lap.lap_time]
    if clean:
        return pj._median([float(x) for x in clean])
    return state.player.best_lap_time


#: A proposed figure matches a fact when it is this close: a structured value is meant
#: to be copied from a tool result (the validator's rule for payload fields), with room
#: only for the last digit of a rounded copy.
PROPOSAL_ABS_TOL = 0.005
PROPOSAL_REL_TOL = 0.001


def _backed(facts: Any, value: float, unit: tuple[str, str] | None) -> bool:
    """Typed and near-exact: a fact of the field's unit family with this value."""
    from rtv.grounding.units import convert

    if unit is None:
        return bool(facts.supports_exact(value)) or value in (0.0, 1.0)
    family, to_unit = unit
    for fact, fact_unit, _path in facts.facts(family):
        spoken = convert(fact, family, fact_unit, to_unit)
        if abs(value - spoken) <= max(PROPOSAL_ABS_TOL, PROPOSAL_REL_TOL * abs(spoken)):
            return True
    return False


def _unsupported(facts: Any, values: Mapping[str, Any]) -> str | None:
    for name, unit in _FIELD_UNITS.items():
        if name not in values:
            continue
        if not _backed(facts, float(values[name]), unit):
            kind = f"{unit[0]} figure" if unit else "figure"
            return (
                f"{name} is not backed by any {kind} in the deterministic data (fuel window, "
                "stop plan, pit outcome, projections); an earlier plan value does not count."
            )
    return None


def propose_revision(
    state: RaceState,
    proposal: Mapping[str, Any],
    *,
    agent: str = "strategist",
    events: Sequence[RaceEvent] = (),
    pit_lane_loss_s: float = sm.DEFAULT_PIT_LANE_LOSS_S,
    check_state: RaceState | None = None,
) -> ProposalOutcome:
    """Apply ``proposal`` to ``state.plan`` if it is grounded and feasible.

    ``check_state`` is the snapshot the agent was shown (the facts and bounds are
    checked against it); ``state`` is the live state the plan is written into. A
    rejected proposal is returned with its reason and leaves the plan untouched.
    """
    seen = check_state or state
    plan = state.plan
    rationale = str(proposal.get("rationale") or "").strip()
    current_version = plan.version if plan is not None else 0

    def reject(reason: str, changes: dict[str, dict[str, Any]] | None = None) -> ProposalOutcome:
        revision = PlanRevision(
            version=current_version,
            accepted=False,
            provenance="agent_proposed",
            agent=agent,
            changes=changes or {},
            rationale=rationale,
            reason=reason,
            lap=seen.player.lap,
            session_time=seen.session_time,
            state_version=seen.version,
        )
        if plan is not None:
            # The log shows it (the live view's revision history); no element and
            # not the version change, and agents are never shown the log.
            plan.revisions = [*plan.revisions, revision][-MAX_REVISIONS:]
        return ProposalOutcome(revision, proposal)

    if plan is None:
        why = (
            "this is not a race segment"
            if not is_race_segment(state)
            else "; ".join(
                sorted(set(derive(seen, None, pit_lane_loss_s=pit_lane_loss_s)[1].values()))
            )
            or "the data does not support one yet"
        )
        return reject(f"No race plan yet: {why}.")
    values, problem = _coerce(proposal)
    if problem:
        return reject(problem)
    values = {k: v for k, v in values.items() if v != _value(plan, k)}
    if not values:
        return reject("The proposal changes nothing in the current plan.")
    changes = {k: {"from": _value(plan, k), "to": v} for k, v in values.items()}
    if not rationale:
        return reject("A proposal needs a rationale.", changes)
    facts = proposal_facts(
        seen,
        events,
        stop_lap=values.get("next_stop_lap", _value(plan, "next_stop_lap")),
        pit_lane_loss_s=pit_lane_loss_s,
    )
    problem = _unsupported(facts, values)
    if problem:
        return reject(problem, changes)
    problem = sanity_check(
        seen, values, base=plan, pit_lane_loss_s=pit_lane_loss_s, events=events
    )
    if problem:
        return reject(problem, changes)

    version = plan.version + 1
    new = plan.model_copy(deep=True)
    lap = seen.player.lap
    for name, value in values.items():
        setattr(
            new,
            name,
            PlanElement(
                value=value,
                unit=ELEMENTS[name],
                provenance="agent_proposed",
                rationale=rationale,
                version=version,
                set_by=agent,
                set_at_lap=lap,
            ),
        )
        new.missing.pop(name, None)
    if "next_stop_lap" in values and "fuel_to_add_l" not in values:
        # The refuel follows the lap: re-price it for the new stop, as derived.
        outcome = sm.simulate_pit_outcome(
            seen, stop_lap=values["next_stop_lap"], pit_lane_loss_s=pit_lane_loss_s
        )
        old = _value(new, "fuel_to_add_l")
        if outcome.fuel_to_add_l is not None and outcome.fuel_to_add_l != old:
            new.fuel_to_add_l = _element(
                outcome.fuel_to_add_l,
                "fuel_to_add_l",
                f"Refuel for a stop on lap {values['next_stop_lap']}, from simulate_pit_outcome.",
                version,
                lap,
            )
            changes["fuel_to_add_l"] = {"from": old, "to": outcome.fuel_to_add_l}
    new.version = version
    revision = PlanRevision(
        version=version,
        accepted=True,
        provenance="agent_proposed",
        agent=agent,
        changes=changes,
        rationale=rationale,
        lap=lap,
        session_time=seen.session_time,
        state_version=seen.version,
    )
    new.revisions = [*new.revisions, revision][-MAX_REVISIONS:]
    state.plan = new
    return ProposalOutcome(revision, proposal)


# --------------------------------------------------------------------------
# what agents see
# --------------------------------------------------------------------------
#: scope -> the elements an agent with that scope is shown.
VIEW_SCOPES: dict[str, tuple[str, ...]] = {
    "full": tuple(ELEMENTS),
    # The vehicle engineer and the coach: how the stint is to be driven, not the stop.
    "posture": ("tyre_posture", "pace_target_s"),
}


def plan_view(state: RaceState, scope: str = "full") -> dict[str, Any]:
    """The plan as an agent sees it: values, provenance and version, no free text.

    Values sit under their element names, whose suffixes carry the unit, so the typed
    FactSet reads ``race_state.plan.next_stop_lap`` as a lap number and
    ``race_state.plan.pace_target_s`` as seconds. Rationales are left out on purpose:
    an agent-written rationale could carry a figure, and the plan must not turn one
    into a fact.
    """
    names = VIEW_SCOPES.get(scope, VIEW_SCOPES["full"])
    plan = state.plan
    if plan is None:
        why = (
            "Not a race segment: the race plan exists only in races."
            if not is_race_segment(state)
            else "The data does not support a plan yet (no fuel model, no clean laps)."
        )
        return {"available": False, "unavailable": why}
    out: dict[str, Any] = {"available": True, "version": plan.version}
    provenance: dict[str, dict[str, Any]] = {}
    missing: dict[str, str] = {}
    for name in names:
        element = getattr(plan, name)
        out[name] = element.value if element is not None else None
        if element is not None:
            provenance[name] = {
                "source": element.provenance,
                "version": element.version,
                "set_by": element.set_by,
            }
        elif name in plan.missing:
            missing[name] = plan.missing[name]
    if scope == "full":
        out["stops_done"] = plan.stops_done
    out["provenance"] = provenance
    if missing:
        out["missing"] = missing
    return out
