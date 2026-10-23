"""Live push of High/Critical alerts to connected staff dashboards over WebSocket.

A WebSocket client is connected to exactly one worker process. An escalation raised while
handling a request in a *different* worker would therefore never reach it — the alert
would be delivered to whichever staff happened to be connected to the right process, which
is indistinguishable from random. When Redis is configured every worker publishes to a
channel and every worker relays what it receives to its own clients, so a connected
dashboard sees an alert regardless of which process raised it.

Route handlers in main.py are plain `def` (run in FastAPI's thread pool), so a broadcast
triggered from inside one of them can't just `await` — this module hands the coroutine to
the main event loop via `run_coroutine_threadsafe` instead, using the loop captured at
startup.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading

from fastapi import WebSocket

from .. import observability
from . import cache

log = logging.getLogger(__name__)

CHANNEL = "raahat:alerts"

_loop: asyncio.AbstractEventLoop | None = None
_connections: set[WebSocket] = set()
_subscriber: threading.Thread | None = None
_stop = threading.Event()


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


async def register(websocket: WebSocket) -> None:
    await websocket.accept()
    _connections.add(websocket)
    observability.incr("raahat_ws_connections_total")


def unregister(websocket: WebSocket) -> None:
    _connections.discard(websocket)


def connection_count() -> int:
    return len(_connections)


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


def _deliver_local(payload: dict) -> None:
    if _loop is None or not _connections:
        return
    asyncio.run_coroutine_threadsafe(_send_all(payload), _loop)


def broadcast_alert(case_id: str, name: str, severity: str, reason: str, alert_id: int | None = None) -> None:
    """Fire-and-forget push to every connected staff dashboard, in every worker.

    Safe to call with no server running (e.g. the ml/predict.py or chatbot.py self-tests).
    """
    payload = {"type": "alert", "case_id": case_id, "name": name, "severity": severity,
               "reason": reason, "alert_id": alert_id}
    redis_client = cache.client()
    if redis_client is not None:
        try:
            redis_client.publish(CHANNEL, json.dumps(payload))
            return  # the subscriber loop in this same worker will deliver it locally
        except Exception:
            log.warning("alert publish failed; delivering to local clients only", exc_info=True)
    _deliver_local(payload)


def start_subscriber() -> None:
    """Relay published alerts to this worker's WebSocket clients.

    Runs on a daemon thread rather than as a task: redis-py's sync pubsub blocks, and the
    alternative (redis.asyncio) would be a second client stack for one subscription.
    """
    global _subscriber
    redis_client = cache.client()
    if redis_client is None or _subscriber is not None:
        return
    _stop.clear()

    def run() -> None:
        while not _stop.is_set():
            try:
                pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(CHANNEL)
                for message in pubsub.listen():
                    if _stop.is_set():
                        break
                    if message.get("type") != "message":
                        continue
                    try:
                        _deliver_local(json.loads(message["data"]))
                    except (ValueError, TypeError):
                        log.warning("dropping malformed alert broadcast")
            except Exception:
                if _stop.is_set():
                    return
                log.warning("alert subscriber disconnected; retrying", exc_info=True)
                _stop.wait(2)

    _subscriber = threading.Thread(target=run, name="raahat-alert-subscriber", daemon=True)
    _subscriber.start()
    log.info("alert subscriber started", extra={"channel": CHANNEL})


def stop_subscriber() -> None:
    _stop.set()
