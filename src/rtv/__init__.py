"""Racing Telemetry Visualiser backend.

Ingests all iRacing telemetry (live shared-memory SDK feed and recorded .ibt
files), persists it columnar in DuckDB/Parquet, and serves it to a web frontend
over REST (charts/replays) and a WebSocket live stream.
"""

__version__ = "0.1.0"
