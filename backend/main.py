"""Raahat FastAPI demo backend."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import auth
from .database import (
    add_case_note, case, consent_status, create_case, create_followup, dashboard, database_view, feedback_summary,
    engagement_level, get_chat_history, get_session, init_db, list_cases, log_audit, missed_checkins, override_case,
    privacy_export,
    record, request_privacy_action, save_chat_feedback, save_chat_message, set_consent, sos, update_alert,
)
from .ml.generate import SUPPORTS
from .ml.predict import assess, band_for, model_version
from .services import ratelimit, realtime, twilio_ivr
from .services.chatbot import reply, transcribe_audio
from .services.notify import notify_escalation

app = FastAPI(title="Raahat API", version="0.1.0")
ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "raahat.html"
# React, ReactDOM, Babel and Tailwind are vendored into static/vendor/ and served from here,
# so the app renders on a machine with no internet access.
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
# Demo scope: every phone call is treated as the seeded demo case. A real deployment would
# map the caller's number (Twilio sends it as `From`) to a registered case instead.
IVR_CASE_ID = "7934"
MAX_VOICE_BYTES = 5 * 1024 * 1024


def _not_blank(value: str) -> str:
    stripped = value.strip()
    if not stripped: raise ValueError("must not be blank")
    return stripped


class ChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    case_id: str = "7934"

    _clean_message = field_validator("message")(_not_blank)


class ChatFeedbackInput(BaseModel):
    message_id: int = Field(gt=0)
    rating: str = Field(pattern="^(helpful|not_helpful)$")


class ConsentInput(BaseModel):
    granted: bool


class PrivacyRequestInput(BaseModel):
    request_type: str = Field(pattern="^(delete_data)$")


class FollowupInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    due_at: str = Field(min_length=1, max_length=80)
    assigned_to: str = Field(default="Care team", max_length=200)

    _clean_title = field_validator("title")(_not_blank)


class CheckinInput(BaseModel):
    case_id: str = "7934"
    mood: int = Field(ge=0, le=3)
    sleep: int = Field(ge=0, le=3)
    safety: int = Field(ge=0, le=2)
    anxiety: int = Field(ge=0, le=2)
    case_pressure: int = Field(ge=0, le=3)
    support: list[str] = []

    @field_validator("support")
    @classmethod
    def _known_supports_only(cls, value: list[str]) -> list[str]:
        unknown = set(value) - set(SUPPORTS)
        if unknown: raise ValueError(f"unknown support option(s): {sorted(unknown)}")
        return value


class CaseInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    district: str = Field(min_length=1, max_length=120)
    state: str = Field(min_length=1, max_length=120)
    category: str = Field(default="Other", max_length=120)

    _clean_name = field_validator("name")(_not_blank)
    _clean_district = field_validator("district")(_not_blank)
    _clean_state = field_validator("state")(_not_blank)


class AlertUpdateInput(BaseModel):
    status: str | None = Field(default=None, pattern="^(Unresolved|In Progress|Resolved)$")
    assigned_to: str | None = Field(default=None, max_length=200)


class OverrideInput(BaseModel):
    score: int = Field(ge=0, le=100)
    risk_band: str = Field(pattern="^(Stable|Low|Moderate|High|Critical)$")
    note: str = Field(default="", max_length=1000)


class SosInput(BaseModel):
    note: str = Field(default="", max_length=1000)


class NoteInput(BaseModel):
    author: str = Field(default="Care team", max_length=200)
    text: str = Field(min_length=1, max_length=2000)

    _clean_text = field_validator("text")(_not_blank)


class LoginInput(BaseModel):
    email: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


def _own_case_or_403(user: dict, case_id: str) -> None:
    """A victim session may only ever act on their own case; staff can act on any case."""
    if user["role"] == "victim" and case_id != user["case_id"]:
        raise HTTPException(403, "You can only access your own case")


def require_ivr_key(x_ivr_key: str | None = Header(default=None)) -> None:
    """A phone call has no web session/bearer token, so IVR calls (from the local
    FreeSWITCH dialplan) authenticate with a shared secret header instead."""
    expected = os.getenv("IVR_API_KEY", "")
    if not expected or x_ivr_key != expected:
        raise HTTPException(403, "Invalid or missing IVR key")


@app.on_event("startup")
def startup() -> None: init_db()


@app.on_event("startup")
async def startup_realtime() -> None:
    """Capture the running event loop so a High/Critical escalation raised from a
    sync route handler (run in FastAPI's thread pool) can still push to WebSocket
    clients — see services/realtime.py."""
    realtime.set_loop(asyncio.get_event_loop())


@app.get("/")
def frontend(): return FileResponse(FRONTEND)


@app.websocket("/ws/alerts")
async def alerts_stream(websocket: WebSocket, token: str = ""):
    """Live push of new High/Critical alerts to staff dashboards. A browser WebSocket
    cannot set an Authorization header, so the session token travels as a query param
    instead (?token=...) — same bearer token issued by /api/auth/login."""
    session = get_session(token)
    if not session or session["role"] not in ("counsellor", "admin"):
        await websocket.close(code=4401)
        return
    await realtime.register(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        realtime.unregister(websocket)


@app.post("/api/auth/login")
def login(payload: LoginInput, request: Request):
    # Brute-force guard: unauthenticated, so the counter is keyed on the caller's IP, and only
    # FAILED attempts are counted — see services/ratelimit.LIMITS.
    key = ratelimit.client_key(request)
    ratelimit.peek("login", key)
    try:
        return auth.login(payload.email, payload.password)
    except HTTPException as failure:
        if failure.status_code == 401: ratelimit.record("login", key)
        raise


@app.post("/api/auth/logout")
def logout(authorization: str | None = Header(default=None)):
    auth.logout(authorization)
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(auth.require_user)):
    """Same shape as /api/dashboard, but scoped to just the caller's own case for a victim session."""
    d = dashboard()
    if user["role"] != "victim": return d
    cid = user["case_id"]
    return {
        "scope": d["scope"],
        "cases": [c for c in d["cases"] if c["id"] == cid],
        "alerts": [a for a in d["alerts"] if a["case_id"] == cid],
        "events": [e for e in d["events"] if e["case_id"] == cid],
        "interventions": [i for i in d["interventions"] if i["case_id"] == cid],
        "notes": [n for n in d["notes"] if n["case_id"] == cid],
        "metrics": d["metrics"],
    }


@app.get("/api/dashboard")
def get_dashboard(user: dict = Depends(auth.require_staff)):
    log_audit(user["email"], user["role"], "view", "dashboard", "-")
    return dashboard()


@app.get("/api/db")
def get_database_view(user: dict = Depends(auth.require_admin)):
    log_audit(user["email"], user["role"], "view", "raw_db", "-")
    return database_view()


@app.get("/api/cases")
def get_cases(user: dict = Depends(auth.require_staff)): return list_cases()


@app.post("/api/cases")
def add_case(payload: CaseInput, user: dict = Depends(auth.require_staff)):
    created = create_case(payload.name, payload.district, payload.state, payload.category)
    log_audit(user["email"], user["role"], "create", "case", created["id"], payload.name)
    return created


@app.get("/api/cases/{case_id}")
def get_case(case_id: str, user: dict = Depends(auth.require_user)):
    _own_case_or_403(user, case_id)
    found = case(case_id)
    if not found: raise HTTPException(404, "Case not found")
    if user["role"] != "victim": log_audit(user["email"], user["role"], "view", "case", case_id)
    return found


def _chat_payload(payload: ChatInput, user: dict) -> dict:
    case_id = user["case_id"] if user["role"] == "victim" else payload.case_id
    # Keyed on the session, not the IP: one flooding account must not lock out everyone
    # else behind the same NAT/proxy.
    ratelimit.check("chat", user["email"])
    current = case(case_id)
    if not current: raise HTTPException(404, "Case not found")
    # History is loaded from the server-side chat_messages table, not sent by the client —
    # a refresh no longer loses context, and a client can't fabricate prior "assistant" turns.
    history = get_chat_history(case_id)
    result = assess(payload.message, previous_score=current["score"], missed_checkins=missed_checkins(case_id),
                    engagement=engagement_level(case_id))
    bot = reply(payload.message, history, result)
    if bot["urgent"]:
        result["risk_band"], result["score"] = "Critical", max(90, result["score"])
        result["explanations"].insert(0, "Safety router detected an urgent message")
        bot = reply(payload.message, history, result)
    elif bot["score_floor"] > result["score"]:
        # An elevated-but-not-urgent disclosure (e.g. witnessing violence, acute PTSD symptoms).
        # The deterministic router is more reliable here than the small text model, so it sets a
        # floor on the score rather than leaving the case reading Low.
        result["score"] = bot["score_floor"]
        result["risk_band"] = band_for(result["score"])
        result["explanations"].insert(0, f"Safety router detected {bot['topic']} disclosure — score floored at {bot['score_floor']}")
    result["chatbot"] = {key: bot[key] for key in ("intent", "emotion", "language", "semantic_confidence", "provider")}
    alert = record(case_id, "chat", payload.message, result, model_version=model_version())
    save_chat_message(case_id, "victim", payload.message, bot["topic"], bot["urgent"], bot["provider"])
    assistant_message_id = save_chat_message(case_id, "assistant", bot["reply"], bot["topic"], bot["urgent"], bot["provider"])
    if alert: notify_escalation(case_id, current["name"], alert["severity"], alert["reason"])
    return {"assistant": bot["reply"], "message_id": assistant_message_id, "urgent": bot["urgent"],
            "topic": bot["topic"], "intent": bot["intent"], "emotion": bot["emotion"],
            "language": bot["language"], "handoff": bot["handoff"], "provider": bot["provider"], "assessment": result}


@app.post("/api/chat")
def chat(payload: ChatInput, user: dict = Depends(auth.require_user)):
    return _chat_payload(payload, user)


@app.post("/api/chat/stream")
async def chat_stream(payload: ChatInput, user: dict = Depends(auth.require_user)):
    """SSE transport for the same persisted chat flow; safety assessment happens before output."""
    result = _chat_payload(payload, user)

    async def events():
        for word in result["assistant"].split(" "):
            yield f"data: {{\"delta\": {json.dumps(word + ' ')}}}\n\n"
            await asyncio.sleep(.015)
        metadata = {key: value for key, value in result.items() if key != "assistant"}
        yield f"event: meta\ndata: {json.dumps(metadata)}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/voice")
