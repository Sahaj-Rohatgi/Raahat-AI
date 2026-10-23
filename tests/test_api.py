"""Backend API tests: auth, access control, validation, and the concurrency fix.

Run against a live server (see tests/README.md):
    python3 -m uvicorn backend.main:app --port 8001    # terminal 1
    python3 tests/test_api.py                          # terminal 2

Uses only the standard library for HTTP calls; imports backend.database (pymysql) only
for the test-cleanup helper, so it must run with the same venv as the server.
"""
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import database  # noqa: E402  (needs sys.path tweak above)
from backend.database import conn  # noqa: E402

BASE = os.getenv("RAAHAT_TEST_BASE", "http://127.0.0.1:8001")

passed, failed = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK   {label}")
    else:
        failed += 1
        print(f"  FAIL {label} {('- ' + detail) if detail else ''}")


def call(path: str, body=None, method=None, token=None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token: headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method or ("POST" if body is not None else "GET"))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            try: return response.status, json.loads(raw)
            except json.JSONDecodeError: return response.status, raw.decode()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try: return exc.code, json.loads(raw)
        except json.JSONDecodeError: return exc.code, raw.decode()


def stream_call(path: str, body: dict, token: str) -> tuple[int, str]:
    request = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


# The number the IVR tests call from. It is linked to the demo case below; an unlinked
# number is refused, which is the point of the mapping.
TEST_CALLER = "+919999000001"


