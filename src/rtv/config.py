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

    # --- coaching (Layer 3: Claude API orchestration) --------------------
    coaching_model: str = Field(
        default="claude-opus-4-8",
        description="Claude model for the orchestration + eval layer. "
        "The ANTHROPIC_API_KEY env var is read directly by the Anthropic SDK.",
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