async def voice_note(request: Request, x_voice_consent: str | None = Header(default=None), user: dict = Depends(auth.require_user)):
    """Transcribe one opt-in voice note, then run the existing text safety/chat flow."""
    if user["role"] != "victim":
        raise HTTPException(403, "Voice notes are available to the participant only")
    if x_voice_consent != "yes":
        raise HTTPException(422, "Voice recording consent is required")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type not in {"audio/webm", "audio/ogg", "audio/wav", "audio/mpeg", "audio/mp4"}:
        raise HTTPException(415, "Use a supported audio format")
    try:
        content_length = int(request.headers.get("content-length", "0") or 0)
    except ValueError:
        raise HTTPException(400, "Invalid voice note size")
    if content_length > MAX_VOICE_BYTES:
        raise HTTPException(413, "Voice note must be 5 MB or smaller")
    audio = await request.body()
    if not audio or len(audio) > MAX_VOICE_BYTES:
        raise HTTPException(413 if audio else 422, "Voice note must be 5 MB or smaller" if audio else "Voice note is empty")
    ratelimit.check("voice", user["email"])
    transcript = transcribe_audio(audio, content_type, get_chat_history(user["case_id"]))
    if not transcript:
        raise HTTPException(503, "Voice transcription is unavailable; please type your message")
    result = _chat_payload(ChatInput(message=transcript), user)
    return {**result, "transcript": transcript, "source": "voice"}


