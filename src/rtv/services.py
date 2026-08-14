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
from rtv.pitwall.orchestrator import PitwallOrchestrator
from rtv.pitwall.radio import RadioFeed
from rtv.pitwall.tts import RadioTTS, build_radio_tts
from rtv.racestate.detectors import DEFAULT_CONFIG
from rtv.racestate.engine import RaceStateEngine
from rtv.racestate.replay import ReplayDriver
from rtv.store.duck import Database
from rtv.store.repository import Repository
from rtv.store.writer import TelemetryWriter
from rtv.stream.hub import LiveHub

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
    #: ``ai`` extra / ANTHROPIC_API_KEY is absent (see build_services).
    pitwall: PitwallOrchestrator | None = None
    #: v2 radio voice. Always present, usually a NullProvider: with no backend
    #: provider configured the browser's Web Speech API does the talking, so this
    #: object's job is mostly to explain that on /api/v1/pitwall/tts.
    tts: RadioTTS | None = None

    def close(self) -> None:
        try:
            if self.replay is not None:
                self.replay.stop()
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
        replay = ReplayDriver(engine)
        if settings.pitwall_agents:
            pitwall = build_pitwall(
                engine, settings, coaching=coaching, repo=repo, tts=tts
            )

    def on_frame(frame: Frame, catalog) -> None:
        hub.publish_frame(frame, catalog)
        if engine is not None:
            # The engine is built resilient=True, so it counts and swallows its
            # own per-frame faults. This is the outer net for anything that could
            # still escape it: live capture and the v1 /ws/live feed must not be
            # reachable from a race-state bug.
            try:
                engine.on_frame(frame, catalog)
            except Exception:  # pragma: no cover - defensive on the hot path
                log.exception("Race-state update failed at tick %s", frame.tick)

    def on_state(state: ConnectionState, session_id: str | None) -> None:
        hub.publish_state(state, session_id)
        if engine is None:
            return
        # iRacing came or went. The engine has no clock of its own, so a
        # disconnect does not corrupt the race state -- it silently freezes it,
        # which is worse: "P4, 2.1 s behind" reads the same whether it is current
        # or four minutes old. Flag it, and stand the agents down so none of them
        # reasons over a race that has stopped.
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
    )


def build_pitwall(
    engine: RaceStateEngine,
    settings: Settings,
    *,
    coaching: CoachingService | None = None,
    repo: Repository | None = None,
    tts: RadioTTS | None = None,
) -> PitwallOrchestrator | None:
    """Assemble the agent layer, or return None when it cannot run.

    The provider is constructed lazily and defensively: with no ``anthropic``
    package and no credential for the selected backend there is nothing to talk
    to, so the layer stays unmounted rather than failing at the first race event.
    That is what keeps the default test path free of any network dependency.

    Which backend, which credential env var and which structured-output strategy
    are :mod:`rtv.llm`'s decision, not this function's -- so pointing the whole
    system at Kimi is configuration rather than a code path.
    """
    from rtv.llm import describe, is_configured, key_env_var
    from rtv.pitwall.agents import build_agents

    if not is_configured(settings):
        log.info(
            "Pitwall agents disabled: %s is not set for provider %s. "
            "The deterministic race-state engine is unaffected.",
            key_env_var(settings),
            describe(settings)["provider"],
        )
        return None
    try:
        from rtv.pitwall.provider import AnthropicProvider

        provider = AnthropicProvider()
    except Exception:  # pragma: no cover - the ai extra is optional
        log.warning(
            "Pitwall agents disabled: the 'ai' extra is not installed "
            '(pip install -e ".[ai]").'
        )
        return None

    # Model ids are mapped onto the active backend here rather than in the agent
    # modules, so the per-agent override env vars keep working unchanged whichever
    # backend is selected. resolve_model() leaves an explicit non-Claude id alone.
    from rtv.llm import resolve_model

    def _fast(configured: str) -> str:
        return resolve_model(
            configured or settings.pitwall_agent_model_fast, tier="fast", settings=settings
        )

    only = [n.strip() for n in settings.pitwall_agents_only.split(",") if n.strip()]
    agents = build_agents(
        models={
            "strategist": resolve_model(
                settings.pitwall_strategist_model
                or settings.pitwall_agent_model_reasoning,
                tier="reasoning",
                settings=settings,
            ),
            "vehicle_engineer": _fast(settings.pitwall_vehicle_engineer_model),
            "spotter": _fast(settings.pitwall_spotter_model),
            "coach": _fast(settings.pitwall_coach_model),
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
