"""Gunicorn configuration for the production process.

Uvicorn's own --workers is fine, but gunicorn gives graceful reloads, worker timeouts and
restart-on-max-requests, which is what keeps a long-running deployment healthy.

Worker count: route handlers here are sync `def`, so each worker serves concurrency from a
thread pool over a bounded database pool. Two workers per core is a starting point;
DB_POOL_SIZE x WEB_CONCURRENCY must stay under the MySQL server's max_connections.
"""
from __future__ import annotations

import multiprocessing
import os

bind = os.getenv("BIND", "0.0.0.0:8000")
workers = int(os.getenv("WEB_CONCURRENCY", str(min(8, (multiprocessing.cpu_count() * 2) + 1))))
worker_class = "uvicorn.workers.UvicornWorker"

# Longer than the slowest deliberate call (voice transcription, 20s upstream timeout) so a
# real request is never killed mid-flight, short enough that a wedged worker is recycled.
timeout = 45
graceful_timeout = 30
# Keepalive slightly above a typical load-balancer idle timeout avoids races where the LB
# reuses a connection the worker has just closed.
keepalive = 75

# Recycle workers periodically; the jitter stops every worker restarting at once.
max_requests = 2000
max_requests_jitter = 200

# Logging is configured by backend.observability (structured, request-id correlated), so
# gunicorn's own access log is disabled rather than duplicating it in a different format.
accesslog = None
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info").lower()

# Trust X-Forwarded-* only from the proxy in front. Set to the load balancer's address;
# "*" is only correct when nothing but the proxy can reach this port.
forwarded_allow_ips = os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
