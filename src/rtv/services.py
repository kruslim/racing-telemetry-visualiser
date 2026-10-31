"""Application service container: wires ingest + store + stream together."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from rtv.coaching.features import CoachingService
from rtv.config import Settings
from rtv.director import NoopDirector, ScenarioScript
from rtv.domain.models import ConnectionState
from rtv.ingest.frame import Frame
from rtv.ingest.ibt import import_ibt
from rtv.ingest.live import LivePoller
from rtv.logging import get_logger
from rtv.pitwall.activity import ActivityLog
from rtv.pitwall.orchestrator import PitwallOrchestrator
from rtv.pitwall.radio import RadioFeed
from rtv.pitwall.tts import RadioTTS, build_radio_tts
from rtv.racestate.detectors import DEFAULT_CONFIG
from rtv.racestate.engine import RaceStateEngine
from rtv.racestate.replay import ReplayDriver
from rtv.store.duck import Database
from rtv.store.journal import SessionRecorder
from rtv.store.repository import Repository
from rtv.store.writer import TelemetryWriter
from rtv.stream.hub import LiveHub
from rtv.vision.service import VisionService

log = get_logger("services")


class ImportJobManager:
    """Runs .ibt imports on background threads, tracking progress in DuckDB."""

    def __init__(self, db: Database, writer: TelemetryWriter, settings: Settings) -> None:
        self._db = db
        self._writer = writer
        self._settings = settings

    def start(self, path: str, label: str | None = None) -> str:
        job_id = str(uuid.uuid4())
        now = datetime.now(tz=UTC)
        self._db.execute(
            "INSERT INTO import_jobs (job_id, source_path, status, progress, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?)",
            [job_id, path, "pending", 0.0, now, now],
        )
        threading.Thread(
            target=self._run, args=(job_id, path, label),
            name=f"rtv-import-{job_id[:8]}", daemon=True,
        ).start()
        return job_id

    def get(self, job_id: str) -> dict | None:
        return self._db.query_one(
            "SELECT * FROM import_jobs WHERE job_id = ?", [job_id]
        )

    def _run(self, job_id: str, path: str, label: str | None = None) -> None:
        self._update(job_id, status="running")
        try:
            if not Path(path).exists():
                raise FileNotFoundError(path)

            def progress(p: float) -> None:
                self._update(job_id, progress=round(p, 4))

            session_id = import_ibt(
                path,
                self._writer,
                flatten_max=self._settings.array_flatten_max,
                chunk_rows=self._settings.ibt_chunk_rows,
                progress=progress,
                label=label,
            )
            self._update(
                job_id, status="complete", progress=1.0, session_id=session_id
            )
        except Exception as exc:  # pragma: no cover - surfaced to client
            log.exception("Import failed for %s", path)
            self._update(job_id, status="error", error=str(exc))

    def _update(self, job_id: str, **fields) -> None:
        fields["updated_at"] = datetime.now(tz=UTC)
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._db.execute(
            f"UPDATE import_jobs SET {sets} WHERE job_id = ?",
            [*fields.values(), job_id],
        )


@dataclass
class AppServices:
    settings: Settings
    db: Database
    writer: TelemetryWriter
    repo: Repository
    coaching: CoachingService
    hub: LiveHub
    poller: LivePoller
    imports: ImportJobManager
    #: v2 pitwall; both are None when RTV_PITWALL is false.
    engine: RaceStateEngine | None = None
    replay: ReplayDriver | None = None
    #: v2 pitwall agent layer; None when RTV_PITWALL_AGENTS is false or the
    #: ``ai`` extra / LLM key is absent (see build_services).
    pitwall: PitwallOrchestrator | None = None
    #: v2 radio voice. Always present, usually a NullProvider: with no backend
    #: provider configured the browser's Web Speech API does the talking, so this
    #: object's job is mostly to explain that on /api/v1/pitwall/tts.
    tts: RadioTTS | None = None
    #: v2 activity trace of the agent layer (routing, model and tool calls,
    #: validator verdicts, radio transitions). Present whenever the engine is.
    activity: ActivityLog | None = None
    #: The race journal writer; None when RTV_PITWALL is false (no engine to record).
    recorder: SessionRecorder | None = None
    #: Onboard video and VLM visual notes (see docs/VISION.md). Always present;
    #: it makes no VLM call unless RTV_VISION_MODE allows it.
    vision: VisionService | None = None

    def close(self) -> None:
        try:
            if self.vision is not None:
                self.vision.close()
            if self.replay is not None:
                self.replay.stop()
            if self.recorder is not None:
                self.recorder.end(reason="shutdown")
        finally:
            try:
                self.poller.stop()
            finally:
                self.db.close()


def build_services(settings: Settings) -> AppServices:
    settings.ensure_dirs()
    db = Database(settings.duckdb_path)
    writer = TelemetryWriter(db, settings.parquet_dir)
    repo = Repository(db, settings.parquet_dir)
    coaching = CoachingService(repo)
    hub = LiveHub()

    engine: RaceStateEngine | None = None
    replay: ReplayDriver | None = None
    pitwall: PitwallOrchestrator | None = None
    activity: ActivityLog | None = None
    recorder: SessionRecorder | None = None
    tts = build_radio_tts(settings)
    if settings.pitwall:
        engine = RaceStateEngine(
            source="live",
            # The live path must not be able to stop. A fault on one frame is
            # counted in metrics.dropped_frames and swallowed; a pitwall that
            # dies at 250 km/h is worse than one that is missing 16 ms of state.
            # Off by default in the constructor, so tests and replays still fail
            # loudly on a real bug.
            resilient=True,
            gap_interval=settings.pitwall_gap_interval,
            fuel_laps=settings.pitwall_fuel_laps,
            config=replace(
                DEFAULT_CONFIG,
                stint_milestone_laps=settings.pitwall_stint_milestone_laps,
                fuel_margin_laps=settings.pitwall_fuel_margin_laps,
                corner_buckets=settings.pitwall_corner_buckets,
                recurrence_min=settings.pitwall_recurrence_min,
                recurrence_window_laps=settings.pitwall_recurrence_window_laps,
                tyre_temp_trend_c_per_lap=settings.pitwall_tyre_temp_trend_c,
                tyre_axle_imbalance_c=settings.pitwall_tyre_axle_imbalance_c,
                oil_temp_max_c=settings.pitwall_oil_temp_max_c,
                water_temp_max_c=settings.pitwall_water_temp_max_c,
                traffic_gap_s=settings.pitwall_traffic_gap_s,
            ),
        )
        # A replay goes through the same fan-out as live capture: the engine
        # (and so the agents) plus the /ws/live hub.
        # The replay API refuses to start while the live poller runs, so when a
        # replay ends the feed is simply disconnected again.
        def on_replay(running: bool, session_id: str | None) -> None:
            if running:
                hub.publish_state(ConnectionState.REPLAY, session_id)
                if session_id:
                    _begin_journal(recorder, pitwall, session_id, "replay")
            else:
                hub.publish_state(ConnectionState.DISCONNECTED, None)
                recorder.end(reason="replay_finished")
                if vision.worker is not None and session_id:
                    vision.worker.end_session(session_id, "replay_finished")

        race = engine  # bound for the closure; engine.state is replaced on reset
        activity = ActivityLog(clock=lambda: (race.state.session_time, race.state.tick))

        def on_replay_frame(frame: Frame, catalog) -> None:
            hub.publish_frame(frame, catalog)
            _journal_frame(recorder, engine)

        replay = ReplayDriver(engine, publish=on_replay_frame, on_lifecycle=on_replay)
        if settings.pitwall_agents:
            pitwall = build_pitwall(
                engine, settings, coaching=coaching, repo=repo, tts=tts
            )
            if pitwall is not None:
                pitwall.attach_activity(activity)
        # Every session the engine consumes, live or replayed, is written to the
        # race journal so the post-race review can play it back.
        recorder = SessionRecorder(
            settings.journal_dir,
            state_interval_s=settings.journal_state_interval_s,
            snapshot=engine.snapshot,
        )
        engine.bus.subscribe_callback(recorder.on_race_event)
        activity.subscribe_callback(recorder.on_activity)
        if pitwall is not None:
            pitwall.feed.subscribe_callback(recorder.on_radio)

    vision = VisionService(settings, repo, coaching, engine=engine)
    live_sid: list[str | None] = [None]

    def on_frame(frame: Frame, catalog) -> None:
        hub.publish_frame(frame, catalog)
        # The live capture stamps its frames with this clock (docs/VISION.md, Live).
        vision.observe_frame(frame.session_time, live_sid[0])
        if engine is not None:
            # The engine is built resilient=True, so it counts and swallows its
            # own per-frame faults. This is the outer net for anything that could
            # still escape it: live capture and the v1 /ws/live feed must not be
            # reachable from a race-state bug.
            try:
                engine.on_frame(frame, catalog)
            except Exception:  # pragma: no cover - defensive on the hot path
                log.exception("Race-state update failed at tick %s", frame.tick)
            _journal_frame(recorder, engine)

    def on_state(state: ConnectionState, session_id: str | None) -> None:
        hub.publish_state(state, session_id)
        live_sid[0] = session_id if state is ConnectionState.IN_SESSION else None
        _auto_capture(vision, state, session_id)
        if engine is None:
            return
        # iRacing came or went. The engine has no clock of its own, so a
        # disconnect does not corrupt the race state -- it silently freezes it,
        # which is worse: "P4, 2.1 s behind" reads the same whether it is current
        # or four minutes old. Flag it, and stand the agents down so none of them
        # reasons over a race that has stopped.
        try:
            if recorder is not None:
                active = recorder.active
                if state is ConnectionState.IN_SESSION and session_id:
                    if active is None or active["session_id"] != session_id:
                        _begin_journal(recorder, pitwall, session_id, "live")
                elif active is not None and active["source"] == "live":
                    recorder.end(reason=f"iRacing telemetry {state.value}")
        except Exception:  # pragma: no cover - never break the live feed
            log.exception("Could not update the race journal for %s", state)
        try:
            if state is ConnectionState.IN_SESSION:
                engine.mark_live()
                if pitwall is not None:
                    pitwall.resume()
            else:
                engine.mark_stale(f"iRacing telemetry {state.value}")
                if pitwall is not None:
                    pitwall.suspend(f"iRacing telemetry {state.value}")
        except Exception:  # pragma: no cover - never break the live feed
            log.exception("Could not apply connection state %s", state)
        if not session_id:
            return
        # Name the live session on the engine, and pick the session-info document
        # up out of the store the poller has just written it to. Doing it here
        # rather than adding a callback keeps LivePoller (v1 ingest) untouched,
        # and it is what makes get_setup_snapshot and the Layer-1 half of
        # get_corner_detail work live rather than only in replay.
        try:
            engine.set_session_id(session_id)
            info = repo.get_session_info(session_id)
            if info:
                engine.set_session_info(info)
        except Exception:  # pragma: no cover - never break the live feed
            log.exception("Could not attach session info for %s", session_id)

    poller = LivePoller(
        writer,
        poll_hz=settings.poll_hz,
        flush_seconds=settings.flush_seconds,
        flatten_max=settings.array_flatten_max,
        on_frame=on_frame,
        on_state=on_state,
    )
    imports = ImportJobManager(db, writer, settings)
    return AppServices(
        settings=settings,
        db=db,
        writer=writer,
        repo=repo,
        coaching=coaching,
        hub=hub,
        poller=poller,
        imports=imports,
        engine=engine,
        replay=replay,
        pitwall=pitwall,
        tts=tts,
        activity=activity,
        recorder=recorder,
        vision=vision,
    )


def _auto_capture(vision: VisionService, state: ConnectionState, session_id: str | None) -> None:
    """``RTV_VISION_CAPTURE_AUTO``: capture while a live session is on, stop when it ends."""
    if not vision.settings.vision_capture_auto:
        return
    try:
        cap = vision.capture
        running = cap is not None and cap.running
        if state is ConnectionState.IN_SESSION and session_id:
            if running and cap.session_id != session_id:
                vision.stop_capture()
                running = False
            if not running:
                vision.start_capture(session_id)
        elif running:
            vision.stop_capture()
    except Exception:  # pragma: no cover - never break the live feed
        log.exception("Automatic capture could not follow %s", state)


def _begin_journal(
    recorder: SessionRecorder | None,
    pitwall: PitwallOrchestrator | None,
    session_id: str,
    source: str,
) -> None:
    """Open a journal run, noting which agents (if any) were listening."""
    if recorder is None:
        return
    try:
        agents = []
        if pitwall is not None:
            agents = [
                {"name": a.get("name"), "model": a.get("model"), "enabled": a.get("enabled")}
                for a in pitwall.status().get("agents", [])
            ]
        recorder.begin(
            session_id,
            source,
            agents=agents,
            agents_enabled=bool(pitwall is not None and pitwall.enabled),
        )
    except Exception:  # pragma: no cover - recording must never stop a session
        log.exception("Could not open a journal run for %s", session_id)


def _journal_frame(recorder: SessionRecorder | None, engine: RaceStateEngine) -> None:
    if recorder is None or recorder.active is None:
        return
    try:
        state = engine.state
        recorder.on_frame(state.session_time, state.tick, state.player.lap)
    except Exception:  # pragma: no cover - defensive on the hot path
        log.exception("Race journal frame hook failed")


def build_pitwall(
    engine: RaceStateEngine,
    settings: Settings,
    *,
    coaching: CoachingService | None = None,
    repo: Repository | None = None,
    tts: RadioTTS | None = None,
) -> PitwallOrchestrator | None:
    """Assemble the agent layer, or return None when it cannot run.

    The provider is constructed lazily and defensively: with no ``anthropic`` SDK
    package (the transport for Kimi's endpoint) or no Kimi Code key there is
    nothing to talk to, so the layer stays unmounted rather than failing at the
    first race event. That is what keeps the default test path free of any
    network dependency.
    """
    from rtv.llm import is_configured, key_env_var
    from rtv.pitwall.agents import build_agents

    if not is_configured(settings):
        log.info(
            "Pitwall agents disabled: %s is not set. "
            "The deterministic race-state engine is unaffected.",
            key_env_var(settings),
        )
        return None
    try:
        from rtv.pitwall.provider import KimiProvider

        provider = KimiProvider()
    except Exception:  # pragma: no cover - the ai extra is optional
        log.warning(
            "Pitwall agents disabled: the 'ai' extra is not installed "
            '(pip install -e ".[ai]").'
        )
        return None

    from rtv.llm import model

    only = [n.strip() for n in settings.pitwall_agents_only.split(",") if n.strip()]
    agents = build_agents(
        models={
            "strategist": model("reasoning", settings.pitwall_strategist_model, settings),
            "vehicle_engineer": model(
                "fast", settings.pitwall_vehicle_engineer_model, settings
            ),
            "spotter": model("fast", settings.pitwall_spotter_model, settings),
            "coach": model("fast", settings.pitwall_coach_model, settings),
        },
        only=only or None,
    )
    if not agents:
        log.warning(
            "Pitwall agents disabled: RTV_PITWALL_AGENTS_ONLY=%r matched no agent.",
            settings.pitwall_agents_only,
        )
        return None
    # RTV_PITWALL_AGENT_COOLDOWN_S is the *fallback* for a trigger that names no
    # cooldown of its own; every trigger currently names one, so the per-agent
    # cadences below (45 s engineer, 15 s spotter, 120 s coach) are what apply.
    agents = [replace(spec, cooldown_s=settings.pitwall_agent_cooldown_s) for spec in agents]
    return PitwallOrchestrator(
        engine,
        provider,
        agents,
        feed=RadioFeed(history=settings.pitwall_radio_history),
        max_inflight=settings.pitwall_max_inflight,
        enabled=settings.pitwall_agents_live,
        director=build_director(settings),
        failure_limit=settings.pitwall_agent_failure_limit,
        max_calls_per_session=settings.pitwall_max_calls_per_session,
        retry_backoff_s=settings.pitwall_retry_backoff_s,
        tool_config={
            "pit_lane_loss_s": settings.pitwall_pit_lane_loss_s,
            "standings_window": 3,
            "corner_buckets": settings.pitwall_corner_buckets,
            "oil_temp_max_c": settings.pitwall_oil_temp_max_c,
            "water_temp_max_c": settings.pitwall_water_temp_max_c,
        },
        # The Layer-1 coaching service and its repository, so get_corner_detail
        # reads the *same* deterministic corner analysis the v1 endpoints serve.
        tool_extras={"coaching": coaching, "repo": repo},
        tts=tts,
    )


def build_director(settings: Settings) -> NoopDirector:
    """Load and validate the configured scenario script, then bind it to a no-op.

    The race-director layer is **planned**: stage 5 ships the schema, the
    protocol and this default, and no walker. So what
    ``RTV_PITWALL_DIRECTOR_SCRIPT`` buys today is validation -- point the server
    at a script and a malformed one fails at boot with a line number instead of
    three laps into a demo.

    A bad path is a warning, not a crash. Losing the rehearsal is a smaller
    failure than refusing to start the pitwall, and the reason is reported on
    ``/api/v1/pitwall/status``.
    """
    path = (settings.pitwall_director_script or "").strip()
    if not path:
        return NoopDirector()
    try:
        script = ScenarioScript.load(path)
    except (OSError, ValueError) as exc:
        log.warning("Could not load the director script %s: %s", path, exc)
        return NoopDirector()
    return NoopDirector(script)