@app.post("/api/chat/feedback")
def chat_feedback(payload: ChatFeedbackInput, user: dict = Depends(auth.require_user)):
    # Feedback is participant-only and can only target an assistant reply in their own case.
    if user["role"] != "victim":
        raise HTTPException(403, "Chat feedback is available to the participant only")
    if not save_chat_feedback(user["case_id"], payload.message_id, payload.rating):
        raise HTTPException(404, "Assistant message not found")
    return {"ok": True}


@app.get("/api/analytics/feedback")
def feedback_analytics(user: dict = Depends(auth.require_staff)):
    return feedback_summary()


@app.get("/api/consent")
def get_consent(user: dict = Depends(auth.require_user)):
    return {"monitoring": consent_status(user["case_id"])}


@app.post("/api/consent")
def update_consent(payload: ConsentInput, user: dict = Depends(auth.require_user)):
    if user["role"] != "victim": raise HTTPException(403, "Consent can only be changed by the participant")
    return {"monitoring": set_consent(user["case_id"], payload.granted)}


@app.get("/api/privacy/export")
def export_privacy_data(user: dict = Depends(auth.require_user)):
    if user["role"] != "victim": raise HTTPException(403, "Privacy export is participant-only")
    return privacy_export(user["case_id"])


@app.post("/api/privacy/delete-request")
def delete_request(payload: PrivacyRequestInput, user: dict = Depends(auth.require_user)):
    if user["role"] != "victim": raise HTTPException(403, "Privacy requests are participant-only")
    return request_privacy_action(user["case_id"], payload.request_type)


