"""Escalation notifications for High/Critical alerts.

Always logs to the console. If ALERT_WEBHOOK_URL is set (e.g. a Slack incoming
webhook or an internal on-call endpoint), also POSTs a JSON payload there so a
real case worker gets pinged instead of relying on someone polling the dashboard.
"""
from __future__ import annotations

import json
import os
from urllib import error, request

from . import realtime


def notify_escalation(case_id: str, name: str, severity: str, reason: str) -> None:
    message = f"[Raahat] {severity} escalation — case {case_id} ({name}): {reason}"
    print(message)
    realtime.broadcast_alert(case_id, name, severity, reason)
    url = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    if not url: return
    payload = {"text": message, "case_id": case_id, "name": name, "severity": severity, "reason": reason}
    try:
        req = request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
        request.urlopen(req, timeout=5)
    except (OSError, error.URLError):
        pass
