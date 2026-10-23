"""Shared state backend (Redis) with a documented single-process fallback.

Two things in this app are only correct when they are shared across every worker:

  * rate limits — counters in one process's memory mean the effective limit is
    (limit x worker count) and resets to zero on every deploy, and
  * alert fan-out — a WebSocket client is connected to exactly one worker, so an
    escalation raised in another worker never reaches it.

If REDIS_URL is set, both use Redis. If it is not, they fall back to per-process state and
say so loudly at startup, because running multi-worker without it is a silent correctness
bug rather than a performance trade-off.
"""
from __future__ import annotations

import logging

from .. import config

log = logging.getLogger(__name__)

_client = None
_checked = False


def client():
    """Returns a connected Redis client, or None if Redis is not configured/reachable."""
    global _client, _checked
    if _checked:
        return _client
    _checked = True
    if not config.REDIS_URL:
        if config.IS_PRODUCTION:
            log.warning("REDIS_URL is not set: rate limits and alert fan-out are per-process only. "
                        "Run a single worker, or configure Redis before scaling out.")
        return None
    try:
        import redis
    except ImportError:
        log.error("REDIS_URL is set but the redis package is not installed")
        return None
    try:
        candidate = redis.Redis.from_url(config.REDIS_URL, decode_responses=True,
                                         socket_timeout=2, socket_connect_timeout=2,
                                         health_check_interval=30)
        candidate.ping()
    except Exception:
        log.exception("Redis is configured but unreachable; falling back to per-process state")
        return None
    _client = candidate
    log.info("Redis connected")
    return _client


def available() -> bool:
    return client() is not None


def reset_for_tests() -> None:
    global _client, _checked
    _client, _checked = None, False
