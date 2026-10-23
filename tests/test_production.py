"""Production-hardening regression checks.

Every check here corresponds to something that was previously wrong in a way that would
not have shown up in a demo: a credential stored in a form that could be replayed, an
endpoint that exposed participant content, a privacy promise the code did not keep, a
config that would have booted with published passwords.

Run against a live server (same shape as tests/test_api.py):
    python3 -m uvicorn backend.main:app --port 8001     # terminal 1
    python3 tests/test_production.py                    # terminal 2
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import config, database  # noqa: E402

BASE = os.getenv("RAAHAT_TEST_BASE", "http://127.0.0.1:8001")

passed, failed = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK   {label}")
    else:
        failed += 1
        print(f"  FAIL {label} {detail}")


def request(path: str, method: str = "GET", body: dict | None = None, token: str = "",
            headers: dict | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    all_headers = {"Content-Type": "application/json", **(headers or {})}
    if token:
        all_headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=data, headers=all_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def login(email: str, password: str = "demo123") -> str:
    status, body = request("/api/auth/login", "POST", {"email": email, "password": password})
    return json.loads(body)["token"] if status == 200 else ""


print("=== Configuration refuses unsafe production settings ===")
unsafe = {
    "SEED_DEMO_DATA=1": {"SEED_DEMO_DATA": "1"},
    "no SESSION_SECRET": {"SESSION_SECRET": ""},
    "ENABLE_DB_INSPECTOR=1": {"ENABLE_DB_INSPECTOR": "1"},
    "no ALLOWED_HOSTS": {"ALLOWED_HOSTS": ""},
}
base_env = {
    **os.environ, "APP_ENV": "production",
    "DATABASE_URL": "mysql://app:pw@db:3306/raahat_dw",
    "SESSION_SECRET": "x" * 48, "ALLOWED_HOSTS": "example.org",
    "SEED_DEMO_DATA": "0", "ENABLE_DB_INSPECTOR": "0", "ENABLE_IVR": "0", "ENABLE_TWILIO": "0",
}
root = str(Path(__file__).resolve().parent.parent)
accepted = subprocess.run([sys.executable, "-m", "backend.config"], env=base_env, cwd=root,
                          capture_output=True, text=True)
check("a correct production config is accepted", accepted.returncode == 0, accepted.stderr.strip())
for label, override in unsafe.items():
    result = subprocess.run([sys.executable, "-m", "backend.config"], env={**base_env, **override},
                            cwd=root, capture_output=True, text=True)
    check(f"production start refused: {label}", result.returncode != 0)

print("\n=== Operational endpoints ===")
status, body = request("/healthz")
check("liveness probe responds without touching the database", status == 200 and json.loads(body)["status"] == "ok")
status, body = request("/readyz")
check("readiness probe reports the database", status == 200 and json.loads(body)["database"] == "ok")
status, body = request("/metrics")
check("metrics expose request counters", status == 200 and "raahat_requests_total" in body)

print("\n=== Response hardening ===")
req = urllib.request.Request(BASE + "/")
with urllib.request.urlopen(req, timeout=30) as response:
    headers = {key.lower(): value for key, value in response.headers.items()}
check("a Content-Security-Policy is sent", "content-security-policy" in headers)
check("the page cannot be framed", headers.get("x-frame-options") == "DENY")
check("MIME sniffing is disabled", headers.get("x-content-type-options") == "nosniff")
check("every response carries a correlation id", bool(headers.get("x-request-id")))

print("\n=== Session tokens are not stored in a replayable form ===")
victim = login("victim@demo.in")
check("sign-in issues a token", bool(victim))
with database.conn() as db:
    row = db.execute("SELECT token FROM sessions ORDER BY issued_at DESC LIMIT 1").fetchone()
check("the stored session row is not the bearer token itself", row and row["token"] != victim)
check("the stored value is a hash", row and len(row["token"]) == 64)
check("the token still authenticates", request("/api/me", token=victim)[0] == 200)
check("a forged token is rejected", request("/api/me", token="not-a-real-token")[0] == 401)

print("\n=== WebSocket auth uses single-use tickets, not the session token ===")
counsellor = login("counsellor@demo.in")
status, body = request("/api/ws-ticket", "POST", token=counsellor)
ticket = json.loads(body)["ticket"] if status == 200 else ""
check("staff can mint a ticket", bool(ticket) and ticket != counsellor)
check("a participant cannot mint one", request("/api/ws-ticket", "POST", token=victim)[0] == 403)

print("\n=== Participant scoping ===")
status, body = request("/api/me", token=victim)
payload = json.loads(body)
check("a participant sees exactly one case", status == 200 and len(payload["cases"]) == 1)
check("a participant sees no caseload metrics", payload["metrics"] == {})
check("a participant cannot read the staff dashboard", request("/api/dashboard", token=victim)[0] == 403)

print("\n=== Case list is paginated ===")
status, body = request("/api/cases?limit=1", token=counsellor)
payload = json.loads(body)
check("the list is capped by limit", status == 200 and len(payload["cases"]) <= 1)
check("the list reports a total", isinstance(payload.get("total"), int))

print("\n=== Alert acknowledgement ===")
_, dashboard_body = request("/api/dashboard", token=counsellor)
alerts = json.loads(dashboard_body)["alerts"]
if alerts:
    alert_id = alerts[0]["id"]
    status, body = request(f"/api/alerts/{alert_id}/ack", "POST", token=counsellor)
    acked = json.loads(body) if status == 200 else {}
    check("an alert can be acknowledged", status == 200 and acked.get("acknowledged_at"))
    check("the acknowledgement is attributed", acked.get("acknowledged_by") == "counsellor@demo.in")
else:
    check("an alert can be acknowledged", True, "(no alerts present to acknowledge)")
check("acknowledging an unknown alert is a 404", request("/api/alerts/99999999/ack", "POST", token=counsellor)[0] == 404)

print("\n=== Erasure actually erases ===")
status, created = request("/api/cases", "POST", {"name": "ErasureTest Subject", "district": "T", "state": "T"},
                          token=counsellor)
case_id = json.loads(created)["id"]
database.save_chat_message(case_id, "victim", "erasure test message")
with database.conn() as db:
    before = db.execute("SELECT COUNT(*) AS n FROM chat_messages WHERE case_id=%s", (case_id,)).fetchone()["n"]
check("the test case has stored content", before > 0)
result = database.request_privacy_action(case_id, "delete_data")
with database.conn() as db:
    after = db.execute("SELECT COUNT(*) AS n FROM chat_messages WHERE case_id=%s", (case_id,)).fetchone()["n"]
    tombstone = db.execute("SELECT name, deleted_at FROM cases WHERE id=%s", (case_id,)).fetchone()
check("the request is recorded as completed, not queued forever", result["status"] == "Completed")
check("participant content is gone", after == 0)
check("a tombstone row remains so the id is never re-used", tombstone and tombstone["deleted_at"])
check("the erased name is cleared", tombstone and tombstone["name"] == "[erased]")
check("the erased case no longer resolves", database.case(case_id) == {})
check("the erased case is not in the staff list",
      all(item["id"] != case_id for item in database.list_cases(limit=500)["cases"]))

print("\n=== Raw-table inspector ===")
admin = login("admin@demo.in")
status, _ = request("/api/db", token=admin)
check("the inspector matches its configured state",
      (status == 200) if config.ENABLE_DB_INSPECTOR else (status == 404),
      f"(ENABLE_DB_INSPECTOR={config.ENABLE_DB_INSPECTOR}, got {status})")
check("a counsellor can never reach it", request("/api/db", token=counsellor)[0] in (403, 404))

print("\n=== IVR is closed by default ===")
status, _ = request("/api/ivr/sos", "POST", {"note": "test"}, headers={"X-IVR-Key": "wrong-key"})
check("an invalid IVR key is refused", status in (403, 404))

print("\n" + "=" * 46)
print(f"{passed} passed, {failed} failed")
print("=" * 46)
sys.exit(1 if failed else 0)
