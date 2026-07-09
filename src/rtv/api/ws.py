"""WebSocket live stream: /ws/live.

Latest-frame-only delivery — the sender task reads the hub snapshot at the
client's requested rate (capped at the poll rate), so a slow client coalesces to
the newest sample and never accumulates a backlog.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import orjson
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from rtv.catalog.decode import decode_flags, decode_label
from rtv.catalog.models import Catalog
from rtv.ingest.frame import Frame
from rtv.logging import get_logger
from rtv.services import AppServices

router = APIRouter()
log = get_logger("api.ws")


@dataclass
class Subscription:
    channels: set[str] = field(default_factory=set)
    rate_hz: float = 30.0
    include_flags: bool = True
    sent_catalog_hash: str | None = None
    last_tick: int = -1
    last_state_seq: int = -1


def _send(ws: WebSocket, payload: dict) -> asyncio.Future:
    return ws.send_text(orjson.dumps(payload).decode("utf-8"))


def _build_data(frame: Frame, catalog: Catalog, sub: Subscription) -> dict:
    v: dict = {}
    flags: dict = {}
    for name in sub.channels:
        if name not in catalog:
            continue
        raw = frame.values.get(name)
        var = catalog[name]
        if var.decoder == "bitfield":
            if sub.include_flags:
                flags[name] = decode_flags(name, raw) if not var.is_array else [
                    decode_flags(name, x) for x in (raw or [])
                ]
            else:
                v[name] = raw
        elif var.decoder == "enum":
            v[name] = decode_label(name, raw) if not var.is_array else [
                decode_label(name, x) for x in (raw or [])
            ]
        else:
            v[name] = raw
    msg = {"type": "data", "tick": frame.tick, "t": frame.session_time, "v": v}
    if flags:
        msg["flags"] = flags
    return msg


@router.websocket("/ws/live")
async def live_ws(ws: WebSocket) -> None:
    await ws.accept()
    services: AppServices = ws.app.state.services
    hub = services.hub
    poll_hz = services.settings.poll_hz
    sub = Subscription()

    async def receiver() -> None:
        while True:
            raw = await ws.receive_text()
            try:
                msg = orjson.loads(raw)
            except orjson.JSONDecodeError:
                await _send(ws, {"type": "error", "code": "bad_json"})
                continue
            op = msg.get("op")
            if op == "subscribe":
                sub.channels.update(msg.get("channels", []))
                if "rate_hz" in msg:
                    sub.rate_hz = max(1.0, min(float(msg["rate_hz"]), poll_hz))
                sub.include_flags = bool(msg.get("include_flags", sub.include_flags))
                sub.sent_catalog_hash = None  # force a fresh catalog push
            elif op == "unsubscribe":
                for c in msg.get("channels", []):
                    sub.channels.discard(c)
            elif op == "set_rate":
                sub.rate_hz = max(1.0, min(float(msg.get("rate_hz", 30)), poll_hz))
            else:
                await _send(ws, {"type": "error", "code": "unknown_op", "op": op})

    async def sender() -> None:
        while True:
            await asyncio.sleep(1.0 / sub.rate_hz)
            state, session_id, state_seq = hub.state()
            if state_seq != sub.last_state_seq:
                sub.last_state_seq = state_seq
                await _send(
                    ws,
                    {"type": "state", "connection": state.value, "session_id": session_id},
                )
            frame, catalog = hub.snapshot()
            if catalog is None or frame is None:
                continue
            if sub.sent_catalog_hash != catalog.schema_hash:
                sub.sent_catalog_hash = catalog.schema_hash
                await _send(ws, {"type": "catalog", **catalog.to_api()})
            if not sub.channels or frame.tick == sub.last_tick:
                continue
            sub.last_tick = frame.tick
            await _send(ws, _build_data(frame, catalog, sub))

    try:
        await asyncio.gather(receiver(), sender())
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover
        log.debug("ws closed: %s", exc)
    finally:
        try:
            await ws.close()
        except Exception:
            pass
