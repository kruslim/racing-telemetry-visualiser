"""WebSocket pitwall feed: /ws/pitwall.

Two different delivery policies share one socket, because the two payloads have
different value profiles:

* ``state`` -- a full :class:`RaceState` snapshot, sent at the client's chosen
  rate and **latest-wins**. An old snapshot is worthless once a newer one exists,
  so a slow client simply skips ahead (the ``/ws/live`` philosophy).
* ``event`` -- pushed the instant it is published and **never coalesced**. A
  lock-up or a yellow flag is a discrete fact; dropping one to save bandwidth
  would silently lie to the strategist consuming this feed.
"""

from __future__ import annotations

import asyncio

import orjson
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from rtv.logging import get_logger
from rtv.services import AppServices

router = APIRouter()
log = get_logger("api.ws_pitwall")

DEFAULT_RATE_HZ = 4.0
MAX_RATE_HZ = 60.0


def _send(ws: WebSocket, payload: dict) -> asyncio.Future:
    return ws.send_text(orjson.dumps(payload).decode("utf-8"))


@router.websocket("/ws/pitwall")
async def pitwall_ws(ws: WebSocket) -> None:
    await ws.accept()
    services: AppServices = ws.app.state.services
    engine = services.engine
    if engine is None:  # pragma: no cover - router is not mounted when off
        await _send(ws, {"type": "error", "code": "pitwall_disabled"})
        await ws.close()
        return

    rate = {"hz": DEFAULT_RATE_HZ}
    sub = engine.bus.subscribe(name="ws-pitwall")

    # Open with the current state so a client is never blank while it waits.
    await _send(ws, {"type": "state", "state": engine.snapshot().to_api()})

    async def receiver() -> None:
        while True:
            raw = await ws.receive_text()
            try:
                msg = orjson.loads(raw)
            except orjson.JSONDecodeError:
                await _send(ws, {"type": "error", "code": "bad_json"})
                continue
            op = msg.get("op")
            if op in ("subscribe", "set_rate"):
                if "rate_hz" in msg:
                    try:
                        rate["hz"] = max(0.5, min(float(msg["rate_hz"]), MAX_RATE_HZ))
                    except (TypeError, ValueError):
                        await _send(ws, {"type": "error", "code": "bad_rate"})
                        continue
                await _send(ws, {"type": "ack", "op": op, "rate_hz": rate["hz"]})
            elif op == "replay_stop" and services.replay is not None:
                services.replay.stop()
                await _send(ws, {"type": "ack", "op": op})
            else:
                await _send(ws, {"type": "error", "code": "unknown_op", "op": op})

    async def state_sender() -> None:
        last_version = -1
        while True:
            await asyncio.sleep(1.0 / rate["hz"])
            snapshot = engine.snapshot()
            if snapshot.version == last_version:
                continue  # nothing new; do not spam an idle socket
            last_version = snapshot.version
            await _send(ws, {"type": "state", "state": snapshot.to_api()})

    async def event_sender() -> None:
        while True:
            events = await sub.next_events(timeout=1.0)
            for event in events:
                await _send(ws, {"type": "event", "event": event.to_api()})

    try:
        await asyncio.gather(receiver(), state_sender(), event_sender())
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover
        log.debug("pitwall ws closed: %s", exc)
    finally:
        sub.close()
        try:
            await ws.close()
        except Exception:
            pass
