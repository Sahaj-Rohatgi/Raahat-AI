"""Sliding-window rate limiting for the public-facing routes.

Counters live in Redis when it is configured, so the limit is the same number whether the
app runs as one process or twenty, and it survives a deploy. Without Redis they fall back
to this process's memory — see services/cache.py for why that is only acceptable
single-worker.

What this actually guards: a loop hammering /api/chat or /api/auth/login, which would
otherwise flood the alert queue, the WebSocket broadcast, and the login path with
brute-force attempts.

Safety note: the SOS limit is deliberately loose. Rate limiting an emergency button is a
trade-off, so the ceiling only exists to stop an automated flood — a distressed person
pressing it repeatedly stays well under it. If Redis is down, SOS is never blocked: an
infrastructure failure must not stand between someone and an emergency.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from .. import config
from . import cache

log = logging.getLogger(__name__)

_hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

# bucket -> (max requests, window in seconds). The login bucket counts FAILED sign-ins only:
# a successful login is not the thing being defended against, and counting them locked out
# ordinary use.
LIMITS = {
    "login": (8, 300),
    "chat": (20, 60),
    "voice": (6, 60),
    "checkin": (12, 60),
    "sos": (10, 60),
    "demo": (20, 60),
}
# Buckets that must fail open if the shared counter store is unavailable.
FAIL_OPEN = {"sos"}


def client_key(request: Request) -> str:
    """The caller's source address.

    X-Forwarded-For is only honoured when TRUST_PROXY_HEADERS says a known proxy sits in
    front. Trusting it unconditionally would let any client forge the header and give
    itself a fresh rate-limit bucket per request, defeating the login brute-force guard
    entirely.
    """
    if config.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            # Left-most entry is the original client; the proxy appends itself on the right.
            return forwarded.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"


def _redis_count(bucket: str, key: str, window: int, record_hit: bool) -> int | None:
    """Sliding window over a Redis sorted set. Returns the count in-window, or None if
    Redis is unavailable (the caller then decides whether to fall back or fail open)."""
    redis_client = cache.client()
    if redis_client is None:
        return None
    name = f"rl:{bucket}:{key}"
    now = time.time()
    try:
        pipe = redis_client.pipeline()
        pipe.zremrangebyscore(name, 0, now - window)
        if record_hit:
            # The member has to be unique per hit or two requests in the same microsecond
            # collapse into one entry and the limit silently doubles.
            pipe.zadd(name, {f"{now}:{time.monotonic_ns()}": now})
        pipe.zcard(name)
        pipe.expire(name, window + 1)
        return pipe.execute()[-2]
    except Exception:
        log.warning("rate-limit store unavailable; falling back to in-process counters", exc_info=True)
        return None


def _local_count(bucket: str, key: str, window: int, record_hit: bool) -> int:
    now, hits = time.monotonic(), _hits[(bucket, key)]
    while hits and now - hits[0] > window:
        hits.popleft()
    if record_hit:
        hits.append(now)
    return len(hits)


def _enforce(bucket: str, key: str, record_hit: bool) -> None:
    limit, window = LIMITS[bucket]
    count = _redis_count(bucket, key, window, record_hit)
    if count is None:
        if bucket in FAIL_OPEN and config.REDIS_URL:
            return  # never block an emergency on an infrastructure failure
        count = _local_count(bucket, key, window, record_hit)
    # When the hit was recorded, `count` already includes it, so the ceiling is `> limit`;
    # a peek is checking whether one more would exceed it, so it is `>= limit`.
    over = count > limit if record_hit else count >= limit
    if over:
        raise HTTPException(429, f"Too many requests; please wait {window}s and try again.",
                            headers={"Retry-After": str(window)})


def peek(bucket: str, key: str) -> None:
    """Raise 429 if the caller is already over the bucket's limit, without recording a hit."""
    _enforce(bucket, key, record_hit=False)


def record(bucket: str, key: str) -> None:
    limit, window = LIMITS[bucket]
    if _redis_count(bucket, key, window, True) is None:
        _local_count(bucket, key, window, True)


def check(bucket: str, key: str) -> None:
    """Record one hit and raise 429 if the caller is over the bucket's limit."""
    _enforce(bucket, key, record_hit=True)


def reset() -> None:
    """Clear every counter — used by the test suite so one suite run does not trip the
    limits for the next."""
    _hits.clear()
    redis_client = cache.client()
    if redis_client is None:
        return
    try:
        keys = list(redis_client.scan_iter("rl:*", count=500))
        if keys:
            redis_client.delete(*keys)
    except Exception:
        log.warning("could not clear Redis rate-limit keys", exc_info=True)