@app.get("/api/handoffs")
def handoff_queue(user: dict = Depends(auth.require_staff)):
    d = dashboard(); cases = {item["id"]: item for item in d["cases"]}
    return [{**alert, "case": cases.get(alert["case_id"], {})} for alert in d["alerts"] if alert["status"] != "Resolved"]


@app.post("/api/checkins")
def checkin(payload: CheckinInput, user: dict = Depends(auth.require_user)):
    ratelimit.check("checkin", user["email"])
    case_id = user["case_id"] if user["role"] == "victim" else payload.case_id
    current = case(case_id)
    if not current: raise HTTPException(404, "Case not found")
    answers = payload.model_dump(exclude={"case_id", "support"})
    summary = "Guided wellbeing check-in: " + ", ".join(f"{key}={value}" for key, value in answers.items())
    # summary is what gets stored/displayed; None is passed as the message so the generated
    # string is not run through the natural-language text classifier (see ml.predict.assess).
    result = assess(None, answers, current["score"], missed_checkins(case_id), engagement_level(case_id))
    if payload.support: result["interventions"] = sorted(set(result["interventions"] + payload.support))
    alert = record(case_id, "checkin", summary, result, model_version=model_version())
    if alert: notify_escalation(case_id, current["name"], alert["severity"], alert["reason"])
    return result


def _phone_checkin(answers: dict, case_id: str, source: str) -> dict:
    """Shared by both phone transports (FreeSWITCH dialplan and Twilio webhooks): the
    keypad answers go through the exact same scoring/alerting pipeline as a web check-in."""
    current = case(case_id)
    if not current: raise HTTPException(404, "Case not found")
    summary = f"IVR wellbeing check-in ({source}): " + ", ".join(f"{key}={value}" for key, value in answers.items())
    result = assess(None, answers, current["score"], missed_checkins(case_id), engagement_level(case_id))
    alert = record(case_id, "checkin", summary, result, model_version=model_version())
    if alert: notify_escalation(case_id, current["name"], alert["severity"], alert["reason"])
    return result


def _phone_sos(case_id: str, note: str) -> dict:
    """Shared by both phone transports — one keypress, no menu and no ML, straight to Critical."""
    current = case(case_id)
    if not current: raise HTTPException(404, "Case not found")
    updated = sos(case_id, note)
    notify_escalation(case_id, current["name"], "Critical", note)
    return updated


@app.post("/api/ivr/checkin")
def ivr_checkin(payload: CheckinInput, _: None = Depends(require_ivr_key)):
    """Phone-in equivalent of /api/checkins, for a caller with no web session — the
    FreeSWITCH dialplan collects the same answers via DTMF keypad and posts them here."""
    return _phone_checkin(payload.model_dump(exclude={"case_id", "support"}), payload.case_id, "phone")


