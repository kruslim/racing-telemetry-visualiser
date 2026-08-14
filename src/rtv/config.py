"""Application configuration.

All settings can be overridden via environment variables prefixed ``RTV_`` or an
``.env`` file. See ``.env.example`` for the full list.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

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

    # --- LLM backend (shared by coaching Layer 3 and the pitwall agents) --
    #
    # Every LLM layer in this repo speaks the Anthropic Messages wire format.
    # Kimi publishes an Anthropic-compatible endpoint, so switching backend is a
    # base-URL + credential + model-id change rather than a rewrite. The one thing
    # that is NOT portable is Anthropic's native structured output
    # (``messages.parse(output_format=...)``), so the provider carries a
    # tool-forced fallback -- see ``llm_structured_output``.
    llm_provider: str = Field(
        default="anthropic",
        description="Which backend the LLM layers talk to: 'anthropic' or 'kimi'. "
        "Selects the default base URL, credential env var, structured-output "
        "strategy and model ids; every one of those stays individually overridable.",
    )
    llm_base_url: str = Field(
        default="",
        description="Override the backend base URL. Empty = the provider default "
        "(Anthropic's own, or RTV_LLM_KIMI_BASE_URL for kimi).",
    )
    llm_api_key_env: str = Field(
        default="",
        description="Name of the env var holding the credential. Empty = "
        "ANTHROPIC_API_KEY for anthropic, KIMI_API_KEY for kimi. The value is read "
        "at client-construction time and never logged.",
    )
    llm_structured_output: str = Field(
        default="",
        description="How a structured answer is obtained: 'native' uses "
        "messages.parse(output_format=...); 'tool' forces a single respond tool and "
        "validates its input. Empty = native for anthropic, tool for kimi -- "
        "Kimi's compatibility layer implements tools but not output_format.",
    )
    llm_kimi_base_url: str = Field(
        default="https://api.kimi.com/coding",
        description="Anthropic-compatible endpoint for the Kimi Coding subscription. "
        "Use https://api.moonshot.ai/anthropic for the pay-per-token developer API.",
    )

    # --- coaching (Layer 3: LLM orchestration) ---------------------------
    coaching_model: str = Field(
        default="claude-opus-4-8",
        description="Model for the orchestration + eval layer. Overridden by "
        "RTV_LLM_KIMI_MODEL_REASONING when llm_provider=kimi and left at its default.",
    )
    llm_kimi_model_reasoning: str = Field(
        default="kimi-k3",
        description="Kimi model used wherever a reasoning-grade model is called.",
    )
    llm_kimi_model_fast: str = Field(
        default="kimi-k3",
        description="Kimi model used wherever a fast-tier model is called. K3 is a "
        "single tier today; split this if Moonshot ships a smaller sibling.",
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
    pitwall_agent_model_fast: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Model for high-frequency, low-stakes agents.",
    )
    pitwall_agent_model_reasoning: str = Field(
        default="claude-sonnet-4-6",
        description="Model for strategy-grade reasoning.",
    )
    pitwall_strategist_model: str = Field(
        default="",
        description="Override the strategist's model. Empty = the reasoning model.",
    )
    pitwall_vehicle_engineer_model: str = Field(
        default="",
        description="Override the vehicle engineer's model. Empty = the fast model.",
    )
    pitwall_spotter_model: str = Field(
        default="", description="Override the spotter's model. Empty = the fast model."
    )
    pitwall_coach_model: str = Field(
        default="", description="Override the live coach's model. Empty = the fast model."
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
    pitwall_max_calls_per_session: int = Field(
        default=400,
        ge=0,
        description="Hard ceiling on billable model calls in one session, counted "
        "across every agent. Generous on purpose: it is a runaway guard, not a "
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

    def session_parquet_dir(self, session_id: str) -> Path:
        return self.parquet_dir / f"session_id={session_id}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