def form_call(path: str, fields: dict[str, str], signed: bool = True, caller: str | None = TEST_CALLER) -> tuple[int, str]:
    """POST a Twilio-shaped webhook: form-encoded body, carrying the same HMAC-SHA1
    signature Twilio sends. The server now fails closed without a token, so the suite
    must run with TWILIO_AUTH_TOKEN set."""
    fields = dict(fields)
    if caller:
        fields.setdefault("From", caller)
    body = urllib.parse.urlencode(fields).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if token and signed:
        payload = BASE + path + "".join(name + fields[name] for name in sorted(fields))
        digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
        headers["X-Twilio-Signature"] = base64.b64encode(digest).decode()
    request = urllib.request.Request(BASE + path, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def login(email: str, password: str = "demo123") -> str | None:
    status, payload = call("/api/auth/login", {"email": email, "password": password})
    return payload.get("token") if status == 200 else None


def cleanup(name_prefix: str) -> None:
    """Remove cases this test file created, so the demo database stays clean."""
    with conn() as db:
        rows = db.execute("SELECT id FROM cases WHERE name LIKE %s", (name_prefix + "%",)).fetchall()
        for row in rows:
            case_id = row["id"]
            for table in ("chat_messages", "events", "alerts", "interventions", "case_notes"):
                db.execute(f"DELETE FROM {table} WHERE case_id=%s", (case_id,))
            db.execute("DELETE FROM cases WHERE id=%s", (case_id,))
    print(f"\n(cleaned up {len(rows)} test case(s))")


print("\n=== Authentication ===")
check("unauthenticated request is rejected", call("/api/dashboard")[0] == 401)
check("wrong password is rejected", call("/api/auth/login", {"email": "admin@demo.in", "password": "nope"})[0] == 401)
check("garbage token is rejected", call("/api/dashboard", token="garbage")[0] == 401)

admin = login("admin@demo.in")
counsellor = login("counsellor@demo.in")
victim = login("victim@demo.in")
check("admin can log in", admin is not None)
check("counsellor can log in", counsellor is not None)
check("victim can log in", victim is not None)

print("\n=== Role-based access control ===")
check("staff can read the dashboard", call("/api/dashboard", token=admin)[0] == 200)
check("victim CANNOT read the full dashboard", call("/api/dashboard", token=victim)[0] == 403)
check("victim CANNOT view another case", call("/api/cases/7935", token=victim)[0] == 403)
check("victim CAN view their own case", call("/api/cases/7934", token=victim)[0] == 200)
check("victim CANNOT override an assessment", call("/api/cases/7934/override", {"score": 1, "risk_band": "Stable"}, "PATCH", victim)[0] == 403)
check("victim CANNOT create a case", call("/api/cases", {"name": "X", "district": "Y", "state": "Z"}, token=victim)[0] == 403)

status, payload = call("/api/me", token=victim)
check("victim /api/me is scoped to their own case only",
      status == 200 and [c["id"] for c in payload["cases"]] == ["7934"],
      f"saw {[c['id'] for c in payload.get('cases', [])]}")

# A victim posting someone else's case_id must be silently pinned to their own case.
status, payload = call("/api/chat", {"message": "I feel okay today", "case_id": "7935"}, token=victim)
check("victim chat cannot be redirected to another case", status == 200)

print("\n=== Chat streaming and feedback ===")
status, stream = stream_call("/api/chat/stream", {"message": "mujhe bahut tension ho rahi hai"}, victim)
meta = re.search(r"event: meta\ndata: (.+)", stream)
metadata = json.loads(meta.group(1)) if meta else {}
check("Hinglish chat streams a Hinglish reply", status == 200 and '"delta"' in stream and metadata.get("language") == "hinglish")
check("streamed assistant reply receives a feedback id", isinstance(metadata.get("message_id"), int))
check("helpful feedback is accepted", call("/api/chat/feedback", {"message_id": metadata.get("message_id", 0), "rating": "helpful"}, token=victim)[0] == 200)

print("\n=== Input validation ===")
check("missing fields rejected", call("/api/cases", {}, token=admin)[0] == 422)
check("empty name rejected", call("/api/cases", {"name": "", "district": "D", "state": "S"}, token=admin)[0] == 422)
check("whitespace-only name rejected", call("/api/cases", {"name": "   ", "district": "D", "state": "S"}, token=admin)[0] == 422)
check("whitespace-only chat message rejected", call("/api/chat", {"message": "   ", "case_id": "7934"}, token=counsellor)[0] == 422)
check("over-long chat message rejected", call("/api/chat", {"message": "a" * 5000, "case_id": "7934"}, token=counsellor)[0] == 422)
check("out-of-range check-in value rejected",
      call("/api/checkins", {"case_id": "7934", "mood": 99, "sleep": 0, "safety": 0, "anxiety": 0, "case_pressure": 0}, token=counsellor)[0] == 422)
check("unknown support option rejected",
      call("/api/checkins", {"case_id": "7934", "mood": 1, "sleep": 1, "safety": 0, "anxiety": 1, "case_pressure": 1,
                             "support": ["<script>alert(1)</script>"]}, token=counsellor)[0] == 422)
check("valid support option accepted",
      call("/api/checkins", {"case_id": "7934", "mood": 1, "sleep": 1, "safety": 0, "anxiety": 1, "case_pressure": 1,
                             "support": ["Counselling"]}, token=counsellor)[0] == 200)
check("invalid risk band rejected", call("/api/cases/7934/override", {"score": 50, "risk_band": "Extreme"}, "PATCH", counsellor)[0] == 422)
check("out-of-range score rejected", call("/api/cases/7934/override", {"score": 500, "risk_band": "Low"}, "PATCH", counsellor)[0] == 422)

print("\n=== Public landing-page demo ===")
status, demo = call("/api/demo/assess", {"message": "I feel calm today and I talked to my family."})
check("demo assess works without a login", status == 200 and demo.get("would_alert") is False, str(demo))
status, demo = call("/api/demo/assess", {"message": "I don't want to live anymore."})
check("demo applies the chat safety router to self-harm",
      status == 200 and demo["urgent"] and demo["risk_band"] == "Critical" and demo["would_alert"], str(demo))
check("demo rejects a blank message", call("/api/demo/assess", {"message": "   "})[0] == 422)
check("demo caps message length", call("/api/demo/assess", {"message": "a" * 501})[0] == 422)

print("\n=== Not-found handling ===")
check("unknown case returns 404", call("/api/cases/99999", token=admin)[0] == 404)
check("unknown alert returns 404", call("/api/alerts/999999", {"status": "Resolved"}, "PATCH", admin)[0] == 404)
check("report for unknown case returns 404", call("/api/cases/99999/report", token=admin)[0] == 404)

print("\n=== Concurrency: 20 simultaneous case creations must not collide ===")
def create(index: int) -> tuple[int, str | None]:
    status, payload = call("/api/cases", {"name": f"ConcurrencyTest {index}", "district": "T", "state": "T"}, token=admin)
    return status, payload.get("id") if isinstance(payload, dict) else None

with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
    results = list(pool.map(create, range(20)))
ids = [case_id for status, case_id in results if status == 200]
check("all 20 concurrent creates succeeded", all(status == 200 for status, _ in results), f"statuses: {[s for s, _ in results]}")
check("all 20 got unique case IDs", len(set(ids)) == len(ids) == 20, f"{len(set(ids))} unique of {len(ids)}")

print("\n=== Case notes persistence ===")
status, created = call("/api/cases", {"name": "NotesTest Case", "district": "T", "state": "T"}, token=counsellor)
note_case = created["id"]
call(f"/api/cases/{note_case}/notes", {"text": "Persisted note from the test suite"}, token=counsellor)
_, dash = call("/api/dashboard", token=counsellor)
check("note is stored and returned by the API",
      any(n["case_id"] == note_case and "Persisted note" in n["text"] for n in dash["notes"]))

call("/api/cases/7934/notes", {"text": "Internal: discussed safety plan"}, token=counsellor)
call("/api/cases/7934/notes", {"text": "You did really well today, Asha.", "share": True}, token=counsellor)
_, mine = call("/api/me", token=victim)
check("participant sees a note shared with them", any("did really well" in n["text"] for n in mine["notes"]))
check("participant never sees an internal case note", not any("Internal:" in n["text"] for n in mine["notes"]))

print("\n=== Report export ===")
status, report = call(f"/api/cases/{note_case}/report", token=counsellor)
check("report generates for a case with no events", status == 200 and "Raahat case report" in report)
check("report includes the case note", "Persisted note from the test suite" in report)

print("\n=== Emergency (SOS) path ===")
status, payload = call(f"/api/cases/{note_case}/sos", {"note": "Test suite SOS"}, token=counsellor)
check("SOS escalates the case to Critical", status == 200 and payload["risk_band"] == "Critical" and payload["score"] == 100)

print("\n=== Twilio phone IVR (TwiML webhooks) ===")
# A phone check-in is written against the case the calling number belongs to, so the test
# number has to be registered first — exactly as a real participant's would be.
database.link_phone(TEST_CALLER, "7934", "test-suite")

check("an unregistered caller is refused rather than defaulted to someone else's case",
      form_call("/api/twilio/menu", {"Digits": "9"}, caller="+919999999999")[0] == 404)

status, twiml = form_call("/api/twilio/voice", {"CallSid": "CAtest"})
check("call entry point returns a TwiML menu",
      status == 200 and "<Gather" in twiml and 'action="/api/twilio/menu"' in twiml)

# Walk the keypad check-in: press 1, then answer all five questions. Twilio is stateless,
# so each step carries the answers so far in the next action URL.
status, twiml = form_call("/api/twilio/menu", {"Digits": "1"})
check("menu digit 1 starts the check-in", status == 200 and "step=0" in twiml)

status, twiml = form_call("/api/twilio/checkin?step=0", {"Digits": "7"})
check("out-of-range digit re-asks the same question", status == 200 and "step=0" in twiml)

answers, twiml = "", ""
for step, digit in enumerate(["1", "2", "1", "1", "2"]):
    status, twiml = form_call(f"/api/twilio/checkin?step={step}&answers={answers}", {"Digits": digit})
    answers = ",".join(filter(None, [answers, digit]))
check("five keypad answers complete the check-in", status == 200 and "check in has been recorded" in twiml)

_, dash = call("/api/dashboard", token=counsellor)
check("phone check-in is scored and stored like a web one",
      any(e["case_id"] == "7934" and e["kind"] == "checkin" and "(Twilio)" in e["content"] for e in dash["events"]))

status, twiml = form_call("/api/twilio/menu", {"Digits": "9"})
check("menu digit 9 escalates to Critical",
      status == 200 and "emergency alert has been sent" in twiml
      and call("/api/cases/7934", token=counsellor)[1]["risk_band"] == "Critical")

check("an unsigned webhook is rejected",
      form_call("/api/twilio/menu", {"Digits": "9"}, signed=False)[0] == 403)

print("\nCourtroom rehearsal")
rehearsal = {"sensor": "camera", "duration_seconds": 420, "questions_answered": 12, "levels_completed": 4,
             "calm_level": 3, "baseline_bpm": 74, "peak_bpm": 109, "recovery_seconds": 40, "spikes": 1,
             "pauses": 0, "self_rating_avg": None,
             "timeline": [{"t": t, "bpm": 74 + t // 30, "level": 1 + min(3, t // 105), "stress": False} for t in range(0, 420, 2)]}
check("staff cannot submit a rehearsal session", call("/api/rehearsal/sessions", rehearsal, token=counsellor)[0] == 403)
status, saved = call("/api/rehearsal/sessions", rehearsal, token=victim)
created_rehearsals = [saved["id"]] if status == 200 else []
# calm through round 3 (60) + 12 questions (10) + settled inside a minute (10)
check("a rehearsal session is saved with a server-computed readiness",
      status == 200 and saved["readiness"] == 80 and saved["case_id"] == "7934", str(saved))
check("calm level above completed rounds is rejected",
      call("/api/rehearsal/sessions", {**rehearsal, "levels_completed": 2}, token=victim)[0] == 422)
check("an oversized rehearsal timeline is rejected",
      call("/api/rehearsal/sessions", {**rehearsal, "timeline": rehearsal["timeline"] * 3}, token=victim)[0] == 422)
check("an unknown sensor is rejected",
      call("/api/rehearsal/sessions", {**rehearsal, "sensor": "smartwatch"}, token=victim)[0] == 422)
status, own = call("/api/rehearsal/sessions", token=victim)
check("participant can list their own rehearsals",
      status == 200 and any(r["id"] in created_rehearsals and len(r["timeline"]) == 210 for r in own))
status, staff_view = call("/api/cases/7934/rehearsals", token=counsellor)
check("counsellor sees the case's rehearsals", status == 200 and any(r["id"] in created_rehearsals for r in staff_view))
check("participant cannot use the staff rehearsal route", call("/api/cases/7934/rehearsals", token=victim)[0] == 403)
check("privacy export includes rehearsal sessions",
      any(r["id"] in created_rehearsals for r in call("/api/privacy/export", token=victim)[1].get("rehearsals", [])))

before_messages = database.get_chat_history("7934", limit=200)
status, verdict = call("/api/rehearsal/check", {"text": "I was standing near the gate, about ten metres away"}, token=victim)
check("an ordinary practice answer is not urgent", status == 200 and verdict["urgent"] is False)
check("a practice answer is not written to the chat history",
      database.get_chat_history("7934", limit=200) == before_messages)
status, verdict = call("/api/rehearsal/check", {"text": "I want to hurt myself after this"}, token=victim)
_, dash = call("/api/dashboard", token=counsellor)
check("an urgent practice answer escalates like a chat message",
      status == 200 and verdict["urgent"] is True and verdict.get("reply")
      and any(a["case_id"] == "7934" and a["severity"] == "Critical" and "rehearsal" in a["reason"] for a in dash["alerts"]))
check("staff cannot use the rehearsal safety check",
      call("/api/rehearsal/check", {"text": "hello"}, token=counsellor)[0] == 403)

with urllib.request.urlopen(BASE + "/healthz", timeout=10) as response:
    check("camera is permitted for this origin only", "camera=(self)" in response.headers.get("Permissions-Policy", ""))

if created_rehearsals:
    with conn() as db:
        db.execute("DELETE FROM rehearsal_sessions WHERE id IN (%s)" % ",".join(["%s"] * len(created_rehearsals)),
                   tuple(created_rehearsals))

cleanup("ConcurrencyTest")
cleanup("NotesTest")

print(f"\n{'=' * 46}\n{passed} passed, {failed} failed\n{'=' * 46}")
sys.exit(1 if failed else 0)
