"""Raahat API — HTTP surface, middleware, and application lifecycle."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import auth, config, observability
from .database import (
    CHECKIN_CADENCE_DAYS,
    acknowledge_alert, add_case_note, case, case_events, case_id_for_phone, close_pool, consent_status, conn,
    create_case, create_followup, dashboard, database_view, feedback_summary,
    engagement_level, get_chat_history, get_session, init_db, list_cases, log_audit, missed_checkins, override_case,
    list_rehearsals, participant_view, privacy_export, purge_expired_sessions,
    record, request_privacy_action, save_chat_feedback, save_chat_message, save_rehearsal, set_consent, sos,
    update_alert,
)
from .ml.forecast import forecast_from_events
from .ml.generate import SUPPORTS
from .ml.predict import assess, band_for, model_version
from .services import cache, ratelimit, realtime, speech, twilio_ivr
from .services.chatbot import reply, route, transcribe_audio
from .services.notify import notify_escalation, start_sweeper, stop_sweeper

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "raahat.html"
FRONTEND_PROD = ROOT / "static" / "index.html"
MAX_VOICE_BYTES = 5 * 1024 * 1024
# Which document is served decides which Content-Security-Policy is correct, so both read
# the same flag. Serving the pre-transpiled bundle while still sending the permissive
# development policy would quietly give up the protection the build exists to enable.
SERVE_BUNDLE = FRONTEND_PROD.exists()
# Short-lived, single-use tickets for the alerts WebSocket — see /api/ws-ticket.
WS_TICKET_TTL_SECONDS = 30
_ws_tickets: dict[str, tuple[float, str]] = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup/shutdown. Replaces the deprecated @app.on_event hooks.

    Order matters: configuration is validated before anything touches the database, so a
    misconfigured production process dies immediately with a readable reason instead of
    booting into a half-working state.
    """
    observability.configure()
    config.validate()
    log.info("starting", extra={"app_env": config.APP_ENV, "version": app.version})
    init_db()
    # Captured so a broadcast raised from a sync route handler (running in FastAPI's thread
    # pool) can still reach WebSocket clients — see services/realtime.py.
    realtime.set_loop(asyncio.get_running_loop())
    cache.client()
    realtime.start_subscriber()
    start_sweeper()
    purged = purge_expired_sessions()
    if purged:
        log.info("purged expired sessions", extra={"rows": purged})
    if config.REQUIRE_FRONTEND_BUNDLE and not config.FRONTEND_BUNDLE.exists():
        raise RuntimeError("Frontend bundle static/app.js is missing — run `npm run build` before deploying. "
                           "Set REQUIRE_FRONTEND_BUNDLE=0 only for a development process.")
    try:
        yield
    finally:
        stop_sweeper()
        realtime.stop_subscriber()
        close_pool()
        log.info("stopped")


app = FastAPI(
    title="Raahat API",
    version="1.0.0",
    lifespan=lifespan,
    # Interactive docs enumerate every endpoint and schema. Useful internally, an
    # unnecessary map of the attack surface on a public deployment.
    docs_url="/docs" if config.ENABLE_API_DOCS else None,
    redoc_url="/redoc" if config.ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if config.ENABLE_API_DOCS else None,
)

# Blocks Host-header poisoning: behind a proxy, an attacker-controlled Host would otherwise
# be echoed into absolute URLs (including the Twilio signature check below).
if config.ALLOWED_HOSTS and config.ALLOWED_HOSTS != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.ALLOWED_HOSTS)
# The app serves its own frontend same-origin, so no cross-origin access is the correct
# default; ALLOWED_ORIGINS exists for the case where the frontend is hosted separately.
if config.ALLOWED_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=config.ALLOWED_ORIGINS, allow_credentials=True,
                       allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
                       allow_headers=["Authorization", "Content-Type", "X-Voice-Consent"], max_age=600)
app.add_middleware(GZipMiddleware, minimum_size=1024)

# React, ReactDOM and Tailwind are vendored into static/vendor/ and served from here, so the
# app renders on a machine with no internet access.
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

