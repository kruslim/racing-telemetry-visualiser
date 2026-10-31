"""Application configuration.

All settings can be overridden via environment variables prefixed ``RTV_`` or an
``.env`` file. See ``.env.example`` for the full list.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RTV_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- storage ---------------------------------------------------------
    data_dir: Path = Field(
        default=Path("./data"),
        description="Root directory for the DuckDB catalog and Parquet telemetry.",
    )
    frontend_dir: Path | None = Field(
        default=None,
        description="Folder served at / (default: the repo's frontend/). The desktop "
        "build points it at its bundled copy.",
    )

    # --- ingestion -------------------------------------------------------
    poll_hz: int = Field(default=60, ge=1, le=360, description="Live poll rate.")
    flush_seconds: float = Field(
        default=0.5, gt=0, description="Buffered seconds before a Parquet flush."
    )
    ibt_chunk_rows: int = Field(
        default=10_000, gt=0, description="Tick-window size for chunked .ibt reads."
    )
    telemetry_dir: Path = Field(
        default=Path.home() / "Documents" / "iRacing" / "telemetry",
        description="Default folder scanned by GET /import/discover for .ibt files.",
    )

    # --- catalog ---------------------------------------------------------
    array_flatten_max: int = Field(
        default=6,
        ge=1,
        description="Arrays with count <= this are flattened to per-index columns; "
        "larger arrays are stored as LIST columns.",
    )

    # --- LLM backend: Kimi Code (shared by every LLM layer) --------------
    #
    # Kimi's endpoint speaks the Anthropic Messages wire format, so the anthropic
    # SDK is the transport. Structured answers come from a forced respond tool;
    # see rtv.llm and rtv.pitwall.provider.
    llm_base_url: str = Field(
        default="",
        description="Override the endpoint. Empty = RTV_LLM_KIMI_BASE_URL.",
    )
    llm_api_key_env: str = Field(
        default="",
        description="Name of the env var holding the credential. Empty = "
        "KIMI_CODE_API_KEY (falling back to KIMI_API_KEY). The value is read at "
        "client-construction time and never logged.",
    )
    llm_kimi_base_url: str = Field(
        default="https://api.kimi.com/coding",
        description="Anthropic-compatible endpoint for the Kimi Code subscription.",
    )
    llm_kimi_model_reasoning: str = Field(
        default="kimi-k3",
        description="Kimi model for reasoning-grade calls: the coach, the orchestrator, "
        "the eval judge and the strategist.",
    )
    llm_kimi_model_fast: str = Field(
        default="kimi-k3",
        description="Kimi model for the high-frequency pitwall agents. K3 is one tier, "
        "so both point at it; split them if a smaller sibling ships.",
    )

    # --- pitwall (v2: deterministic race-state engine + agents) ----------
    pitwall: bool = Field(
        default=True,
        description="Enable the race-state engine, /api/v1/racestate, /ws/pitwall "
        "and the replay driver. Disable to run the v1 surface alone.",
    )
    pitwall_gap_interval: int = Field(
        default=6,
        ge=1,
        description="Recompute standings gaps every Nth frame (6 @ 60 Hz = 10 Hz). "
        "Everything cheaper than gaps still updates every frame.",
    )
    pitwall_fuel_laps: int = Field(
        default=5,
        ge=1,
        description="Green laps in the rolling fuel-consumption mean.",
    )

    pitwall_stint_milestone_laps: int = Field(
        default=5,
        ge=1,
        description="Emit a stint_lap_milestone event every N green laps on a set. "
        "This is the strategist's periodic wake-up, and it is lap-driven, not timed.",
    )
    pitwall_fuel_margin_laps: float = Field(
        default=1.0,
        description="Laps of slack to the finish below which fuel_margin_low fires.",
    )

    # --- deterministic aggregation (stage 3 agent triggers) --------------
    pitwall_corner_buckets: int = Field(
        default=20,
        ge=4,
        le=100,
        description="Lap fractions the track is split into when deciding whether two "
        "lockups happened at 'the same corner'. 20 = 5%% of a lap.",
    )
    pitwall_recurrence_min: int = Field(
        default=3,
        ge=2,
        description="Repeats of one issue at one corner before recurring_issue fires. "
        "This is what keeps a single lockup from costing an LLM call.",
    )
    pitwall_recurrence_window_laps: int = Field(
        default=5,
        ge=1,
        description="Only repeats within this many laps of each other count.",
    )
    pitwall_tyre_temp_trend_c: float = Field(
        default=3.0,
        gt=0.0,
        description="Degrees per lap of sustained tyre-temperature drift across a "
        "stint before tyre_out_of_band fires.",
    )
    pitwall_tyre_axle_imbalance_c: float = Field(
        default=15.0,
        gt=0.0,
        description="Left-to-right tyre temperature spread across one axle that "
        "counts as out of band. Relative, so it needs no per-car knowledge.",
    )
    pitwall_oil_temp_max_c: float = Field(
        default=130.0,
        description="Oil temperature above which car_health_warning fires.",
    )
    pitwall_water_temp_max_c: float = Field(
        default=105.0,
        description="Water temperature above which car_health_warning fires.",
    )
    pitwall_traffic_gap_s: float = Field(
        default=1.5,
        gt=0.0,
        description="Track-position gap inside which a car counts as close, for the "
        "spotter's traffic_close trigger.",
    )

    # --- pitwall agents (v2 stage 2: event-driven LLM layer) -------------
    pitwall_agents: bool = Field(
        default=False,
        description="Mount the agent layer, the radio feed and /api/v1/pitwall/*. "
        "Off by default because it spends money: a key exported for the Layer-3 "
        "coach must not silently start billing for live race radio. Agents still "
        "call no model until a race event fires a trigger.",
    )
    pitwall_agents_live: bool = Field(
        default=True,
        description="Global kill switch applied at startup. False mounts the agent "
        "layer but never invokes a model (radio stays silent).",
    )
    pitwall_strategist_model: str = Field(
        default="",
        description="Override the strategist's model. Empty = RTV_LLM_KIMI_MODEL_REASONING.",
    )
    pitwall_vehicle_engineer_model: str = Field(
        default="",
        description="Override the vehicle engineer's model. Empty = RTV_LLM_KIMI_MODEL_FAST.",
    )
    pitwall_spotter_model: str = Field(
        default="", description="Override the spotter's model. Empty = RTV_LLM_KIMI_MODEL_FAST."
    )
    pitwall_coach_model: str = Field(
        default="", description="Override the live coach's model. Empty = RTV_LLM_KIMI_MODEL_FAST."
    )
    pitwall_strategist_max_age_s: float | None = Field(
        default=None,
        description=(
            "Seconds a strategist call may wait for the radio before it airs "
            "tagged late. Unset = the agent's default (30 s); 0 = never tagged late."
        ),
    )
    pitwall_vehicle_engineer_max_age_s: float | None = Field(
        default=None,
        description=(
            "Seconds a vehicle engineer call may wait for the radio before it airs "
            "tagged late. Unset = the agent's default (20 s); 0 = never tagged late."
        ),
    )
    pitwall_spotter_max_age_s: float | None = Field(
        default=None,
        description=(
            "Seconds a spotter call may wait for the radio before it airs "
            "tagged late. Unset = the agent's default (3 s); 0 = never tagged late."
        ),
    )
    pitwall_coach_max_age_s: float | None = Field(
        default=None,
        description=(
            "Seconds a coach call may wait for the radio before it airs "
            "tagged late. Unset = the agent's default (30 s); 0 = never tagged late."
        ),
    )
    pitwall_agents_only: str = Field(
        default="",
        description="Comma-separated agent names to mount. Empty mounts all of them; "
        "'strategist,spotter' is how a deployment runs a subset without code changes.",
    )
    pitwall_max_inflight: int = Field(
        default=2,
        ge=1,
        le=16,
        description="Cap on concurrent in-flight agent LLM calls.",
    )
    pitwall_agent_cooldown_s: float = Field(
        default=30.0,
        ge=0.0,
        description="Default minimum session-seconds between invocations per trigger type.",
    )
    pitwall_pit_lane_loss_s: float = Field(
        default=25.0,
        gt=0.0,
        description="Seconds lost to a green-flag pit stop; the input to the "
        "deterministic rejoin-position projection.",
    )
    pitwall_radio_history: int = Field(
        default=200,
        ge=1,
        description="Radio messages kept in the ring buffer for late joiners.",
    )
    journal_state_interval_s: float = Field(
        default=1.0,
        gt=0,
        description="Session seconds between race-state snapshots in the race journal.",
    )
    pitwall_max_calls_per_session: int = Field(
        default=400,
        ge=0,
        description="Hard ceiling on billable model calls in one session, counted "
        "across every agent and the live VLM. Generous on purpose: it is a runaway guard, not a "
        "budget (a normal race hour is a low-tens number). 0 = unlimited. The "
        "counter is exposed on GET /api/v1/pitwall/status.",
    )
    pitwall_retry_backoff_s: float = Field(
        default=0.5,
        ge=0.0,
        description="Pause before the single retry of a failed model call. One "
        "retry only: a pit call that lands three corners late is worse than none.",
    )
    pitwall_agent_failure_limit: int = Field(
        default=3,
        ge=0,
        description="Consecutive failed invocations before an agent is taken off "
        "the air automatically. 0 disables the circuit breaker. A revoked key "
        "would otherwise cost one doomed call per race event for a whole race.",
    )

    # --- race director (v2 stage 5: planned; interfaces only) ------------
    pitwall_director_script: str = Field(
        default="",
        description="Path to a ScenarioScript JSON file (see "
        "docs/director_scenario.example.json). Loaded and VALIDATED at startup, "
        "then bound to the default NoopDirector -- which injects nothing, because "
        "the race-director layer ships as interfaces only. Pointing at a script "
        "is currently a way to lint it, not to run it.",
    )

    # --- pitwall TTS (v2 stage 4: the radio's voice) ---------------------
    # The default voice is the *browser's* Web Speech API, which costs nothing
    # and needs no configuration. Everything below is the optional premium path;
    # with it unset, RadioMessage.audio_url stays None and the frontend speaks.
    pitwall_tts: str = Field(
        default="",
        description="Backend TTS provider name (see rtv.pitwall.tts registry: "
        "'none', 'rest', 'openai'). Empty = browser Web Speech only.",
    )
    pitwall_tts_url: str = Field(
        default="",
        description="Speech endpoint for the 'rest'/'openai' provider, e.g. "
        "https://api.openai.com/v1/audio/speech.",
    )
    pitwall_tts_api_key: str = Field(
        default="",
        description="Bearer token for the TTS endpoint. Absent = the provider "
        "reports itself unavailable and the browser does the talking.",
    )
    pitwall_tts_model: str = Field(
        default="", description="Vendor TTS model id. Empty = the provider's default."
    )
    pitwall_tts_format: str = Field(
        default="mp3", description="Vendor audio format requested (mp3, opus, wav)."
    )
    pitwall_tts_media_type: str = Field(
        default="audio/mpeg", description="Content-Type served for that format."
    )
    pitwall_tts_voices: str = Field(
        default="",
        description="Per-agent vendor voice overrides, e.g. "
        "'strategist=onyx,spotter=fable'. Empty = the built-in role voices.",
    )
    pitwall_tts_timeout_s: float = Field(
        default=8.0, gt=0.0, description="Timeout on one synthesis request."
    )
    pitwall_tts_cache: int = Field(
        default=64,
        ge=0,
        description="Synthesised clips kept in memory, keyed by message id.",
    )

    # --- vision: onboard video and VLM visual notes (see docs/VISION.md) ---
    vision_mode: Literal["off", "post", "live"] = Field(
        default="off",
        description="off: no VLM calls (videos, sync, frames and dry-run plans still "
        "work). post: post-race analyses may call Kimi. live: post, plus kept notes for "
        "the session the engine is consuming are published as visual_observation events.",
    )
    vision_model: str = Field(
        default="",
        description="Model for vision calls. Empty = RTV_LLM_KIMI_MODEL_REASONING.",
    )
    vision_max_frames: int = Field(
        default=60,
        ge=1,
        le=2000,
        description="Frame budget per analysis; moments are cut to fit, by trigger rank.",
    )
    vision_frames_per_moment: int = Field(
        default=4, ge=1, le=12, description="Frames sent to the VLM for one moment."
    )
    vision_frame_width: int = Field(
        default=768,
        ge=160,
        le=3840,
        description="Frames are scaled down to this width (px) before they are sent.",
    )
    vision_ffmpeg: str = Field(default="ffmpeg", description="ffmpeg binary name or path.")
    vision_ffprobe: str = Field(default="ffprobe", description="ffprobe binary name or path.")
    vision_capture_input: str = Field(
        default='-f gdigrab -framerate 30 -draw_mouse 0 -i "title=iRacing.com Simulator"',
        description="ffmpeg input arguments for live capture. Default: the iRacing window "
        "on Windows (gdigrab). Any ffmpeg input works, e.g. '-f dshow -i video=OBS "
        "Virtual Camera'.",
    )
    vision_capture_record: bool = Field(
        default=True,
        description="Also write the capture to an MP4 in the session's vision folder and "
        "register it, synced, when the capture stops.",
    )
    vision_capture_preview_fps: float = Field(
        default=10.0, gt=0, le=60, description="Frames per second kept for the live view."
    )
    vision_capture_ring_s: float = Field(
        default=20.0, ge=5, le=600, description="Seconds of live frames kept in memory."
    )
    vision_capture_lag_s: float = Field(
        default=0.0,
        ge=0,
        le=2,
        description="Capture latency taken off each frame's session-time stamp (s).",
    )
    vision_capture_auto: bool = Field(
        default=False,
        description="Start capture when a live iRacing session begins, stop when it ends.",
    )
    vision_live_triggers: str = Field(
        default="incident,offtrack,lockup",
        description="Race events that send the frames around them to the VLM in live mode "
        "(any of incident, offtrack, lockup, wheelspin, traffic_close, recurring_issue).",
    )
    vision_live_max_calls: int = Field(
        default=10,
        ge=0,
        le=500,
        description="VLM calls per session run in live mode; 0 turns the live VLM off. "
        "An inner limit: each call also counts against RTV_PITWALL_MAX_CALLS_PER_SESSION.",
    )
    vision_live_radio: bool = Field(
        default=False,
        description="Let live visual notes wake the spotter and coach (radio calls at info "
        "priority). Off: they go to the timeline, the journal and the review only.",
    )
    vision_live_min_gap_s: float = Field(
        default=5.0,
        ge=0,
        description="Live events closer than this (session s) to the last analysed one "
        "are merged into it.",
    )

    # --- server ----------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8000
    autostart_live: bool = Field(
        default=False, description="Start the live poller on server startup."
    )
    log_level: str = "INFO"

    # --- derived paths ---------------------------------------------------
    @property
    def duckdb_path(self) -> Path:
        return self.data_dir / "telemetry.duckdb"

    @property
    def parquet_dir(self) -> Path:
        return self.data_dir / "parquet"

    def ensure_dirs(self) -> None:
        """Create the data directories if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.parquet_dir.mkdir(parents=True, exist_ok=True)

    @property
    def journal_dir(self) -> Path:
        """Race journals (see :mod:`rtv.store.journal`), one folder per session."""
        return self.data_dir / "sessions"

    def session_parquet_dir(self, session_id: str) -> Path:
        return self.parquet_dir / f"session_id={session_id}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
