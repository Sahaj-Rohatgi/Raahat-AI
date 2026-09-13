"""MySQL store for the hackathon demo.

Uses a plain PyMySQL connection per call (no pooling) — fine at demo scale, and keeps
the code readable. All tables live in the database named by DATABASE_URL so the whole
schema is inspectable in a GUI client (MySQL Workbench, DBeaver, etc.) instead of only
via the /api/db endpoint below.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import pymysql
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

DATABASE_URL = os.getenv("DATABASE_URL", "mysql://root:root@localhost:3306/raahat_dw")
CHECKIN_CADENCE_DAYS = 7
ENGAGEMENT_WINDOW_DAYS = 14
MAX_MISSED_CHECKINS = 10  # keep in step with ml.generate.MAX_MISSED_CHECKINS


def now() -> str: return datetime.now(timezone.utc).isoformat()


def _connect_kwargs() -> dict:
    """Turns the mysql://user:pass@host:port/dbname URL into PyMySQL connect args."""
    url = urlparse(DATABASE_URL)
    return {
        "host": url.hostname or "localhost",
        "port": url.port or 3306,
        "user": unquote(url.username or "root"),
        "password": unquote(url.password or ""),
        "database": (url.path or "/").lstrip("/") or "raahat_dw",
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
        "autocommit": False,
    }


class _Connection:
    """Thin wrapper so `with conn() as db: db.execute(...)` returns a cursor
    that is iterable and has fetchone()/fetchall(), and the block commits on clean exit.
    A fresh cursor per execute() so an earlier result set is never clobbered by a later
    statement inside the same block."""

    def __init__(self, raw: pymysql.connections.Connection):
        self._raw = raw

    def cursor(self): return self._raw.cursor()

    def execute(self, sql: str, params: tuple | list = ()):
        cur = self._raw.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self): self._raw.commit()

    def __enter__(self): return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._raw.commit()
            else:
                self._raw.rollback()
        finally:
            self._raw.close()
        return False


def conn() -> _Connection:
    return _Connection(pymysql.connect(**_connect_kwargs()))


def _json(value):
    """MySQL hands JSON columns back as a string; decode to the dict the callers expect."""
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _decode(row: dict) -> dict:
    row = dict(row)
    if "assessment" in row:
        row["assessment"] = _json(row["assessment"])
    return row