# Content-Security-Policy: everything the page needs is same-origin, so the policy is a
# strict allowlist with no external script/style/connect sources at all.
#
# The production page (npm run build) contains no inline script of any kind, so script-src
# is a bare 'self' — an injected <script> simply does not execute, which is the single most
# valuable thing a CSP does. The development page is the one that needs the escapes: the
# in-browser Babel path evaluates the JSX source and injects the result as an inline
# script. That is precisely why production does not use it.
_SCRIPT_SRC = "'self'" if SERVE_BUNDLE else "'self' 'unsafe-inline' 'unsafe-eval'"
CSP = (
    "default-src 'self'; "
    f"script-src {_SCRIPT_SRC}; "
    # Tailwind's runtime injects a <style> element, and the page carries inline styles.
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self' data:; media-src 'self' blob:; "
    "connect-src 'self'; "
    "frame-ancestors 'self'; base-uri 'none'; form-action 'self'; object-src 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    # The browser features this app uses: the microphone, for opt-in voice notes, and the
    # camera, for the opt-in pulse reading during courtroom rehearsal (frames are analysed in
    # the page and never uploaded). Everything else is switched off so a compromised
    # dependency cannot reach for it.
    "Permissions-Policy": "geolocation=(), camera=(self), payment=(), usb=(), microphone=(self)",
}


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Correlate, time, and secure every response.

    One middleware rather than three: they all need the same request/response pair, and
    each additional middleware is another full ASGI wrapper on the hot path.
    """
    request_id = request.headers.get("x-request-id", "")[:64] or observability.new_request_id()
    token = observability.request_id_var.set(request_id)
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        # Never let a stack trace or a database error message reach the client: they leak
        # schema and library versions, and on this app the message could contain case data.
        observability.incr("raahat_requests_total", method=request.method, status="5xx")
        log.exception("unhandled error", extra={"path": request.url.path, "method": request.method})
        response = JSONResponse({"detail": "Internal server error", "request_id": request_id}, status_code=500)
    finally:
        observability.request_id_var.reset(token)
    duration = time.monotonic() - started
    # The raw path would create an unbounded label set (one series per case id); the route
    # template collapses them to one series per endpoint.
    route = request.scope.get("route")
    path_template = getattr(route, "path", "unmatched")
    observability.incr("raahat_requests_total", method=request.method,
                       path=path_template, status=f"{response.status_code // 100}xx")
    observability.observe("raahat_request_seconds", duration, method=request.method, path=path_template)
    if duration > 2:
        log.warning("slow request", extra={"path": request.url.path, "seconds": round(duration, 3)})
    response.headers.update(SECURITY_HEADERS)
    response.headers["X-Request-ID"] = request_id
    if config.ENABLE_HSTS:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


def _not_blank(value: str) -> str:
    stripped = value.strip()
    if not stripped: raise ValueError("must not be blank")
    return stripped


class ChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    case_id: str = "7934"

    _clean_message = field_validator("message")(_not_blank)


class DemoAssessInput(BaseModel):
    message: str = Field(min_length=1, max_length=500)

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
    # Shown on the participant's home screen as a note from their care team.
    share: bool = False

    _clean_text = field_validator("text")(_not_blank)


class RehearsalCheckInput(BaseModel):
    text: str = Field(min_length=1, max_length=2000)

    _clean_text = field_validator("text")(_not_blank)


class RehearsalPoint(BaseModel):
    t: int = Field(ge=0, le=7200)
    bpm: int | None = Field(default=None, ge=30, le=220)
    level: int = Field(ge=0, le=4)
    stress: bool = False


class RehearsalSessionInput(BaseModel):
    sensor: str = Field(pattern="^(camera|simulated|self_report)$")
    duration_seconds: int = Field(ge=0, le=7200)
    questions_answered: int = Field(ge=0, le=200)
    levels_completed: int = Field(ge=0, le=4)
    calm_level: int = Field(ge=0, le=4)
    baseline_bpm: int | None = Field(default=None, ge=30, le=220)
    peak_bpm: int | None = Field(default=None, ge=30, le=220)
    recovery_seconds: int | None = Field(default=None, ge=0, le=7200)
    spikes: int = Field(ge=0, le=200)
    pauses: int = Field(ge=0, le=200)
    self_rating_avg: float | None = Field(default=None, ge=1, le=5)
    # Downsampled by the client; capped so one session cannot become an unbounded JSON blob.
    timeline: list[RehearsalPoint] = Field(default=[], max_length=400)

    @field_validator("calm_level")
    @classmethod
    def _calm_within_completed(cls, value: int, info) -> int:
        completed = info.data.get("levels_completed")
        if completed is not None and value > completed:
            raise ValueError("calm_level cannot exceed levels_completed")
        return value


class LoginInput(BaseModel):
    email: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


def _own_case_or_403(user: dict, case_id: str) -> None:
    """A victim session may only ever act on their own case; staff can act on any case."""
    if user["role"] == "victim" and case_id != user["case_id"]:
        raise HTTPException(403, "You can only access your own case")


def require_ivr_key(x_ivr_key: str | None = Header(default=None)) -> None:
    """A phone call has no web session/bearer token, so IVR calls (from the local
    FreeSWITCH dialplan) authenticate with a shared secret header instead.

    Compared in constant time: a plain `!=` on a secret leaks its prefix to a caller who
    can measure response times, and this key is enough to write check-ins and fire
    Critical escalations. The whole route family is off unless ENABLE_IVR is set.
    """
    if not config.ENABLE_IVR:
        raise HTTPException(404, "Not found")
    expected = config.IVR_API_KEY
    if not expected or not secrets.compare_digest(x_ivr_key or "", expected):
        log.warning("rejected IVR request with invalid key")
        raise HTTPException(403, "Invalid or missing IVR key")


@app.get("/", include_in_schema=False)
def frontend():
    """Serve the pre-transpiled page in production, the Babel-transformed source locally.

    `npm run build` writes static/index.html + static/app.js; the development path keeps
    raahat.html directly editable. Cache-Control is no-store because the HTML names the
    content-hashed bundle — the bundle itself is immutable and cached hard by the static
    mount, the document that points at it must never be.
    """
    path = FRONTEND_PROD if SERVE_BUNDLE else FRONTEND
    return FileResponse(path, headers={"Cache-Control": "no-store"})


@app.get("/demo", include_in_schema=False)
def demo_frame():
    """Laptop-friendly phone mockup: the app inside an iPhone frame (same-origin iframe)."""
    return FileResponse(ROOT / "static" / "demo.html", headers={"Cache-Control": "no-store"})


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Liveness: is this process running? Deliberately touches nothing external — a
    liveness probe that fails when the database blips gets the container killed during a
    failover it would otherwise have ridden out."""
    return {"status": "ok", "version": app.version, "env": config.APP_ENV}


