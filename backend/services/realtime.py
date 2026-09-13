"""Live push of High/Critical alerts to connected staff dashboards over WebSocket.

Route handlers in main.py are plain `def` (run in FastAPI's thread pool), so a
broadcast triggered from inside one of them can't just `await` — this module hands
the coroutine to the main event loop via `run_coroutine_threadsafe` instead, using
the loop captured once at startup.
"""
from __future__ import annotations

import asyncio
import json

from fastapi import WebSocket

_loop: asyncio.AbstractEventLoop | None = None
_connections: set[WebSocket] = set()


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


async def register(websocket: WebSocket) -> None:
    await websocket.accept()
    _connections.add(websocket)


def unregister(websocket: WebSocket) -> None:
    _connections.discard(websocket)


async def _send_all(payload: dict) -> None:
    dead = []
    message = json.dumps(payload)
    for websocket in list(_connections):
        try:
            await websocket.send_text(message)
        except Exception:
            dead.append(websocket)
    for websocket in dead:
        _connections.discard(websocket)


def broadcast_alert(case_id: str, name: str, severity: str, reason: str) -> None:
    """Fire-and-forget push to every connected staff dashboard. Safe to call even if
    no server is running yet (e.g. the `ml/predict.py` or `chatbot.py` self-tests)."""
    if _loop is None or not _connections:
        return
    payload = {"type": "alert", "case_id": case_id, "name": name, "severity": severity, "reason": reason}
    asyncio.run_coroutine_threadsafe(_send_all(payload), _loop)
