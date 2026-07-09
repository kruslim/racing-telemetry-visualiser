"""Offline end-to-end smoke test of the HTTP surface (no iRacing required).

Boots the real FastAPI app via TestClient (runs lifespan), writes a synthetic
session straight through the storage writer, then exercises every read endpoint
over HTTP and prints the results.

Run:  python scripts/smoke_offline.py
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import time
import uuid

# Make src + repo root importable when run directly.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

os.environ.setdefault(
    "RTV_DATA_DIR", os.path.join(tempfile.gettempdir(), "rtv_smoke_" + uuid.uuid4().hex[:8])
)

from fastapi.testclient import TestClient  # noqa: E402

from rtv.catalog.builder import RawHeader, build_catalog  # noqa: E402
from rtv.catalog.types import IRType  # noqa: E402
from rtv.config import get_settings  # noqa: E402
from rtv.domain.models import Session, SessionKind  # noqa: E402
from rtv.ingest.frame import FrameBuffer  # noqa: E402
from rtv.ingest.normalize import LapTracker  # noqa: E402
from rtv.main import create_app  # noqa: E402


def synthetic_catalog():
    headers = [
        RawHeader("SessionTime", IRType.DOUBLE, unit="s"),
        RawHeader("SessionTick", IRType.INT),
        RawHeader("Lap", IRType.INT),
        RawHeader("LapDist", IRType.FLOAT, unit="m"),
        RawHeader("LapDistPct", IRType.FLOAT, unit="%"),
        RawHeader("Speed", IRType.FLOAT, unit="m/s"),
        RawHeader("Throttle", IRType.FLOAT, unit="%"),
        RawHeader("Brake", IRType.FLOAT, unit="%"),
        RawHeader("Gear", IRType.INT),
        RawHeader("Lat", IRType.DOUBLE, unit="deg"),
        RawHeader("Lon", IRType.DOUBLE, unit="deg"),
        RawHeader("OnPitRoad", IRType.BOOL),
        RawHeader("LapLastLapTime", IRType.FLOAT, unit="s"),
        RawHeader("SessionFlags", IRType.BITFIELD),
        RawHeader("TyrePressure", IRType.FLOAT, count=4, unit="kPa"),
        RawHeader("CarIdxLapDistPct", IRType.FLOAT, count=64, unit="%"),
    ]
    return build_catalog(headers, source="ibt", flatten_max=6)


def populate(services, session_id: str) -> None:
    cat = synthetic_catalog()
    services.writer.begin_session(
        Session(session_id=session_id, kind=SessionKind.IBT, schema_hash=cat.schema_hash,
                started_at=time.time()),
        cat,
    )
    tracker = LapTracker(session_id)
    buf = FrameBuffer(cat)
    tick, t = 0, 0.0
    for lap in range(1, 4):
        for i in range(300):
            pct = i / 300
            a = pct * 2 * math.pi
            values = {
                "SessionTime": t, "SessionTick": tick, "Lap": lap,
                "LapDist": pct * 5000.0, "LapDistPct": pct,
                "Speed": 40 + 20 * math.sin(a), "Throttle": max(0.0, math.sin(a)),
                "Brake": max(0.0, -math.sin(a)), "Gear": 3,
                "Lat": 52 + 0.01 * math.sin(a), "Lon": -1 + 0.01 * math.cos(a),
                "OnPitRoad": False, "LapLastLapTime": 90.0 if i == 0 and lap > 1 else 0.0,
                "SessionFlags": 0x4, "TyrePressure": [165.0, 165.5, 164.0, 164.5],
                "CarIdxLapDistPct": [pct] + [0.0] * 63,
            }
            tracker.update(values, tick=tick, session_time=t)
            buf.append(values, tick=tick, session_time=t, lap=lap)
            tick += 1
            t += 1 / 60
    tracker.finalize(tick=tick - 1, session_time=float(tick - 1))
    services.writer.write_telemetry(session_id, buf.to_arrow())
    services.writer.write_laps(session_id, tracker.laps)
    services.writer.end_session(session_id, sample_count=tick, ended_at=time.time())


def show(label, r):
    body = r.json()
    print(f"\n=== {label} -> HTTP {r.status_code} ===")
    if isinstance(body, dict):
        for k, v in body.items():
            s = str(v)
            print(f"  {k}: {s[:90]}{'…' if len(s) > 90 else ''}")
    else:
        print("  ", str(body)[:200])


def main() -> int:
    get_settings.cache_clear()
    app = create_app()
    sid = "smoke-session"
    with TestClient(app) as c:
        populate(app.state.services, sid)

        show("/health", c.get("/api/v1/health"))
        show("/live/status", c.get("/api/v1/live/status"))
        show("/sessions", c.get("/api/v1/sessions"))

        var = c.get(f"/api/v1/sessions/{sid}/variables").json()
        print(f"\n=== /sessions/{sid}/variables ===")
        print(f"  schema_hash={var['schema_hash']} count={var['count']}")
        for v in var["variables"][:6]:
            extra = f" flags={v.get('flags')}" if v.get("flags") else ""
            print(f"  - {v['name']:<18} {v['ir_type']:<9} unit={v['unit'] or '-':<6}"
                  f" storage={v['storage']}{extra}")
        print(f"  ... ({var['count']} variables total; CarIdxLapDistPct stored as "
              f"{next(x['storage'] for x in var['variables'] if x['name']=='CarIdxLapDistPct')})")

        show("/laps", c.get(f"/api/v1/sessions/{sid}/laps"))

        ch = c.get(f"/api/v1/sessions/{sid}/channels",
                   params={"names": "Speed,Throttle,Brake", "lap": 2,
                           "max_points": 200, "x": "lap_dist_pct"})
        body = ch.json()
        print("\n=== /channels?names=Speed,Throttle,Brake&lap=2&max_points=200 ===")
        ds = body["downsample"]
        print(f"  x={body['x']} points={ds['points']} "
              f"source_points={ds['source_points']} bucket={ds['bucket']}")
        print(f"  channels={list(body['channels'].keys())} "
              f"Speed.y[0:3]={[round(y,1) for y in body['channels']['Speed']['y'][:3]]}")

        cmp = c.get(f"/api/v1/sessions/{sid}/compare",
                    params={"name": "Speed", "laps": "1,2,3", "grid": 100}).json()
        print("\n=== /compare?name=Speed&laps=1,2,3&grid=100 ===")
        print(f"  laps={list(cmp['laps'].keys())} grid={cmp['grid']} "
              f"x_values[0:3]={[round(x,3) for x in cmp['x_values'][:3]]}")

        tm = c.get(f"/api/v1/sessions/{sid}/trackmap",
                   params={"color": "Speed", "max_points": 500}).json()
        print("\n=== /trackmap?color=Speed&max_points=500 ===")
        print(f"  count={tm['count']} first_point={tm['points'][0]}")

        flat = c.get(f"/api/v1/sessions/{sid}/channels/TyrePressure_0",
                     params={"lap": 1, "max_points": 50}).json()
        print("\n=== /channels/TyrePressure_0 (flattened array column) ===")
        print(f"  points={flat['downsample']['points']} "
              f"y[0]={flat['channels']['TyrePressure_0']['y'][0]}")

        cf = c.get("/api/v1/coaching/lap-findings",
                   params={"session_id": sid, "main_lap": 2, "ref_lap": 1})
        body = cf.json()
        print(f"\n=== /coaching/lap-findings?main_lap=2&ref_lap=1 -> HTTP {cf.status_code} ===")
        print(f"  corners={len(body['corners'])} losing={body['chief']['losing_n']} "
              f"net_lap={body['chief']['net_lap']:.3f}s lap_length={body['lap_length_m']:.0f} m")
        if body["notes"]:
            print(f"  notes={body['notes']}")

    print("\nAll offline endpoints responded successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