@app.get("/readyz", include_in_schema=False)
def readyz():
    """Readiness: can this process serve traffic? Checks the dependency it cannot work
    without, so a pod with a dead database is taken out of the load balancer rotation
    instead of serving errors."""
    try:
        with conn() as db:
            db.execute("SELECT 1").fetchone()
    except Exception:
        log.exception("readiness check failed")
        return JSONResponse({"status": "unavailable", "database": "unreachable"}, status_code=503)
    return {"status": "ready", "database": "ok", "pool": database_pool_stats(),
            "redis": cache.available(), "ws_clients": realtime.connection_count()}


@app.get("/metrics", include_in_schema=False)
def metrics(request: Request):
    """Prometheus scrape target. Not public: metrics leak caseload volume and traffic
    shape, so it is restricted to the private networks a scraper runs on."""
    client_host = request.client.host if request.client else ""
    if config.IS_PRODUCTION and not _is_private_address(client_host):
        raise HTTPException(404, "Not found")
    return PlainTextResponse(observability.render_metrics(), media_type="text/plain; version=0.0.4")


def _is_private_address(host: str) -> bool:
    import ipaddress
    try:
        return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def database_pool_stats() -> dict:
    from .database import pool_stats
    return pool_stats()


@app.post("/api/ws-ticket")
def ws_ticket(user: dict = Depends(auth.require_staff)):
    """Mint a single-use, 30-second ticket for the alerts WebSocket.

    A browser WebSocket cannot send an Authorization header, so the old flow put the
    session bearer token in the query string — where it lands in proxy access logs, browser
    history and any Referer. A ticket is worthless once used or 30 seconds old, so the same
    leak costs nothing.
    """
    ticket = secrets.token_urlsafe(24)
    _ws_tickets[ticket] = (time.monotonic() + WS_TICKET_TTL_SECONDS, user["token"])
    # Opportunistic sweep; the dict only ever holds seconds' worth of entries.
    stale = [key for key, (expiry, _) in _ws_tickets.items() if expiry < time.monotonic()]
    for key in stale:
        _ws_tickets.pop(key, None)
    return {"ticket": ticket, "expires_in": WS_TICKET_TTL_SECONDS}


