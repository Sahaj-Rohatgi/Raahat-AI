"""Escalation notifications for High/Critical alerts.

An escalation that is raised but never delivered is the failure mode this whole system
exists to prevent, so delivery is treated as part of the safety path rather than a
best-effort side effect:

  * it never blocks the participant's request — the HTTP POST to the on-call webhook runs
    on a background worker thread, so a slow or hanging webhook cannot delay the reply
    someone in distress is waiting for;
  * it retries with backoff, and a permanent failure is logged at ERROR with the case id
    and counted in `raahat_escalation_delivery_total{outcome="failed"}` so it can be
    alerted on. Silently swallowing the exception, as this did, meant a dead webhook was
    indistinguishable from a quiet day;
  * an alert nobody acknowledges inside ESCALATION_ACK_SECONDS is re-notified by the
    sweeper (see escalation_sweep) rather than waiting for someone to refresh a dashboard.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib import error, request

from .. import config, database, observability
from . import cache, realtime

log = logging.getLogger(__name__)

RETRY_DELAYS = (0, 2, 8)  # three attempts
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="raahat-notify")
_sweeper: threading.Thread | None = None
_stop = threading.Event()


def _post(url: str, payload: dict) -> None:
    body = json.dumps(payload).encode()
    last: Exception | None = None
    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        if delay:
            time.sleep(delay)
        try:
            req = request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            with request.urlopen(req, timeout=5) as response:
                if 200 <= response.status < 300:
                    observability.incr("raahat_escalation_delivery_total", outcome="delivered")
                    return
                last = RuntimeError(f"HTTP {response.status}")
        except (OSError, error.URLError) as failure:
            last = failure
        log.warning("escalation webhook attempt failed", extra={"attempt": attempt, "error": str(last)})
    observability.incr("raahat_escalation_delivery_total", outcome="failed")
    log.error("escalation webhook permanently failed — on-call was NOT paged",
              extra={"case_id": payload.get("case_id"), "severity": payload.get("severity"), "error": str(last)})


def notify_escalation(case_id: str, name: str, severity: str, reason: str, alert_id: int | None = None) -> None:
    """Page whoever is on call for this escalation. Returns immediately."""
    observability.incr("raahat_escalations_total", severity=severity)
    log.warning("escalation raised", extra={"case_id": case_id, "severity": severity, "alert_id": alert_id})
    realtime.broadcast_alert(case_id, name, severity, reason, alert_id)
    if not config.ALERT_WEBHOOK_URL:
        observability.incr("raahat_escalation_delivery_total", outcome="no_webhook_configured")
        return
    payload = {"text": f"[Raahat] {severity} escalation — case {case_id} ({name}): {reason}",
               "case_id": case_id, "name": name, "severity": severity, "reason": reason, "alert_id": alert_id}
    _executor.submit(_post, config.ALERT_WEBHOOK_URL, payload)


def _sweep_lease(seconds: int) -> bool:
    """Only one process may sweep per interval.

    Every worker runs the sweeper thread, so without a lease each unacknowledged alert
    would be re-paged once per worker — turning a reminder into a pager storm. With Redis
    the lease is a SET NX EX; without it there is only one process anyway (see
    services/cache.py), so the sweep proceeds.
    """
    redis_client = cache.client()
    if redis_client is None:
        return True
    try:
        return bool(redis_client.set("raahat:sweep-lease", "1", nx=True, ex=max(1, seconds - 1)))
    except Exception:
        log.warning("could not take sweep lease; skipping this round", exc_info=True)
        return False


def escalation_sweep() -> int:
    """Re-notify High/Critical alerts still unacknowledged past the window. Returns how many."""
    try:
        stale = database.unacknowledged_escalations(config.ESCALATION_ACK_SECONDS)
    except Exception:
        log.exception("escalation sweep could not read pending alerts")
        return 0
    for alert in stale:
        log.error("escalation unacknowledged past SLA — re-paging",
                  extra={"case_id": alert["case_id"], "alert_id": alert["id"],
                         "severity": alert["severity"], "attempts": alert.get("notify_attempts", 0)})
        observability.incr("raahat_escalation_reminders_total", severity=alert["severity"])
        notify_escalation(alert["case_id"], alert.get("name") or "", alert["severity"],
                          f"UNACKNOWLEDGED for {config.ESCALATION_ACK_SECONDS}s: {alert['reason']}", alert["id"])
        database.mark_alert_notified(alert["id"])
    return len(stale)


def start_sweeper(interval_seconds: int = 60) -> None:
    global _sweeper
    if _sweeper is not None:
        return
    _stop.clear()

    def run() -> None:
        while not _stop.wait(interval_seconds):
            try:
                if _sweep_lease(interval_seconds):
                    escalation_sweep()
            except Exception:
                log.exception("escalation sweep failed")

    _sweeper = threading.Thread(target=run, name="raahat-escalation-sweeper", daemon=True)
    _sweeper.start()
    log.info("escalation sweeper started", extra={"interval_seconds": interval_seconds})


def stop_sweeper() -> None:
    _stop.set()