@app.post("/api/ivr/sos")
def ivr_sos(payload: SosInput, case_id: str = "7934", _: None = Depends(require_ivr_key)):
    """Phone-in equivalent of the one-tap emergency button."""
    return _phone_sos(case_id, payload.note or "Emergency reported via IVR (phone keypad)")


async def _twilio_params(request: Request) -> dict[str, str]:
    """Parse and verify a Twilio webhook. The body is form-encoded, parsed here with the
    stdlib rather than pulling in python-multipart for two routes.

    The signature covers the URL Twilio dialled, which behind ngrok/a proxy is not the URL
    this app sees — so it is rebuilt from the forwarded headers before checking."""
    body = (await request.body()).decode("utf-8", "replace")
    params = {key: values[-1] for key, values in parse_qs(body, keep_blank_values=True).items()}
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    url = f"{scheme}://{host}{request.url.path}" + (f"?{request.url.query}" if request.url.query else "")
    if not twilio_ivr.validate_signature(request.headers.get("x-twilio-signature", ""), url, params):
        raise HTTPException(403, "Invalid Twilio signature")
    return params


def _twiml(xml: str) -> Response:
    return Response(content=xml, media_type="application/xml")


@app.post("/api/twilio/voice")
async def twilio_voice(request: Request):
    """Entry point Twilio hits when someone dials the number — plays the same menu the
    FreeSWITCH dialplan does: 1 for a wellbeing check-in, 9 for emergency."""
    await _twilio_params(request)
    return _twiml(twilio_ivr.gather(twilio_ivr.MENU_PROMPT, "/api/twilio/menu"))


@app.post("/api/twilio/menu")
async def twilio_menu(request: Request):
    digit = (await _twilio_params(request)).get("Digits", "")
    if digit == "9":
        _phone_sos(IVR_CASE_ID, "Emergency reported via IVR (phone keypad)")
        return _twiml(twilio_ivr.say_and_hangup(twilio_ivr.SOS_DONE))
    if digit == "1":
        return _twiml(twilio_ivr.gather(twilio_ivr.QUESTIONS[0][1], "/api/twilio/checkin?step=0"))
    return _twiml(twilio_ivr.say_and_hangup(twilio_ivr.INVALID))


@app.post("/api/twilio/checkin")
async def twilio_checkin(request: Request, step: int = 0, answers: str = ""):
    """One question per request — Twilio is stateless, so the answers collected so far ride
    along in the query string of the next step's action URL."""
    digit = (await _twilio_params(request)).get("Digits", "")
    if not 0 <= step < len(twilio_ivr.QUESTIONS):
        return _twiml(twilio_ivr.say_and_hangup(twilio_ivr.INVALID))
    collected = [value for value in answers.split(",") if value.isdigit()][:step]
    _, prompt, highest = twilio_ivr.QUESTIONS[step]
    if not digit.isdigit() or int(digit) > highest:
        return _twiml(twilio_ivr.gather(prompt, f"/api/twilio/checkin?step={step}&answers={','.join(collected)}"))
    collected.append(digit)
    if step + 1 < len(twilio_ivr.QUESTIONS):
        next_prompt = twilio_ivr.QUESTIONS[step + 1][1]
        return _twiml(twilio_ivr.gather(next_prompt, f"/api/twilio/checkin?step={step + 1}&answers={','.join(collected)}"))
    scored = {name: int(value) for (name, _, _), value in zip(twilio_ivr.QUESTIONS, collected)}
    _phone_checkin(scored, IVR_CASE_ID, "Twilio")
    return _twiml(twilio_ivr.say_and_hangup(twilio_ivr.CHECKIN_DONE))


@app.patch("/api/alerts/{alert_id}")
def patch_alert(alert_id: int, payload: AlertUpdateInput, user: dict = Depends(auth.require_staff)):
    updated = update_alert(alert_id, payload.status, payload.assigned_to)
    if not updated: raise HTTPException(404, "Alert not found")
    log_audit(user["email"], user["role"], "update", "alert", str(alert_id), f"status={payload.status} assigned_to={payload.assigned_to}")
    return updated