@app.websocket("/ws/alerts")
async def alerts_stream(websocket: WebSocket, ticket: str = ""):
    """Live push of new High/Critical alerts to staff dashboards.

    Authenticated with a one-shot ticket from /api/ws-ticket rather than the bearer token
    itself. Tickets are held in this process's memory, which is correct even multi-worker:
    the ticket is redeemed by the very next request from the same client, and a miss just
    means the client mints another.
    """
    expiry_and_token = _ws_tickets.pop(ticket, None)
    session = None
    if expiry_and_token and expiry_and_token[0] >= time.monotonic():
        session = get_session(expiry_and_token[1])
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
        result = auth.login(payload.email, payload.password)
    except HTTPException as failure:
        if failure.status_code == 401:
            ratelimit.record("login", key)
            # Failed sign-ins are the first signal of credential stuffing; the email is
            # logged (it is the account being attacked, not content) but never the password.
            log.warning("failed sign-in", extra={"email": payload.email[:120], "source": key})
            observability.incr("raahat_logins_total", outcome="failed")
        raise
    observability.incr("raahat_logins_total", outcome="ok")
    log.info("sign-in", extra={"role": result["role"], "source": key})
    return result


@app.post("/api/auth/logout")
def logout(authorization: str | None = Header(default=None)):
    auth.logout(authorization)
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(auth.require_user)):
    """Same shape as /api/dashboard, but scoped to just the caller's own case for a
    participant session — and scoped in SQL, so no other case's rows are ever loaded to
    answer it (see database.participant_view)."""
    if user["role"] == "victim":
        return participant_view(user["case_id"])
    return dashboard()


@app.get("/api/dashboard")
def get_dashboard(user: dict = Depends(auth.require_staff)):
    log_audit(user["email"], user["role"], "view", "dashboard", "-")
    return dashboard()


@app.get("/api/db")
def get_database_view(user: dict = Depends(auth.require_admin)):
    """Raw-table inspector.

    This returns participant chat transcripts and every other table in the schema. It is a
    development and internal-review tool, off unless ENABLE_DB_INSPECTOR is set, and every
    use is audited — an administrator reading a participant's messages wholesale is exactly
    the access a safeguarding audit needs to be able to reconstruct.
    """
    if not config.ENABLE_DB_INSPECTOR:
        raise HTTPException(404, "Not found")
    log_audit(user["email"], user["role"], "view", "raw_db", "-")
    log.warning("raw database inspector used", extra={"actor": user["email"]})
    return database_view()


@app.get("/api/cases")
def get_cases(limit: int = 100, offset: int = 0, search: str = "", risk_band: str = "",
              user: dict = Depends(auth.require_staff)):
    if risk_band and risk_band not in ("Stable", "Low", "Moderate", "High", "Critical"):
        raise HTTPException(422, "Unknown risk band")
    return list_cases(limit=limit, offset=max(0, offset), search=search[:120], risk_band=risk_band)


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
    if alert: notify_escalation(case_id, current["name"], alert["severity"], alert["reason"], alert["id"])
    observability.incr("raahat_assessments_total", channel="chat", band=result["risk_band"])
    return {"assistant": bot["reply"], "message_id": assistant_message_id, "urgent": bot["urgent"],
            "topic": bot["topic"], "intent": bot["intent"], "emotion": bot["emotion"],
            "language": bot["language"], "handoff": bot["handoff"], "provider": bot["provider"], "assessment": result}


