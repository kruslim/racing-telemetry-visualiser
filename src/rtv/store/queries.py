"""SQL builders for chart, lap-comparison and track-map queries over Parquet.

Channel/column names are validated against a regex and quoted; file paths are
passed as bound parameters to ``read_parquet`` so neither is injectable.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class QueryError(ValueError):
    pass


def _ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise QueryError(f"Invalid column name: {name!r}")
    return f'"{name}"'


def _x_expr(x: str) -> str:
    mapping = {
        "tick": "tick",
        "session_time": "session_time",
        "lap_dist_pct": '"LapDistPct"',
    }
    if x not in mapping:
        raise QueryError(f"Invalid x axis: {x!r}")
    return mapping[x]


def lap_glob(session_dir: Path, lap: int) -> str:
    return str(session_dir / f"lap={int(lap)}" / "*.parquet")


def session_glob(session_dir: Path) -> str:
    return str(session_dir / "lap=*" / "*.parquet")


def count_rows_sql() -> str:
    return "SELECT count(*) AS n FROM read_parquet(?)"


def channels_sql(names: list[str], x: str, bucket: int) -> str:
    """Bucketed min/avg/max downsample for one or more channels over a lap."""
    xexpr = _x_expr(x)
    chan_idents = [_ident(n) for n in names]
    if bucket <= 1:
        select_cols = ", ".join(
            [f"{xexpr} AS x"] + [f"{c} AS {c}" for c in chan_idents]
        )
        return (
            f"SELECT {select_cols} FROM read_parquet(?) ORDER BY tick"
        )

    aggs = [f"avg({xexpr}) AS x"]
    for n, c in zip(names, chan_idents, strict=True):
        aggs.append(f"avg({c}) AS {_ident(n + '__avg')}")
        aggs.append(f"min({c}) AS {_ident(n + '__min')}")
        aggs.append(f"max({c}) AS {_ident(n + '__max')}")
    agg_sql = ", ".join(aggs)
    return f"""
        WITH src AS (
            SELECT *, (row_number() OVER (ORDER BY tick) - 1) // {int(bucket)} AS _b
            FROM read_parquet(?)
        )
        SELECT {agg_sql} FROM src GROUP BY _b ORDER BY _b
    """


def compare_sql(name: str, grid: int) -> str:
    """Resample one channel onto a `grid`-step lap_dist_pct axis (0..1)."""
    c = _ident(name)
    return f"""
        SELECT CAST(floor("LapDistPct" * {int(grid)}) AS INTEGER) AS bucket,
               avg({c}) AS y
        FROM read_parquet(?)
        WHERE "LapDistPct" IS NOT NULL
        GROUP BY bucket
        ORDER BY bucket
    """


def trackmap_sql(color: str | None, bucket: int) -> str:
    """Decimated GPS polyline, optionally coloured by a channel."""
    color_sel = f", avg({_ident(color)}) AS color" if color else ""
    if bucket <= 1:
        color_raw = f", {_ident(color)} AS color" if color else ""
        return (
            'SELECT "Lat" AS lat, "Lon" AS lon, "LapDistPct" AS lap_dist_pct'
            f"{color_raw} FROM read_parquet(?) ORDER BY tick"
        )
    return f"""
        WITH src AS (
            SELECT *, (row_number() OVER (ORDER BY tick) - 1) // {int(bucket)} AS _b
            FROM read_parquet(?)
        )
        SELECT avg("Lat") AS lat, avg("Lon") AS lon,
               avg("LapDistPct") AS lap_dist_pct{color_sel}
        FROM src GROUP BY _b ORDER BY _b
    """


def bucket_size(total: int, max_points: int) -> int:
    """Rows per bucket so the result has <= max_points points."""
    if total <= max_points or max_points <= 0:
        return 1
    return math.ceil(total / max_points)
