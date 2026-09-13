"""In-process sliding-window rate limiting for the public-facing routes.

Demo scope: counters live in this process's memory, so they reset on restart and are
not shared across workers — a real deployment would put them in Redis. That is enough
to stop the thing this actually guards against: a single loop hammering /api/chat or
/api/auth/login and flooding the alert queue, the WebSocket broadcast, and the login
path with brute-force attempts.

Safety note: the SOS limit is deliberately loose. Rate limiting an emergency button is
a trade-off, so the ceiling only exists to stop an automated flood — a distressed person
pressing it repeatedly stays well under it.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

_hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

# bucket -> (max requests, window in seconds). The login bucket counts FAILED sign-ins only:
# a successful login is not the thing being defended against, and counting them locked out
# ordinary use — the demo role switcher performs a real re-login on every switch, and the test
# suite signs in several times per run.
LIMITS = {
    "login": (8, 300),
    "chat": (20, 60),
    "voice": (6, 60),
    "checkin": (12, 60),
    "sos": (10, 60),
}


def client_key(request: Request) -> str:
    client = request.client
    return client.host if client else "unknown"


def peek(bucket: str, key: str) -> None:
    """Raise 429 if the caller is already over the bucket's limit, without recording a hit."""
    limit, window = LIMITS[bucket]
    now, hits = time.monotonic(), _hits[(bucket, key)]
    while hits and now - hits[0] > window:
        hits.popleft()
    if len(hits) >= limit:
        retry_after = max(1, int(window - (now - hits[0])))
        raise HTTPException(429, f"Too many requests; please wait {retry_after}s and try again.",
                            headers={"Retry-After": str(retry_after)})


def record(bucket: str, key: str) -> None:
    _hits[(bucket, key)].append(time.monotonic())


def check(bucket: str, key: str) -> None:
    """Record one hit and raise 429 if the caller is over the bucket's limit."""
    peek(bucket, key)
    record(bucket, key)


def reset() -> None:
    """Clear every counter — used by the test suite so one suite run does not trip the
    limits for the next."""
    _hits.clear()