@app.post("/api/chat")
def chat(payload: ChatInput, user: dict = Depends(auth.require_user)):
    return _chat_payload(payload, user)


@app.post("/api/demo/assess")
def demo_assess(payload: DemoAssessInput, request: Request):
    """The landing page's "See how Raahat listens" preview: the real text model on a typed
    sentence, with no login. Nothing is persisted and no alert is raised — there is no case
    behind it — so it is rate limited per address and returns only the assessment."""
    ratelimit.check("demo", ratelimit.client_key(request))
    result = assess(payload.message)
    # The same deterministic safety router the chat applies, so a self-harm disclosure the
    # small text model cannot read still comes back Critical here, exactly as it would in chat.
    # route() is the local regex/semantic layer only — no LLM reply is generated.
    routed = route(payload.message)
    if routed["urgent"]:
        result["risk_band"], result["score"] = "Critical", max(90, result["score"])
        result["explanations"].insert(0, "Safety router detected an urgent message")
    elif routed.get("score_floor", 0) > result["score"]:
        result["score"] = routed["score_floor"]
        result["risk_band"] = band_for(result["score"])
        result["explanations"].insert(0, f"Safety router detected {routed['topic']} disclosure — score floored at {routed['score_floor']}")
    observability.incr("raahat_assessments_total", channel="demo", band=result["risk_band"])
    # Mirrors the alert rule in database.record, which this endpoint never calls.
    would_alert = result["risk_band"] in ("High", "Critical") or result["escalation_probability"] >= 65
    return {**{key: result[key] for key in ("score", "risk_band", "text_band", "confidence", "escalation_probability",
                                           "explanations", "interventions")},
            "urgent": routed["urgent"], "topic": routed["topic"], "would_alert": would_alert}


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
    # Local faster-whisper first, so the audio does not leave this server; Gemini only if the
    # local model is not installed or fails. Both are blocking (model inference / HTTP), so
    # they run in a worker thread instead of stalling the event loop for every other request.
    local = await asyncio.to_thread(speech.transcribe_local, audio, content_type)
    transcript, engine = local["transcript"], "local"
    if not transcript:
        transcript = await asyncio.to_thread(transcribe_audio, audio, content_type, get_chat_history(user["case_id"]))
        engine = "gemini"
    if not transcript:
        raise HTTPException(503, "Voice transcription is unavailable; please type your message")
    observability.incr("raahat_voice_notes_total", engine=engine)
    result = _chat_payload(ChatInput(message=transcript), user)
    # The acoustic signals are context for the care team only; they do not change the score.
    return {**result, "transcript": transcript, "source": "voice", "transcribed_by": engine, "acoustic": local["acoustic"]}


class TTSInput(BaseModel):
    text: str = Field(min_length=1, max_length=1000)

    _clean_text = field_validator("text")(_not_blank)


@app.post("/api/tts")
def text_to_speech(payload: TTSInput, user: dict = Depends(auth.require_user)):
    """Server-side speech for an assistant reply, when a TTS engine (chatterbox) is installed.
    503 otherwise, and the frontend speaks with the browser's own voices instead."""
    ratelimit.check("voice", user["email"])
    audio = speech.synthesize_speech(payload.text)
    if not audio:
        raise HTTPException(503, "Server speech synthesis is not installed")
    return Response(content=audio, media_type="audio/wav")


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


