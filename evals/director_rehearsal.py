"""Eval-only: rehearse a scripted race (a :class:`ScenarioScript`) against the agents.

The race director makes things happen that did not happen, so it runs only here, never
in the app: over the synthetic race (:data:`DIRECTOR_RACE`, generated, test data) or a
recorded session read from the store (``store_source``, read-only), with its own journal
written to an eval output directory outside ``data/sessions``. ``evals/run_director.py``
is the command line; tests call :func:`rehearse` directly.

How a rehearsal runs. Frames go into a :class:`RaceStateEngine` one at a time and, after
each, a :class:`~rtv.director.ScriptedDirector` is polled through the orchestrator's own
``poll_director`` (so an injected event goes onto the race bus like a detected one).
Every event on the bus is then routed the way the app's pump routes it
(``orch.dispatch``): template calls go on the channel at once and the agents it wakes go
to their one-slot inboxes (latest wins, a critical event or a question holds its slot).
The radio is pumped on the session clock (``RadioFeed.pump``) *before* the agents run,
so a template airs on its own tick as it does in the app, where the model calls are
still in flight; it never queues behind a model answer to the same tick and goes stale.
The agents then run **inline** (``orch.drain_inboxes``): the race clock waits for them.
That makes a rehearsal deterministic (the same script over the same race gives a
byte-identical event log) and independent of replay speed and model latency: ``speed``
only paces frames against the wall clock, and every trigger is in session seconds.

Offline the agents answer from :func:`rehearsal_answer`, a scripted provider that reads
the trigger and the injected overlay and answers like a careful agent would (the
strategist proposes the plan revision a mandatory stop needs); ``live=True`` uses Kimi.

The report (:class:`Rehearsal`) says, per injection: when it fired and against which
condition, which agents and which deterministic callers it woke and whether that is what
the same event would wake had the sim thrown it, what aired, the plan revisions it led
to, and the checks below. A replayed ``.ibt`` has no CarIdx channels, so a traffic
injection there has no field behind it: the report says so instead of inventing one.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.synthetic_race import ScenarioSpec, scenario_source
from rtv.director import (
    DIRECTOR_ID_KEY,
    DIRECTOR_KIND_KEY,
    INJECTED_KEY,
    ScenarioScript,
    ScriptedDirector,
    ScriptedInjection,
    is_injected,
)
from rtv.pitwall.activity import ActivityLog
from rtv.pitwall.callers import build_callers
from rtv.pitwall.orchestrator import PitwallOrchestrator
from rtv.pitwall.provider import ProviderResponse, ScriptedProvider, tool_use_turn
from rtv.pitwall.radio import RadioFeed
from rtv.racestate import RaceStateEngine
from rtv.racestate.models import EventType, RaceEvent, RaceState
from rtv.racestate.replay import ReplaySource
from rtv.store.journal import SessionRecorder

#: The shipped worked script.
EXAMPLE_SCRIPT = Path(__file__).resolve().parents[1] / "docs" / "director_scenario.example.json"
#: Race control's scenario (stage 09 part A): an injected approach to the incident limit.
INCIDENT_LIMIT_SCRIPT = (
    Path(__file__).resolve().parents[1] / "docs" / "director_incident_limit.example.json"
)
#: Scenario names ``evals/run_director.py --scenario`` accepts besides a path.
SCENARIOS: dict[str, Path] = {"example": EXAMPLE_SCRIPT, "incident_limit": INCIDENT_LIMIT_SCRIPT}

#: The synthetic race a script is rehearsed over by default. Generated test data, like
#: every ``evals/synthetic_*`` race: a 12-lap race of 60 s laps (10 Hz keeps it quick),
#: no real caution (so every yellow in a rehearsal is the script's), a tank that reaches
#: the flag (so a scripted mandatory stop is the only reason to stop), and a race
#: session, so the race plan exists. Telemetry runs to lap 9, past the example script's
#: last entry at 420 s.
DIRECTOR_RACE = ScenarioSpec(
    hz=10,
    ticks_per_lap=600,
    laps=9,
    end_pct=0.2,
    race_laps=12,
    yellow_lap=99,
    fuel_start=4.0,
    fuel_capacity=4.0,
    fuel_per_lap=0.3,
    pit_entry_lap=99,
    pit_exit_lap=99,
    session_num=0,
    sessions=("Race",),
)

#: The source label on the journal's lifecycle start record.
EVAL_SOURCE = "eval"
PROVENANCE_KEYS = (INJECTED_KEY, DIRECTOR_KIND_KEY, DIRECTOR_ID_KEY)


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------
@dataclass
class InjectionReport:
    """What one script entry did."""

    id: str
    kind: str
    event_type: str
    condition: str
    fired: bool = False
    event_key: str | None = None
    session_time: float | None = None
    lap: int | None = None
    tick: int | None = None
    #: Session seconds after the ``after`` entry fired (chained entries only).
    interval_s: float | None = None
    woke: list[str] = field(default_factory=list)
    templated: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    blocked: str | None = None
    #: What the same event would wake had the sim thrown it (no provenance keys).
    as_real: list[str] = field(default_factory=list)
    aired: list[dict[str, Any]] = field(default_factory=list)
    plan_revisions: list[dict[str, Any]] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Rehearsal:
    """A finished rehearsal: the report, the logs and where the journal went."""

    scenario: str
    source: str
    session_id: str
    live: bool
    frames: int
    injections: list[InjectionReport]
    #: Every race event, as ``RaceEvent.to_api()`` (no wall clock: replay-stable).
    events: list[dict[str, Any]]
    #: Every radio message the channel aired or dropped, in order.
    radio: list[dict[str, Any]]
    journal: Path | None
    director: dict[str, Any]
    standings: bool

    @property
    def ok(self) -> bool:
        return all(all(r.checks.values()) for r in self.injections if r.fired) and all(
            r.fired for r in self.injections
        )

    def event_log(self) -> str:
        """The event log as JSON lines: what the determinism check compares."""
        return "".join(json.dumps(e, sort_keys=True, default=str) + "\n" for e in self.events)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "source": self.source,
            "session_id": self.session_id,
            "live": self.live,
            "frames": self.frames,
            "standings": self.standings,
            "ok": self.ok,
            "journal": str(self.journal) if self.journal else None,
            "director": self.director,
            "injections": [r.to_dict() for r in self.injections],
        }

    def markdown(self) -> str:
        lines = [
            f"# Director rehearsal: {self.scenario}",
            "",
            f"Source: {self.source} (session `{self.session_id}`), "
            f"{'Kimi (live)' if self.live else 'scripted provider (offline)'}, "
            f"{self.frames} frames. Scripted events are rehearsal data, not race data.",
            "",
        ]
        if not self.standings:
            lines += [
                "This recording has no CarIdx channels (a replayed .ibt), so there is no "
                "field: a traffic injection names a car nobody can see.",
                "",
            ]
        lines += [
            "| entry | fired | condition | woke | templates | as real | aired | checks |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in self.injections:
            when = (
                f"{r.session_time:.1f} s, lap {r.lap}" if r.fired and r.session_time is not None
                else "never"
            )
            aired = "; ".join(f"{a['agent']}: {a['spoken_text']}" for a in r.aired) or "-"
            checks = ", ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in r.checks.items())
            lines.append(
                f"| {r.id} | {when} | {r.condition} | {', '.join(r.woke) or '-'} | "
                f"{', '.join(r.templated) or '-'} | {', '.join(r.as_real) or '-'} | "
                f"{aired} | {checks} |"
            )
        notes = [(r.id, n) for r in self.injections for n in r.notes]
        if notes:
            lines += ["", "Notes:", ""] + [f"- {i}: {n}" for i, n in notes]
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# the offline provider
# --------------------------------------------------------------------------
_TRIGGER_RE = re.compile(r"TRIGGER: (\S+)")


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text", "")) for b in content if isinstance(b, Mapping)
        )
    return ""


def _slice_of(system: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    for block in system:
        text = str(block.get("text", ""))
        if text.startswith("RACE STATE:\n"):
            try:
                return json.loads(text[len("RACE STATE:\n") :])
            except ValueError:
                return {}
    return {}


def _injected_entry(slice_: Mapping[str, Any], event_type: str) -> Mapping[str, Any] | None:
    for entry in slice_.get("injected") or ():
        if entry.get("event_type") == event_type:
            return entry
    return None


def rehearsal_answer(
    *, agent: str, system: Sequence[Mapping[str, Any]], messages: Sequence[Mapping[str, Any]],
    turn: int, **_: Any,
) -> ProviderResponse | dict[str, Any]:
    """The offline stand-in for the model: careful, number-light, grounded answers.

    It reads the trigger and the state slice it was given, so a rehearsal exercises the
    real routing, tools, validator and policies without a key. The strategist, woken by
    a mandatory-stop regulation, first proposes the plan revision through
    ``propose_plan_revision`` (the stop on the regulation's last lap), then calls it.
    """
    first = _text_of(messages[0].get("content")) if messages else ""
    match = _TRIGGER_RE.search(first)
    key = match.group(1) if match else ""
    slice_ = _slice_of(system)
    regulation = _injected_entry(slice_, EventType.REGULATION_CHANGE.value)
    rule = (regulation or {}).get("payload") or {}
    stop_lap = rule.get("applies_to_lap") if not (regulation or {}).get("served") else None

    if agent == "strategist":
        if key.startswith("regulation_change") and stop_lap is not None and turn == 0:
            return tool_use_turn(
                [
                    (
                        "propose_plan_revision",
                        {
                            "next_stop_lap": stop_lap,
                            "rationale": "A regulation makes the stop mandatory.",
                        },
                    )
                ]
            )
        if stop_lap is not None:
            return {
                "priority": "advisory",
                "spoken_text": f"The stop is mandatory now, plan to box lap {stop_lap}.",
                "detail_text": "The scripted regulation requires a stop inside its window.",
                "recommendation": "plan_stop",
                "target_lap": stop_lap,
                "confidence": "medium",
                "rationale": "The injected regulation sets the window; the fuel needs none.",
            }
        no_stop = (slice_.get("stop_plan") or {}).get("call") == "no_stop_needed"
        if key.startswith("flag_change:yellow") and no_stop:
            return {
                "priority": "advisory",
                "spoken_text": "Full course yellow. The tank reaches the flag, we stay out.",
                "detail_text": "Caution out; the stop plan needs no stop.",
                "recommendation": "stay_out",
                "confidence": "medium",
                "rationale": "stop_plan says no_stop_needed.",
            }
        if key.startswith("flag_change:yellow"):
            return {
                "priority": "advisory",
                "spoken_text": "Full course yellow. Holding the plan until the numbers move.",
                "detail_text": "Caution out; the stop stays where the plan has it.",
                "recommendation": "hold",
                "confidence": "low",
                "rationale": "The offline rehearsal does not price a caution stop.",
            }
        return {
            "priority": "info",
            "spoken_text": "Nothing changes the plan, we hold.",
            "detail_text": "No change to the stop plan.",
            "recommendation": "hold",
            "confidence": "medium",
            "rationale": "No new data moves the stop.",
        }
    if agent == "vehicle_engineer":
        if key.startswith("weather_change"):
            return {
                "priority": "advisory",
                "spoken_text": "Rain on the way. Keep heat in the fronts, gentle on the kerbs.",
                "detail_text": "The scripted weather step brings water; grip will drop.",
                "finding": "tyre_temps",
                "trend": "unknown",
                "confidence": "medium",
                "rationale": "From the injected weather condition only.",
            }
        return {
            "priority": "info",
            "spoken_text": "Car is fine.",
            "detail_text": "Nothing out of band.",
            "finding": "none",
            "confidence": "medium",
            "rationale": "No warning in the car health data.",
        }
    if agent == "race_control":
        figures = {
            name: int(m.group(1))
            for name in ("incidents", "incident_limit")
            if (m := re.search(rf'"{name}":(\d+)', first))
        }
        if key.startswith("incident_limit") and len(figures) == 2:
            return {
                "priority": "critical",
                "spoken_text": "One more heavy contact ends our race. Give everyone room.",
                "detail_text": "The incident count is one heavy contact from the session's limit.",
                "consequence": "incident_limit",
                "incidents": figures["incidents"],
                "incident_limit": figures["incident_limit"],
                "driver_action": "Give everyone room.",
                "confidence": "high",
            }
        return {
            "priority": "info",
            "spoken_text": "Nothing for race control.",
            "detail_text": "No consequence in the race-control data.",
            "consequence": "none",
            "confidence": "medium",
        }
    if agent == "spotter":
        return {
            "priority": "info",
            "spoken_text": "Clear around you.",
            "detail_text": "No car close.",
            "threat": "clear",
            "side": "unknown",
            "action": "none",
            "confidence": "medium",
        }
    return {
        "priority": "info",
        "spoken_text": "Keep it tidy.",
        "detail_text": "No pattern to work on.",
        "theme": "consistency",
        "cue": "Keep it tidy.",
        "from_telemetry": False,
        "confidence": "low",
        "rationale": "Nothing in the findings.",
    }


# --------------------------------------------------------------------------
# the rehearsal
# --------------------------------------------------------------------------
def journal_root_allowed(out_dir: Path, journal_dir: Path | None) -> bool:
    """False when ``out_dir`` is (inside) the app's ``data/sessions``: never write there."""
    if journal_dir is None:
        return True
    out, sessions = Path(out_dir).resolve(), Path(journal_dir).resolve()
    return out != sessions and sessions not in out.parents


def _app_journal_dir() -> Path | None:
    try:
        from rtv.config import get_settings

        return get_settings().journal_dir
    except Exception:  # pragma: no cover - settings are always readable in a checkout
        return None


def build_rehearsal(
    script: ScenarioScript,
    *,
    provider: Any = None,
    agents: Sequence[Any] | None = None,
    activity: ActivityLog | None = None,
) -> tuple[RaceStateEngine, PitwallOrchestrator, ScriptedDirector]:
    """An engine, an orchestrator and the scripted director bound to ``script``."""
    from rtv.pitwall.agents import AGENT_REGISTRY

    engine = RaceStateEngine(source="replay")
    director = ScriptedDirector(script)
    orch = PitwallOrchestrator(
        engine,
        provider if provider is not None else ScriptedProvider(rehearsal_answer),
        list(AGENT_REGISTRY if agents is None else agents),
        feed=RadioFeed(),
        clock=None,
        director=director,
        activity=activity,
        callers=build_callers(),
        retry_backoff_s=0.0,
    )
    return engine, orch, director


async def rehearse(
    script: ScenarioScript,
    source: ReplaySource | None = None,
    *,
    out_dir: Path | None = None,
    live: bool = False,
    provider: Any = None,
    agents: Sequence[Any] | None = None,
    speed: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.perf_counter,
    source_label: str = "synthetic",
    on_fire: Callable[[RaceEvent, float], None] | None = None,
) -> Rehearsal:
    """Run ``script`` over ``source`` (default: :data:`DIRECTOR_RACE`) and report.

    ``out_dir`` gets the journal (``<out_dir>/<session_id>/runs/*-eval.jsonl``), refused
    under the app's ``data/sessions``. ``speed`` > 0 paces frames at that multiple of
    real time with ``sleep`` / ``clock`` (injectable for tests); 0 runs flat out. Either
    way agents run inline after the tick's templates have aired, and every trigger is in
    session seconds. ``on_fire`` is told
    each injected event and the ``clock`` reading when it fired.
    """
    if live and provider is None:
        from rtv.pitwall.provider import KimiProvider

        provider = KimiProvider()
    source = source if source is not None else scenario_source(DIRECTOR_RACE)

    activity = ActivityLog(clock=None)
    engine, orch, director = build_rehearsal(
        script, provider=provider, agents=agents, activity=activity
    )
    activity._clock = lambda: (engine.state.session_time, engine.state.tick)  # noqa: SLF001
    orch.open_inboxes()

    events: list[RaceEvent] = []
    pending: list[RaceEvent] = []
    dispatches: list[dict[str, Any]] = []
    radio: list[dict[str, Any]] = []
    engine.bus.subscribe_callback(events.append)
    engine.bus.subscribe_callback(pending.append)
    activity.subscribe_callback(
        lambda r: dispatches.append(r) if r.get("kind") == "dispatch" else None
    )
    orch.feed.subscribe_callback(lambda m: radio.append(m.model_dump(mode="json")))

    recorder: SessionRecorder | None = None
    journal_path: Path | None = None
    if out_dir is not None:
        if not journal_root_allowed(out_dir, _app_journal_dir()):
            raise ValueError(
                f"{out_dir} is the app's journal folder; a rehearsal never writes there."
            )
        recorder = SessionRecorder(Path(out_dir), snapshot=engine.snapshot)
        engine.bus.subscribe_callback(recorder.on_race_event)
        activity.subscribe_callback(recorder.on_activity)
        orch.feed.subscribe_callback(recorder.on_radio)

    engine.bind_catalog(source.catalog, session_id=source.session_id)
    engine.set_session_info(source.session_info)
    engine.state.session.source = "replay"
    if recorder is not None:
        recorder.begin(
            source.session_id,
            EVAL_SOURCE,
            scenario=script.name,
            director=director.name,
            rehearsal_source=source_label,
            live=live,
        )
        journal_path = Path(recorder.active_run()["path"])

    fired_at: dict[str, RaceEvent] = {}
    reports = {
        i.id: InjectionReport(
            id=i.id, kind=i.kind.value, event_type=i.event_type.value,
            condition=i.when.describe(),
        )
        for i in script.injections
    }
    frames = 0
    max_gap = 0.0
    last_t: float | None = None
    t0: float | None = None
    started = clock()
    for frame in source.frames:
        if speed and speed > 0:
            t0 = frame.session_time if t0 is None else t0
            behind = (frame.session_time - t0) / speed - (clock() - started)
            if behind > 0:
                sleep(behind)
        engine.on_frame(frame, source.catalog)
        frames += 1
        if last_t is not None:
            max_gap = max(max_gap, frame.session_time - last_t)
        last_t = frame.session_time
        state = engine.snapshot()
        if recorder is not None:
            recorder.on_frame(state.session_time, state.tick, state.player.lap)
        for event in orch.poll_director(state):
            entry = script.injection(str(event.payload.get(DIRECTOR_ID_KEY)))
            fired_at[entry.id] = event
            report = reports[entry.id]
            _fill_fired(report, entry, event, fired_at, state)
            report.as_real = _as_real(engine, orch, event, state)
            if on_fire is not None:
                on_fire(event, clock())
        i = 0
        while i < len(pending):
            # As the app's pump does: route everything queued so far. Template calls go
            # on the channel now; agent runs go to the agents' one-slot inboxes.
            while i < len(pending):
                event = pending[i]
                i += 1
                before = len(dispatches)
                orch.dispatch(event, engine.snapshot())
                if is_injected(event):
                    _fill_dispatch(reports, event, dispatches[before:])
            # The channel airs what it can before any model answers: in the app the
            # calls are still in flight, so a same-tick template never waits on one.
            orch.feed.pump(state.session_time)
            # Then the agents, inline (the race clock waits for them: deterministic).
            # Their answers queue behind what already aired; any event they cause
            # (a plan revision) is routed on the next pass of this loop.
            await orch.drain_inboxes()
        pending.clear()
        orch.feed.pump(state.session_time)
    orch.feed.flush()
    final = engine.snapshot()
    if recorder is not None:
        recorder.end(reason="rehearsal_finished", scenario=script.name)

    rehearsal = Rehearsal(
        scenario=script.name,
        source=source_label,
        session_id=source.session_id,
        live=live,
        frames=frames,
        injections=list(reports.values()),
        events=[e.to_api() for e in events],
        radio=radio,
        journal=journal_path,
        director=director.describe(),
        standings=bool(final.capabilities.standings),
    )
    _score(rehearsal, script, fired_at, events, max_gap)
    return rehearsal


def _fill_dispatch(
    reports: Mapping[str, InjectionReport], event: RaceEvent, records: Sequence[Mapping[str, Any]]
) -> None:
    report = reports[str(event.payload.get(DIRECTOR_ID_KEY))]
    for record in records:
        data = record.get("data") or {}
        if record.get("event_key") == event.key and record.get("tick") == event.tick:
            report.woke = list(data.get("accepted") or [])
            report.templated = list(data.get("templated") or [])
            report.skipped = dict(data.get("skipped") or {})
            report.blocked = data.get("blocked")
            return


def _fill_fired(
    report: InjectionReport,
    entry: ScriptedInjection,
    event: RaceEvent,
    fired_at: Mapping[str, RaceEvent],
    state: RaceState,
) -> None:
    report.fired = True
    report.event_key = event.key
    report.session_time = event.session_time
    report.lap = event.lap
    report.tick = event.tick
    parent = entry.when.after
    if parent is not None and parent in fired_at:
        report.interval_s = round(event.session_time - fired_at[parent].session_time, 6)


def _as_real(
    engine: RaceStateEngine, orch: PitwallOrchestrator, event: RaceEvent, state: RaceState
) -> list[str]:
    """Who the same event wakes when the sim throws it: a fresh orchestrator, no provenance."""
    trace: list[dict[str, Any]] = []
    log = ActivityLog(clock=None)
    log.subscribe_callback(trace.append)
    shadow = PitwallOrchestrator(
        engine,
        None,
        [rt.spec for rt in orch.runtimes.values()],
        feed=RadioFeed(),
        clock=None,
        callers=build_callers(),
        activity=log,
    )
    real = event.model_copy(
        update={"payload": {k: v for k, v in event.payload.items() if k not in PROVENANCE_KEYS}}
    )
    accepted = shadow.dispatch(real, state)
    templated: list[str] = []
    for record in trace:
        if record.get("kind") == "dispatch":
            templated = list((record.get("data") or {}).get("templated") or [])
    return sorted(set(accepted) | set(templated))


def _score(
    rehearsal: Rehearsal,
    script: ScenarioScript,
    fired_at: Mapping[str, RaceEvent],
    events: Sequence[RaceEvent],
    max_gap: float,
) -> None:
    """The per-injection eval checks (all deterministic)."""
    tolerance = max(max_gap, 1e-6) + 1e-6
    for report in rehearsal.injections:
        entry = script.injection(report.id)
        if not report.fired:
            report.notes.append("never fired: its condition did not hold before the data ended")
            continue
        event = fired_at[report.id]
        key, tick = event.key, event.tick
        report.aired = [
            {
                "agent": m["agent"],
                "source": m.get("source"),
                "spoken_text": m["spoken_text"],
                "refused": m.get("refused"),
                "late": m.get("late"),
                "expired": m.get("expired"),
            }
            for m in rehearsal.radio
            if (m.get("event_ref") or {}).get("key") == key
            and (m.get("event_ref") or {}).get("tick") == tick
        ]
        checks = report.checks
        checks["provenance_ok"] = all(k in event.payload for k in PROVENANCE_KEYS) and (
            event.payload.get(DIRECTOR_ID_KEY) == entry.id
        )
        checks["condition_ok"] = _condition_held(entry, report, tolerance)
        checks["woke_as_real_ok"] = sorted(set(report.woke) | set(report.templated)) == (
            report.as_real
        )
        heard = [a for a in report.aired if not a["refused"] and not a.get("expired")]
        checks["radio_ok"] = bool(heard) or not report.as_real
        if entry.event_type is EventType.REGULATION_CHANGE:
            report.plan_revisions = [
                e.payload
                for e in events
                if e.event_type is EventType.PLAN_REVISED
                and e.session_time >= event.session_time
                and e.payload.get("provenance") == "agent_proposed"
            ]
            checks["plan_revised_ok"] = _plan_inside_window(event, report.plan_revisions)
        if entry.event_type is EventType.WEATHER_CHANGE:
            woke = set(report.woke)
            checks["weather_wakes_ok"] = {"strategist", "vehicle_engineer"} <= woke
        if entry.event_type is EventType.INCIDENT:
            # The read-out is race control's template, never a model call.
            checks["template_ok"] = "race_control" in report.templated and (
                "race_control" not in report.woke
            )
        if entry.event_type is EventType.INCIDENT_LIMIT:
            woken = event.payload.get("stage") == "final"
            calls = [a for a in heard if a["agent"] == "race_control"]
            checks["race_control_ok"] = (
                ("race_control" in report.woke) is woken and bool(calls) is woken
            )
        if entry.event_type in (EventType.TRAFFIC_CLOSE, EventType.BLUE_FLAG):
            checks["template_ok"] = "spotter" in report.templated and (
                "spotter" not in report.woke
            )
            if not rehearsal.standings:
                report.notes.append(
                    "no standings in this recording (no CarIdx channels): the scripted car "
                    f"{event.payload.get('car_idx')} is not in any field"
                )


def _condition_held(entry: ScriptedInjection, report: InjectionReport, tol: float) -> bool:
    when = entry.when
    t, lap = report.session_time or 0.0, report.lap
    if when.at_session_time is not None and not (
        when.at_session_time <= t <= when.at_session_time + tol
        or when.while_flag is not None or when.at_lap is not None
    ):
        return False
    if when.at_lap is not None and (lap is None or lap < when.at_lap):
        return False
    if when.after is not None:
        if report.interval_s is None or report.interval_s < when.delay_s - 1e-6:
            return False
        if when.while_flag is None and report.interval_s > when.delay_s + tol:
            return False
    return True


def _plan_inside_window(event: RaceEvent, revisions: Sequence[Mapping[str, Any]]) -> bool:
    from rtv.racestate.plan import regulation_stop

    rule = regulation_stop(event.payload, event.session_time)
    if rule is None:
        return bool(revisions)
    for rev in revisions:
        change = (rev.get("changes") or {}).get("next_stop_lap") or {}
        to = change.get("to")
        if isinstance(to, int | float) and rule.allows(int(to)):
            return True
    return False


def write_outputs(rehearsal: Rehearsal, out_dir: Path) -> dict[str, Path]:
    """report.json, REPORT.md, events.jsonl and radio.jsonl in ``out_dir``."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "report": out_dir / "report.json",
        "markdown": out_dir / "REPORT.md",
        "events": out_dir / "events.jsonl",
        "radio": out_dir / "radio.jsonl",
    }
    paths["report"].write_text(
        json.dumps(rehearsal.to_dict(), indent=1, default=str) + "\n", encoding="utf-8"
    )
    paths["markdown"].write_text(rehearsal.markdown(), encoding="utf-8")
    paths["events"].write_text(rehearsal.event_log(), encoding="utf-8")
    paths["radio"].write_text(
        "".join(json.dumps(m, default=str) + "\n" for m in rehearsal.radio), encoding="utf-8"
    )
    return paths
