"""Structured logging, request correlation, and process metrics.

Every log line carries the request id of the call that produced it, so a single
participant-facing failure can be followed across the request handler, the database
layer, and the escalation notifier. In production the format is one JSON object per
line, which is what a log shipper expects; locally it stays human-readable.

Nothing here logs message bodies, transcripts, or any other participant content — only
identifiers and outcomes. Case ids are logged because on-call needs them; what was said
in a case never leaves the database.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
import uuid
from collections import defaultdict

from . import config

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

# Fields already on every LogRecord; anything else a caller passed via `extra` is merged
# into the JSON payload rather than dropped.
_STANDARD_FIELDS = set(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message", "asctime", "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        payload.update({k: v for k, v in vars(record).items() if k not in _STANDARD_FIELDS})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rid = request_id_var.get()
        prefix = f"[{rid[:8]}] " if rid != "-" else ""
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {prefix}{record.name}: {record.getMessage()}"
        extras = {k: v for k, v in vars(record).items() if k not in _STANDARD_FIELDS}
        if extras:
            base += "  " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure() -> None:
    """Install the root handler. Safe to call more than once (tests, reload)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if config.LOG_FORMAT == "json" else TextFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(config.LOG_LEVEL)
    # uvicorn installs its own colourised handlers; route them through ours instead so
    # access logs and application logs share one format and one request id.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    if config.SENTRY_DSN:
        _init_sentry()


def _init_sentry() -> None:
    try:
        import sentry_sdk
    except ImportError:
        logging.getLogger(__name__).warning("SENTRY_DSN is set but sentry-sdk is not installed")
        return
    sentry_sdk.init(dsn=config.SENTRY_DSN, environment=config.APP_ENV,
                    traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.05")),
                    send_default_pii=False)


def new_request_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Minimal in-process metrics, exposed at /metrics in Prometheus text format.
# Deliberately dependency-free: the counters that matter operationally here are few,
# and a scrape target is more useful than no metrics at all while waiting for one.
# ---------------------------------------------------------------------------
_counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
_histograms: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = defaultdict(list)
_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)

_HELP = {
    "raahat_requests_total": ("counter", "HTTP requests handled, by method, path template and status class."),
    "raahat_request_seconds": ("histogram", "HTTP request duration in seconds."),
    "raahat_escalations_total": ("counter", "Risk escalations raised, by severity."),
    "raahat_escalation_delivery_total": ("counter", "Outbound escalation notifications, by outcome."),
    "raahat_assessments_total": ("counter", "Risk assessments recorded, by channel and resulting band."),
    "raahat_llm_calls_total": ("counter", "Outbound LLM calls, by purpose and outcome."),
}


def incr(name: str, value: float = 1.0, **labels: str) -> None:
    _counters[(name, tuple(sorted(labels.items())))] += value


def observe(name: str, seconds: float, **labels: str) -> None:
    _histograms[(name, tuple(sorted(labels.items())))].append(seconds)


def _label_string(labels: tuple[tuple[str, str], ...], extra: str = "") -> str:
    parts = [f'{key}="{str(value)}"' for key, value in labels]
    if extra:
        parts.append(extra)
    return "{" + ",".join(parts) + "}" if parts else ""


def render_metrics() -> str:
    lines = []
    seen = set()
    for (name, labels), value in sorted(_counters.items()):
        if name not in seen:
            kind, help_text = _HELP.get(name, ("counter", name))
            lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
            seen.add(name)
        lines.append(f"{name}{_label_string(labels)} {value}")
    for (name, labels), values in sorted(_histograms.items()):
        if name not in seen:
            kind, help_text = _HELP.get(name, ("histogram", name))
            lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
            seen.add(name)
        for bucket in _BUCKETS:
            bucket_labels = _label_string(labels, 'le="%s"' % bucket)
            lines.append(f"{name}_bucket{bucket_labels} {sum(1 for v in values if v <= bucket)}")
        inf_labels = _label_string(labels, 'le="+Inf"')
        lines.append(f"{name}_bucket{inf_labels} {len(values)}")
        lines.append(f"{name}_sum{_label_string(labels)} {sum(values)}")
        lines.append(f"{name}_count{_label_string(labels)} {len(values)}")
    return "\n".join(lines) + "\n"