@app.post("/api/rehearsal/check")
def rehearsal_check(payload: RehearsalCheckInput, user: dict = Depends(auth.require_user)):
    """Safety screen for a typed courtroom-rehearsal answer.

    Practice answers are rehearsal, not disclosure, so they are not stored and do not move the
    distress score. The one exception is the same one the chat makes: if the deterministic
    safety router reads an answer as urgent (self-harm, a threat happening now, ...), that is a
    real disclosure that happened to be typed into the practice box, and it is escalated
    through the exact path a chat message would take.
    """
    if user["role"] != "victim":
        raise HTTPException(403, "Rehearsal is available to the participant only")
    ratelimit.check("chat", user["email"])
    routed = route(payload.text)
    if not routed["urgent"]:
        return {"urgent": False, "topic": routed["topic"]}
    case_id = user["case_id"]
    current = case(case_id)
    if not current: raise HTTPException(404, "Case not found")
    result = assess(payload.text, previous_score=current["score"], missed_checkins=missed_checkins(case_id),
                    engagement=engagement_level(case_id))
    result["risk_band"], result["score"] = "Critical", max(90, result["score"])
    result["explanations"].insert(0, "Safety router detected an urgent message during courtroom rehearsal")
    alert = record(case_id, "rehearsal", payload.text, result, model_version=model_version())
    if alert: notify_escalation(case_id, current["name"], alert["severity"], alert["reason"], alert["id"])
    observability.incr("raahat_assessments_total", channel="rehearsal", band=result["risk_band"])
    return {"urgent": True, "topic": routed["topic"], "reply": reply(payload.text, [], result)["reply"]}


@app.post("/api/rehearsal/sessions")
def add_rehearsal_session(payload: RehearsalSessionInput, user: dict = Depends(auth.require_user)):
    if user["role"] != "victim":
        raise HTTPException(403, "Rehearsal is available to the participant only")
    if not case(user["case_id"]): raise HTTPException(404, "Case not found")
    return save_rehearsal(user["case_id"], payload.model_dump())


@app.get("/api/rehearsal/sessions")
def own_rehearsal_sessions(user: dict = Depends(auth.require_user)):
    if user["role"] != "victim":
        raise HTTPException(403, "Use /api/cases/{case_id}/rehearsals for a case you are supporting")
    return list_rehearsals(user["case_id"])


@app.get("/api/cases/{case_id}/rehearsals")
def case_rehearsals(case_id: str, user: dict = Depends(auth.require_staff)):
    if not case(case_id): raise HTTPException(404, "Case not found")
    log_audit(user["email"], user["role"], "view", "rehearsals", case_id)
    return list_rehearsals(case_id)


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
    if alert: notify_escalation(case_id, current["name"], alert["severity"], alert["reason"], alert["id"])
    observability.incr("raahat_assessments_total", channel="checkin", band=result["risk_band"])
    return result


def _phone_checkin(answers: dict, case_id: str, source: str) -> dict:
    """Shared by both phone transports (FreeSWITCH dialplan and Twilio webhooks): the
    keypad answers go through the exact same scoring/alerting pipeline as a web check-in."""
    current = case(case_id)
    if not current: raise HTTPException(404, "Case not found")
    summary = f"IVR wellbeing check-in ({source}): " + ", ".join(f"{key}={value}" for key, value in answers.items())
    result = assess(None, answers, current["score"], missed_checkins(case_id), engagement_level(case_id))
    alert = record(case_id, "checkin", summary, result, model_version=model_version())
    if alert: notify_escalation(case_id, current["name"], alert["severity"], alert["reason"], alert["id"])
    observability.incr("raahat_assessments_total", channel=source, band=result["risk_band"])
    return result


def _phone_sos(case_id: str, note: str) -> dict:
    """Shared by both phone transports — one keypress, no menu and no ML, straight to Critical."""
    current = case(case_id)
    if not current: raise HTTPException(404, "Case not found")
    updated = sos(case_id, note)
    notify_escalation(case_id, current["name"], "Critical", note, updated.get("alert_id"))
    return updated


def _case_for_caller(caller: str) -> str:
    """Resolve the phone number a call came from to the case it belongs to.

    Every phone entry point used to write to one hardcoded case id, which at any scale
    above one participant means recording one person's distress answers — and raising one
    person's emergency — against another person's record. An unregistered number is
    refused rather than defaulted; IVR_FALLBACK_CASE_ID exists only for a single-tenant
    pilot line and is unset by default.
    """
    resolved = case_id_for_phone(caller) or config.IVR_FALLBACK_CASE_ID
    if not resolved:
        log.warning("IVR call from unregistered number", extra={"digits": len(caller or "")})
        raise HTTPException(404, "This number is not registered for phone check-ins")
    return resolved