@app.patch("/api/cases/{case_id}/override")
def override(case_id: str, payload: OverrideInput, user: dict = Depends(auth.require_staff)):
    if not case(case_id): raise HTTPException(404, "Case not found")
    updated = override_case(case_id, payload.score, payload.risk_band, payload.note)
    log_audit(user["email"], user["role"], "override", "case", case_id, f"score={payload.score} risk_band={payload.risk_band}")
    return updated


@app.post("/api/cases/{case_id}/notes")
def add_note(case_id: str, payload: NoteInput, user: dict = Depends(auth.require_staff)):
    if not case(case_id): raise HTTPException(404, "Case not found")
    note = add_case_note(case_id, payload.author, payload.text)
    log_audit(user["email"], user["role"], "create", "case_note", case_id)
    return note


@app.post("/api/cases/{case_id}/followups")
def add_followup(case_id: str, payload: FollowupInput, user: dict = Depends(auth.require_staff)):
    if not case(case_id): raise HTTPException(404, "Case not found")
    followup = create_followup(case_id, payload.title, payload.due_at, payload.assigned_to or user["name"])
    log_audit(user["email"], user["role"], "create", "followup", case_id, payload.title)
    return followup


@app.post("/api/cases/{case_id}/sos")
def sos_endpoint(case_id: str, payload: SosInput, user: dict = Depends(auth.require_user)):
    resolved_id = user["case_id"] if user["role"] == "victim" else case_id
    _own_case_or_403(user, resolved_id)
    ratelimit.check("sos", user["email"])
    current = case(resolved_id)
    if not current: raise HTTPException(404, "Case not found")
    updated = sos(resolved_id, payload.note)
    notify_escalation(resolved_id, current["name"], "Critical", payload.note or "Emergency button pressed by the individual")
    return updated


@app.get("/api/cases/{case_id}/report", response_class=PlainTextResponse)
def case_report(case_id: str, user: dict = Depends(auth.require_staff)):
    current = case(case_id)
    if not current: raise HTTPException(404, "Case not found")
    d = dashboard()
    case_alerts = [a for a in d["alerts"] if a["case_id"] == case_id]
    case_events = [e for e in d["events"] if e["case_id"] == case_id]
    case_interventions = [i for i in d["interventions"] if i["case_id"] == case_id]
    case_notes = [n for n in d["notes"] if n["case_id"] == case_id]
    lines = [
        f"Raahat case report — {current['name']} (Case #{case_id})",
        f"District / State: {current['district']}, {current['state']}",
        f"Category: {current.get('category') or '—'}",
        f"Current score: {current['score']} / 100  ·  Risk band: {current['risk_band']}",
        f"Last updated: {current['updated_at']}",
        f"Last check-in: {current.get('last_checkin_at') or 'No check-in recorded'}",
        "",
        f"Alerts ({len(case_alerts)}):",
        *([f"  - [{a['severity']}] {a['status']} — {a['reason']}  ({a['created_at']})" for a in case_alerts] or ["  (none)"]),
        "",
        f"Interventions ({len(case_interventions)}):",
        *([f"  - {i['title']} ({i['status']})  ({i['created_at']})" for i in case_interventions] or ["  (none)"]),
        "",
        f"Case notes ({len(case_notes)}):",
        *([f"  - [{n['author']}] {n['text']}  ({n['created_at']})" for n in case_notes] or ["  (none)"]),
        "",
        f"Recent events ({len(case_events)}):",
        *([f"  - [{e['kind']}] {e['content'][:150]}  ({e['created_at']})" for e in case_events] or ["  (none)"]),
        "",
        "This report is generated by a demo system with model-assisted, English-tuned risk assessments.",
        "High/Critical findings and any manual overrides require human review and are not a clinical, legal, or emergency-dispatch determination.",
    ]
    return "\n".join(lines)