# Each statement is executed on its own — MySQL's protocol (unlike sqlite3's
# executescript) does not allow multiple commands in a single execute() call.
# Indexed/primary-key text columns are VARCHAR: MySQL cannot index a bare TEXT column.
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS cases (
        id VARCHAR(64) PRIMARY KEY, name TEXT, district TEXT, state TEXT, score INTEGER,
        risk_band TEXT, category VARCHAR(64) DEFAULT 'Other', updated_at TEXT, created_at TEXT,
        last_checkin_at TEXT
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS events (
        id INT AUTO_INCREMENT PRIMARY KEY, case_id VARCHAR(64), kind VARCHAR(32), content TEXT,
        assessment JSON, model_version TEXT, created_at TEXT
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS alerts (
        id INT AUTO_INCREMENT PRIMARY KEY, case_id VARCHAR(64), severity TEXT, reason TEXT,
        status VARCHAR(32) DEFAULT 'Unresolved', assigned_to TEXT, resolved_at TEXT, created_at TEXT
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS interventions (
        id INT AUTO_INCREMENT PRIMARY KEY, case_id VARCHAR(64), title TEXT,
        status VARCHAR(32) DEFAULT 'Recommended',
        assigned_to TEXT, due_at TEXT, completed_at TEXT, created_at TEXT
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS case_notes (
        id INT AUTO_INCREMENT PRIMARY KEY, case_id VARCHAR(64), author TEXT, text TEXT, created_at TEXT
    ) ENGINE=InnoDB""",
    # Login accounts. Demo scope: password stored as plain text (no hashing) by explicit
    # request — do not carry this column choice into anything beyond this demo.
    """CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY, email VARCHAR(255) UNIQUE NOT NULL, password TEXT NOT NULL,
        role TEXT NOT NULL, name TEXT NOT NULL, case_id VARCHAR(64), is_active BOOLEAN DEFAULT TRUE,
        created_at TEXT, last_login_at TEXT
    ) ENGINE=InnoDB""",
    # Server-side sessions replace the old in-memory dict: they survive a reload/restart
    # and are visible/auditable in the database itself.
    """CREATE TABLE IF NOT EXISTS sessions (
        token VARCHAR(255) PRIMARY KEY, user_id INT NOT NULL,
        issued_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, expires_at DATETIME NOT NULL,
        revoked_at DATETIME NULL,
        CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users(id)
    ) ENGINE=InnoDB""",
    # Both sides of every chat turn, stored server-side so a page refresh doesn't lose
    # context and a client can't inject fabricated "assistant" history into the prompt.
    """CREATE TABLE IF NOT EXISTS chat_messages (
        id INT AUTO_INCREMENT PRIMARY KEY, case_id VARCHAR(64), sender VARCHAR(32) NOT NULL,
        message TEXT NOT NULL, topic TEXT, urgent BOOLEAN, provider TEXT, created_at TEXT
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS chat_feedback (
        id INT AUTO_INCREMENT PRIMARY KEY, case_id VARCHAR(64) NOT NULL, chat_message_id INT NOT NULL,
        rating VARCHAR(16) NOT NULL CHECK (rating IN ('helpful', 'not_helpful')), created_at TEXT
    ) ENGINE=InnoDB""",
    # Who looked at / changed what. Only staff (counsellor/admin) actions are logged —
    # a victim acting on their own case is not "access to someone else's data".
    """CREATE TABLE IF NOT EXISTS audit_log (
        id INT AUTO_INCREMENT PRIMARY KEY, actor_email TEXT, actor_role TEXT, action TEXT,
        entity_type TEXT, entity_id TEXT, details TEXT, created_at TEXT
    ) ENGINE=InnoDB""",
    # Case-level ownership (separate from per-alert assigned_to). Schema only for now —
    # not yet wired to an endpoint.
    """CREATE TABLE IF NOT EXISTS case_assignments (
        id INT AUTO_INCREMENT PRIMARY KEY, case_id VARCHAR(64), user_email TEXT, role_on_case TEXT,
        assigned_at TEXT, unassigned_at TEXT
    ) ENGINE=InnoDB""",
    # Consent to monitoring/data-sharing. Schema only for now — not yet wired to an endpoint.
    """CREATE TABLE IF NOT EXISTS consent (
        id INT AUTO_INCREMENT PRIMARY KEY, case_id VARCHAR(64), consent_type VARCHAR(64), granted BOOLEAN,
        granted_at TEXT, withdrawn_at TEXT, method TEXT
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS followups (
        id INT AUTO_INCREMENT PRIMARY KEY, case_id VARCHAR(64) NOT NULL, title TEXT NOT NULL, due_at TEXT,
        status VARCHAR(32) NOT NULL DEFAULT 'Open', assigned_to TEXT, created_at TEXT
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS privacy_requests (
        id INT AUTO_INCREMENT PRIMARY KEY, case_id VARCHAR(64) NOT NULL, request_type VARCHAR(64) NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'Open', created_at TEXT
    ) ENGINE=InnoDB""",
]

# MySQL has no "ADD COLUMN IF NOT EXISTS", so each entry is (table, column, definition)
# and _add_column_if_missing() checks information_schema before altering.
MIGRATIONS = [
    ("cases", "category", "VARCHAR(64) DEFAULT 'Other'"),
    ("cases", "created_at", "TEXT"),
    ("cases", "last_checkin_at", "TEXT"),
    ("alerts", "assigned_to", "TEXT"),
    ("alerts", "resolved_at", "TEXT"),
    ("events", "model_version", "TEXT"),
    ("interventions", "assigned_to", "TEXT"),
    ("interventions", "due_at", "TEXT"),
    ("interventions", "completed_at", "TEXT"),
]

# Demo accounts — same three logins the README documents. Password stored in plain text
# by explicit request for this hackathon build; do not do this outside a demo.
DEMO_USERS = [
    ("victim@demo.in", "demo123", "victim", "Asha K.", "7934"),
    ("counsellor@demo.in", "demo123", "counsellor", "Dr. Meera Sharma", None),
    ("admin@demo.in", "demo123", "admin", "Admin User", None),
]


def _add_column_if_missing(cur, table: str, column: str, definition: str) -> None:
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() "
        "AND table_name=%s AND column_name=%s",
        (table, column),
    )
    if not cur.fetchone():
        cur.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")


def init_db() -> None:
    with conn() as db:
        cur = db.cursor()
        for statement in SCHEMA:
            cur.execute(statement)
        for table, column, definition in MIGRATIONS:
            _add_column_if_missing(cur, table, column, definition)
        cur.execute(
            "INSERT IGNORE INTO cases (id,name,district,state,score,risk_band,updated_at,category,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("7934", "Asha K.", "South Delhi", "Delhi", 62, "Moderate", now(), "Witness Intimidation", now()),
        )
        cur.execute("UPDATE cases SET district='South Delhi', state='Delhi' WHERE id='7934' AND district='Demo District'")
        created = now()
        for email, password, role, name, case_id in DEMO_USERS:
            cur.execute(
                "INSERT IGNORE INTO users (email,password,role,name,case_id,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                (email, hash_password(password), role, name, case_id, created),
            )
        # Upgrade rows written before password hashing existed: any stored value that is not
        # in the scrypt format is still the plain-text password, so re-hash it in place. A
        # database created after this change never matches, so this costs one query.
        cur.execute("SELECT id, password FROM users WHERE password NOT LIKE %s", ("scrypt$%",))
        legacy_rows = cur.fetchall()
        for row in legacy_rows:
            cur.execute("UPDATE users SET password=%s WHERE id=%s", (hash_password(row["password"]), row["id"]))


# ---------------------------------------------------------------------------
# Auth: users + sessions
# ---------------------------------------------------------------------------

# Passwords are stored as scrypt hashes ("scrypt$<salt hex>$<hash hex>"), never in plain
# text. Parameters are scrypt's interactive-login defaults; the salt is per-user, so two
# accounts sharing a password do not share a hash.
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(stored: str, password: str) -> bool:
    """Constant-time check against a stored scrypt hash. A row that is not in the scrypt
    format is a pre-hashing legacy row and is rejected — init_db() upgrades those on
    startup, so this only ever fires for a row written by hand."""
    try:
        scheme, salt_hex, digest_hex = (stored or "").split("$")
        if scheme != "scrypt": return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def get_user_by_email(email: str) -> dict | None:
    with conn() as db:
        row = db.execute("SELECT * FROM users WHERE email=%s AND is_active=1", (email.strip().lower(),)).fetchone()
    return row


def touch_login(user_id: int) -> None:
    with conn() as db:
        db.execute("UPDATE users SET last_login_at=%s WHERE id=%s", (now(), user_id))


def create_session(user_id: int, ttl_seconds: int) -> str:
    import secrets
    token = secrets.token_urlsafe(24)
    with conn() as db:
        db.execute(
            "INSERT INTO sessions (token,user_id,expires_at) VALUES (%s,%s, DATE_ADD(NOW(), INTERVAL %s SECOND))",
            (token, user_id, ttl_seconds),
        )
    return token


def get_session(token: str) -> dict | None:
    with conn() as db:
        row = db.execute(
            """SELECT u.id AS user_id, u.email, u.role, u.name, u.case_id
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token=%s AND s.revoked_at IS NULL AND s.expires_at > NOW() AND u.is_active=1""",
            (token,),
        ).fetchone()
    return row


def revoke_session(token: str) -> None:
    with conn() as db:
        db.execute("UPDATE sessions SET revoked_at=NOW() WHERE token=%s AND revoked_at IS NULL", (token,))


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def log_audit(actor_email: str, actor_role: str, action: str, entity_type: str, entity_id: str, details: str = "") -> None:
    with conn() as db:
        db.execute(
            "INSERT INTO audit_log (actor_email,actor_role,action,entity_type,entity_id,details,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (actor_email, actor_role, action, entity_type, entity_id, details, now()),
        )


# ---------------------------------------------------------------------------
# Chat message history (server-side, so a page refresh or a tampered client
# payload can't erase or fabricate what the assistant actually said)
# ---------------------------------------------------------------------------

def save_chat_message(case_id: str, sender: str, message: str, topic: str = "", urgent: bool = False, provider: str = "") -> int:
    with conn() as db:
        cur = db.execute(
            "INSERT INTO chat_messages (case_id,sender,message,topic,urgent,provider,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (case_id, sender, message, topic, urgent, provider, now()),
        )
        new_id = cur.lastrowid
    return new_id


def save_chat_feedback(case_id: str, chat_message_id: int, rating: str) -> bool:
    """Store one explicit RLHF signal only for an assistant reply in the caller's case."""
    with conn() as db:
        message = db.execute(
            "SELECT 1 FROM chat_messages WHERE id=%s AND case_id=%s AND sender='assistant'",
            (chat_message_id, case_id),
        ).fetchone()
        if not message:
            return False
        db.execute(
            "INSERT INTO chat_feedback(case_id,chat_message_id,rating,created_at) VALUES (%s,%s,%s,%s)",
            (case_id, chat_message_id, rating, now()),
        )
    return True


def feedback_summary() -> dict:
    with conn() as db:
        rows = db.execute("SELECT rating, COUNT(*) count FROM chat_feedback GROUP BY rating").fetchall()
        by_intent = db.execute("""SELECT m.topic, f.rating, COUNT(*) count FROM chat_feedback f
            JOIN chat_messages m ON m.id=f.chat_message_id GROUP BY m.topic, f.rating ORDER BY count DESC""").fetchall()
    totals = {row["rating"]: row["count"] for row in rows}
    total = sum(totals.values())
    return {"total": total, "helpful": totals.get("helpful", 0), "not_helpful": totals.get("not_helpful", 0),
            "helpful_rate": round(totals.get("helpful", 0) * 100 / total) if total else 0,
            "by_intent": [dict(row) for row in by_intent]}


def consent_status(case_id: str) -> bool:
    with conn() as db:
        row = db.execute("SELECT granted FROM consent WHERE case_id=%s AND consent_type='monitoring' ORDER BY id DESC LIMIT 1", (case_id,)).fetchone()
    return True if row is None else bool(row["granted"])


def set_consent(case_id: str, granted: bool) -> bool:
    with conn() as db:
        db.execute("INSERT INTO consent(case_id,consent_type,granted,granted_at,withdrawn_at,method) VALUES (%s,'monitoring',%s,%s,%s,'app')",
                   (case_id, granted, now() if granted else None, None if granted else now()))
    return granted


def create_followup(case_id: str, title: str, due_at: str, assigned_to: str) -> dict:
    with conn() as db:
        cur = db.execute("INSERT INTO followups(case_id,title,due_at,assigned_to,created_at) VALUES (%s,%s,%s,%s,%s)",
                         (case_id, title, due_at, assigned_to, now()))
        new_id = cur.lastrowid
    return {"id": new_id, "case_id": case_id, "title": title, "due_at": due_at, "assigned_to": assigned_to, "status": "Open"}


def privacy_export(case_id: str) -> dict:
    with conn() as db:
        messages = [dict(row) for row in db.execute("SELECT sender,message,created_at FROM chat_messages WHERE case_id=%s ORDER BY id", (case_id,))]
        checkins = [_decode(row) for row in db.execute("SELECT content,assessment,created_at FROM events WHERE case_id=%s AND kind='checkin' ORDER BY id", (case_id,))]
    return {"case_id": case_id, "messages": messages, "checkins": checkins, "monitoring_consent": consent_status(case_id)}


def request_privacy_action(case_id: str, request_type: str) -> dict:
    with conn() as db:
        cur = db.execute("INSERT INTO privacy_requests(case_id,request_type,created_at) VALUES (%s,%s,%s)",
                         (case_id, request_type, now()))
        new_id = cur.lastrowid
        status = db.execute("SELECT status FROM privacy_requests WHERE id=%s", (new_id,)).fetchone()["status"]
    return {"id": new_id, "case_id": case_id, "request_type": request_type, "status": status}


def get_chat_history(case_id: str, limit: int = 20) -> list[dict]:
    """Returns the last `limit` turns in {"role","content"} shape, oldest first — the
    shape backend.services.chatbot.route()/_gemini_reply() already expect."""
    with conn() as db:
        rows = db.execute(
            "SELECT sender, message FROM chat_messages WHERE case_id=%s ORDER BY id DESC LIMIT %s",
            (case_id, limit),
        ).fetchall()
    rows = list(rows)
    rows.reverse()
    return [{"role": "user" if row["sender"] == "victim" else "assistant", "content": row["message"]} for row in rows]


# ---------------------------------------------------------------------------
# Cases / events / alerts / interventions / notes
# ---------------------------------------------------------------------------

def case(case_id: str = "7934") -> dict:
    with conn() as db:
        row = db.execute("SELECT * FROM cases WHERE id=%s", (case_id,)).fetchone()
    return dict(row) if row else {}


def list_cases() -> list[dict]:
    with conn() as db:
        return [dict(row) for row in db.execute("SELECT * FROM cases ORDER BY score DESC")]


def create_case(name: str, district: str, state: str, category: str = "Other") -> dict:
    """Allocates the next numeric id. Concurrent creates retry on a collision rather
    than surface it as a 500 (mirrors the previous sqlite behaviour)."""
    created = now()
    last_error: pymysql.err.IntegrityError | None = None
    for _ in range(8):
        try:
            with conn() as db:
                existing_ids = [row["id"] for row in db.execute("SELECT id FROM cases")]
                numeric = [int(i) for i in existing_ids if i.isdigit()]
                new_id = str(max(numeric) + 1) if numeric else "1000"
                db.execute(
                    "INSERT INTO cases (id,name,district,state,score,risk_band,updated_at,category,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (new_id, name, district, state, 0, "Stable", created, category, created),
                )
            return case(new_id)
        except pymysql.err.IntegrityError as exc:
            last_error = exc
    raise last_error


def missed_checkins(case_id: str) -> int:
    """How many check-in cycles (CHECKIN_CADENCE_DAYS each) have been skipped since the last one."""
    current = case(case_id)
    if not current: return 0
    reference = current["last_checkin_at"] or current["created_at"]
    if not reference: return 0
    elapsed_days = (datetime.now(timezone.utc) - datetime.fromisoformat(reference)).total_seconds() / 86400
    # Cap matches ml.generate.MAX_MISSED_CHECKINS — the training data spans the same range,
    # so a value produced here is always one the model has actually seen.
    return max(0, min(MAX_MISSED_CHECKINS, int(elapsed_days // CHECKIN_CADENCE_DAYS)))


def engagement_level(case_id: str) -> int:
    """How engaged this case has been recently, on the same 0-4 scale the models were
    trained on (0 = no contact at all, 4 = in touch several times). Derived from how many
    events (check-ins, chat messages, SOS) the case produced in the last
    ENGAGEMENT_WINDOW_DAYS. Previously this feature was hardcoded to 3 at inference time,
    so the trained model's engagement input never varied — it is now a real signal."""
    with conn() as db:
        rows = db.execute(
            "SELECT created_at FROM events WHERE case_id=%s ORDER BY id DESC LIMIT 50", (case_id,)
        ).fetchall()
    cutoff = datetime.now(timezone.utc) - timedelta(days=ENGAGEMENT_WINDOW_DAYS)
    recent = 0
    for row in rows:
        try:
            if datetime.fromisoformat(row["created_at"]) >= cutoff: recent += 1
        except (TypeError, ValueError):
            continue
    return max(0, min(4, recent))


def record(case_id: str, kind: str, content: str, assessment: dict, model_version: str = "") -> dict | None:
    """Persist an event/score update and return {severity, reason} if it triggered a new alert, else None."""
    created = now()
    alert_info = None
    with conn() as db:
        db.execute(
            "INSERT INTO events(case_id,kind,content,assessment,model_version,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (case_id, kind, content, json.dumps(assessment), model_version, created),
        )
        if kind == "checkin":
            db.execute("UPDATE cases SET score=%s, risk_band=%s, updated_at=%s, last_checkin_at=%s WHERE id=%s", (assessment["score"], assessment["risk_band"], created, created, case_id))
        else:
            db.execute("UPDATE cases SET score=%s, risk_band=%s, updated_at=%s WHERE id=%s", (assessment["score"], assessment["risk_band"], created, case_id))
        if assessment["risk_band"] in ("High", "Critical") or assessment["escalation_probability"] >= 65:
            severity = "Critical" if assessment["risk_band"] == "Critical" else "High"
            reason = "; ".join(assessment["explanations"][:2])
            db.execute("INSERT INTO alerts(case_id,severity,reason,created_at) VALUES (%s,%s,%s,%s)", (case_id, severity, reason, created))
            alert_info = {"severity": severity, "reason": reason}
        for title in assessment["interventions"]:
            db.execute("INSERT INTO interventions(case_id,title,created_at) VALUES (%s,%s,%s)", (case_id, title, created))
    return alert_info


def override_case(case_id: str, score: int, risk_band: str, note: str = "") -> dict:
    """A human care-team override of the AI assessment; logged as its own event kind for audit."""
    created = now()
    overridden = {"score": score, "risk_band": risk_band, "escalation_probability": 0,
                  "interventions": [], "explanations": ["Manually overridden by care team"] + ([note] if note else [])}
    with conn() as db:
        db.execute("UPDATE cases SET score=%s, risk_band=%s, updated_at=%s WHERE id=%s", (score, risk_band, created, case_id))
        db.execute(
            "INSERT INTO events(case_id,kind,content,assessment,created_at) VALUES (%s,%s,%s,%s,%s)",
            (case_id, "override", note or "Manual review override", json.dumps(overridden), created),
        )
    return case(case_id)


def add_case_note(case_id: str, author: str, text: str) -> dict:
    created = now()
    with conn() as db:
        cur = db.execute(
            "INSERT INTO case_notes(case_id,author,text,created_at) VALUES (%s,%s,%s,%s)",
            (case_id, author, text, created),
        )
        new_id = cur.lastrowid
    return {"id": new_id, "case_id": case_id, "author": author, "text": text, "created_at": created}


def sos(case_id: str, note: str = "") -> dict:
    """A direct emergency-button press: skips the ML assessment entirely and escalates immediately."""
    created = now()
    reason = note or "Emergency button pressed by the individual"
    overridden = {"score": 100, "risk_band": "Critical", "escalation_probability": 100, "interventions": ["Counselling"], "explanations": [reason]}
    with conn() as db:
        db.execute("UPDATE cases SET score=100, risk_band='Critical', updated_at=%s WHERE id=%s", (created, case_id))
        db.execute(
            "INSERT INTO events(case_id,kind,content,assessment,created_at) VALUES (%s,%s,%s,%s,%s)",
            (case_id, "sos", reason, json.dumps(overridden), created),
        )
        db.execute("INSERT INTO alerts(case_id,severity,reason,created_at) VALUES (%s,%s,%s,%s)", (case_id, "Critical", reason, created))
    return case(case_id)


def update_alert(alert_id: int, status: str | None = None, assigned_to: str | None = None) -> dict:
    with conn() as db:
        row = db.execute("SELECT * FROM alerts WHERE id=%s", (alert_id,)).fetchone()
        if not row: return {}
        fields, values = [], []
        if status is not None:
            fields.append("status=%s"); values.append(status)
            fields.append("resolved_at=%s"); values.append(now() if status == "Resolved" else None)
        if assigned_to is not None:
            fields.append("assigned_to=%s"); values.append(assigned_to)
        if fields:
            values.append(alert_id)
            db.execute(f"UPDATE alerts SET {','.join(fields)} WHERE id=%s", values)
        updated = db.execute("SELECT * FROM alerts WHERE id=%s", (alert_id,)).fetchone()
    return dict(updated)


def dashboard() -> dict:
    with conn() as db:
        cases = [dict(row) for row in db.execute("SELECT * FROM cases ORDER BY score DESC")]
        alerts = [dict(row) for row in db.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 50")]
        events = [_decode(row) for row in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 30")]
        interventions = [dict(row) for row in db.execute("SELECT * FROM interventions ORDER BY id DESC LIMIT 40")]
        notes = [dict(row) for row in db.execute("SELECT * FROM case_notes ORDER BY id DESC LIMIT 50")]
        followups = [dict(row) for row in db.execute("SELECT * FROM followups ORDER BY id DESC LIMIT 50")]
    metrics = {
        "total_cases": len(cases),
        "open_alerts": sum(1 for alert in alerts if alert["status"] == "Unresolved"),
        "high_risk": sum(1 for item in cases if item["risk_band"] in ("High", "Critical")),
        "moderate_risk": sum(1 for item in cases if item["risk_band"] == "Moderate"),
        "avg_distress": round(sum(item["score"] for item in cases) / max(len(cases), 1)),
    }
    return {"scope": {"district": "South Delhi", "state": "Delhi", "country": "India"}, "cases": cases, "alerts": alerts, "events": events, "interventions": interventions, "notes": notes, "followups": followups, "feedback": feedback_summary(), "metrics": metrics}


# Columns that must never leave the server, even for an admin reading the raw-table
# inspector: a live session token is a bearer credential (anyone holding it *is* that
# user) and a password is a credential too. Both are masked rather than dropped so the
# schema still reads honestly in the UI.
REDACTED_COLUMNS = {
    "users": {"password": lambda value: "********"},
    "sessions": {"token": lambda value: f"...{str(value)[-4:]}" if value else value},
}


def _redact(table: str, row: dict) -> dict:
    rules = REDACTED_COLUMNS.get(table)
    if not rules: return row
    return {key: (rules[key](value) if key in rules else value) for key, value in row.items()}


def database_view() -> dict:
    tables = {}
    with conn() as db:
        names = [row["table_name"] for row in db.execute(
            "SELECT table_name AS table_name FROM information_schema.tables WHERE table_schema=DATABASE() "
            "AND table_type='BASE TABLE' ORDER BY table_name"
        )]
        for name in names:
            columns = [row["column_name"] for row in db.execute(
                "SELECT column_name AS column_name FROM information_schema.columns WHERE table_schema=DATABASE() "
                "AND table_name=%s ORDER BY ordinal_position", (name,)
            )]
            order_clause = "ORDER BY id DESC" if "id" in columns else ""
            rows = [_redact(name, _decode(row)) for row in db.execute(f'SELECT * FROM `{name}` {order_clause} LIMIT 50')]
            count = db.execute(f'SELECT COUNT(*) count FROM `{name}`').fetchone()["count"]
            tables[name] = {"columns": columns, "rows": rows, "count": count}
    return {"path": DATABASE_URL.split("@")[-1], "tables": tables}