@app.post("/api/ivr/checkin")
def ivr_checkin(payload: CheckinInput, caller: str = "", _: None = Depends(require_ivr_key)):
    """Phone-in equivalent of /api/checkins, for a caller with no web session — the
    FreeSWITCH dialplan collects the same answers via DTMF keypad and posts them here.
    `caller` is the calling number the dialplan captured."""
    case_id = _case_for_caller(caller or payload.case_id)
    return _phone_checkin(payload.model_dump(exclude={"case_id", "support"}), case_id, "phone")


@app.post("/api/ivr/sos")
def ivr_sos(payload: SosInput, caller: str = "", _: None = Depends(require_ivr_key)):
    """Phone-in equivalent of the one-tap emergency button."""
    return _phone_sos(_case_for_caller(caller), payload.note or "Emergency reported via IVR (phone keypad)")


async def _twilio_params(request: Request) -> dict[str, str]:
    """Parse and verify a Twilio webhook. The body is form-encoded, parsed here with the
    stdlib rather than pulling in python-multipart for two routes.

    The signature covers the URL Twilio dialled, which behind ngrok/a proxy is not the URL
    this app sees — so it is rebuilt from the forwarded headers before checking."""
    if not config.ENABLE_TWILIO:
        raise HTTPException(404, "Not found")
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
    params = await _twilio_params(request)
    digit = params.get("Digits", "")
    if digit == "9":
        _phone_sos(_case_for_caller(params.get("From", "")), "Emergency reported via IVR (phone keypad)")
        return _twiml(twilio_ivr.say_and_hangup(twilio_ivr.SOS_DONE))
    if digit == "1":
        return _twiml(twilio_ivr.gather(twilio_ivr.QUESTIONS[0][1], "/api/twilio/checkin?step=0"))
    return _twiml(twilio_ivr.say_and_hangup(twilio_ivr.INVALID))


@app.post("/api/twilio/checkin")
async def twilio_checkin(request: Request, step: int = 0, answers: str = ""):
    """One question per request — Twilio is stateless, so the answers collected so far ride
    along in the query string of the next step's action URL."""
    params = await _twilio_params(request)
    digit = params.get("Digits", "")
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
    _phone_checkin(scored, _case_for_caller(params.get("From", "")), "Twilio")
    return _twiml(twilio_ivr.say_and_hangup(twilio_ivr.CHECKIN_DONE))


@app.post("/api/alerts/{alert_id}/ack")
def ack_alert(alert_id: int, user: dict = Depends(auth.require_staff)):
    """Claim an alert. Time from creation to this call is the service level the care team
    is actually accountable for, and is what the escalation sweeper measures against."""
    updated = acknowledge_alert(alert_id, user["email"])
    if not updated: raise HTTPException(404, "Alert not found")
    log_audit(user["email"], user["role"], "acknowledge", "alert", str(alert_id))
    log.info("alert acknowledged", extra={"alert_id": alert_id, "case_id": updated.get("case_id")})
    return updated


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


@app.get("/api/cases/{case_id}/forecast")
def case_forecast(case_id: str, user: dict = Depends(auth.require_staff)):
    """Where this case's distress score is heading over the next check-in cycles.

    Projected from the case's own stored assessments (see ml.forecast) — a decision-support
    signal for triage, never an automated decision. A case without enough recorded history
    returns available=false with the reason, so the UI can say so instead of guessing."""
    if not case(case_id): raise HTTPException(404, "Case not found")
    log_audit(user["email"], user["role"], "view", "forecast", case_id)
    return forecast_from_events(case_events(case_id), CHECKIN_CADENCE_DAYS)


@app.post("/api/cases/{case_id}/notes")
def add_note(case_id: str, payload: NoteInput, user: dict = Depends(auth.require_staff)):
    if not case(case_id): raise HTTPException(404, "Case not found")
    note = add_case_note(case_id, payload.author, payload.text, payload.share)
    log_audit(user["email"], user["role"], "create", "case_note", case_id, "shared with participant" if payload.share else "")
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
    notify_escalation(resolved_id, current["name"], "Critical",
                      payload.note or "Emergency button pressed by the individual", updated.get("alert_id"))
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
