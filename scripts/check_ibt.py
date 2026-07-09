"""Import a real .ibt file through the production pipeline and query it.

Usage:  python scripts/check_ibt.py "C:\\path\\to\\file.ibt"
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from rtv.ingest.ibt import import_ibt  # noqa: E402
from rtv.store.duck import Database  # noqa: E402
from rtv.store.repository import Repository  # noqa: E402
from rtv.store.writer import TelemetryWriter  # noqa: E402


def main(path: str) -> int:
    data_dir = Path(tempfile.gettempdir()) / ("rtv_ibt_" + uuid.uuid4().hex[:8])
    parquet_dir = data_dir / "parquet"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(data_dir / "telemetry.duckdb")
    writer = TelemetryWriter(db, parquet_dir)
    repo = Repository(db, parquet_dir)

    print(f"Importing: {path}")
    t0 = time.time()
    last = [0.0]

    def progress(p: float) -> None:
        if p - last[0] >= 0.1 or p >= 1.0:
            last[0] = p
            print(f"  ... {p*100:5.1f}%")

    sid = import_ibt(path, writer, chunk_rows=20_000, progress=progress)
    dt = time.time() - t0
    print(f"Imported session {sid} in {dt:.1f}s")

    sess = repo.get_session(sid)
    print("\n--- SESSION ---")
    for k in ("kind", "car_id", "track_name", "ir_subsession_id", "sample_count",
              "schema_hash", "status"):
        print(f"  {k}: {sess.get(k)}")

    cat = repo.get_catalog(sid)
    print(f"\n--- CATALOG: {cat['count']} variables ---")
    by_storage: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for v in cat["variables"]:
        by_storage[v["storage"]] = by_storage.get(v["storage"], 0) + 1
        by_type[v["ir_type"]] = by_type.get(v["ir_type"], 0) + 1
    print(f"  by type:    {by_type}")
    print(f"  by storage: {by_storage}")
    sample = [v["name"] for v in cat["variables"]]
    print(f"  first 12:   {sample[:12]}")

    laps = repo.get_laps(sid)
    print(f"\n--- LAPS: {len(laps)} ---")
    for lap in laps[:12]:
        lt = lap["lap_time"]
        print(f"  lap {lap['lap']:>2}  time={lt:7.3f}s  "
              f"ticks {lap['start_tick']}..{lap['end_tick']}  "
              f"out={lap['is_out_lap']} in={lap['is_in_lap']}")

    # Pick a representative mid lap for channel checks.
    target = laps[len(laps) // 2]["lap"] if laps else 0
    print(f"\n--- CHANNELS (lap {target}) ---")
    for names in (["Speed"], ["Throttle", "Brake", "RPM"]):
        try:
            res = repo.get_channels(sid, names, lap=target, max_points=500,
                                    x="lap_dist_pct")
            for n in names:
                ys = [y for y in res["channels"][n]["y"] if y is not None]
                unit = res["channels"][n]["unit"]
                if ys:
                    print(f"  {n:<10} unit={unit or '-':<6} "
                          f"min={min(ys):8.2f} max={max(ys):8.2f} "
                          f"pts={res['downsample']['points']} "
                          f"src={res['downsample']['source_points']}")
        except Exception as exc:
            print(f"  {names}: ERROR {exc}")

    print("\n--- TRACK MAP ---")
    try:
        tm = repo.get_trackmap(sid, lap=target, color="Speed", max_points=300)
        pts = tm["points"]
        lats = [p["lat"] for p in pts]
        lons = [p["lon"] for p in pts]
        print(f"  points={len(pts)}  lat[{min(lats):.5f}..{max(lats):.5f}]  "
              f"lon[{min(lons):.5f}..{max(lons):.5f}]")
    except Exception as exc:
        print(f"  trackmap: {exc}")

    info = repo.get_session_info(sid)
    print("\n--- SESSION INFO (YAML) ---")
    if info:
        wk = info.get("WeekendInfo", {})
        print(f"  TrackDisplayName: {wk.get('TrackDisplayName')}")
        print(f"  TrackConfigName:  {wk.get('TrackConfigName')}")
        drv = info.get("DriverInfo", {})
        drivers = drv.get("Drivers", [])
        if drivers:
            d0 = drivers[0]
            print(f"  Driver[0]: {d0.get('UserName')} / car {d0.get('CarScreenName')}")
        print(f"  top-level sections: {list(info.keys())}")
    else:
        print("  (no session info parsed)")

    db.close()
    print("\nOK — real .ibt imported and queried successfully.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python scripts/check_ibt.py <file.ibt>")
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
