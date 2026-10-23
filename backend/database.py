"""MySQL store.

Connections come from a bounded pool (see `_Pool`): a connection-per-call is fine at demo
traffic but collapses under real load, because MySQL charges a TCP handshake plus auth for
every request and caps concurrent connections well below the number of in-flight requests
a thread-pooled FastAPI app will attempt.

All tables live in the database named by DATABASE_URL.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse

import pymysql

from . import config

log = logging.getLogger(__name__)

DATABASE_URL = config.DATABASE_URL
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


class _Pool:
    """Bounded connection pool.

    Borrowed connections are pinged before use (`ping(reconnect=True)`) so a link the
    server has already closed — MySQL's `wait_timeout` reaps idle connections, and so do
    proxies — is transparently re-established rather than surfacing as a random
    OperationalError on an unrelated request. Connections older than MAX_AGE_SECONDS are
    retired on return, which keeps the pool from pinning a database that is being failed
    over or rotated behind a proxy.

    Waiting for a free slot is bounded: exhausting the pool raises rather than queueing
    forever, so a stalled database shows up as fast 503s instead of a hung process.
    """

    MAX_AGE_SECONDS = 1800

    def __init__(self, size: int, timeout: float):
        self._idle: queue.LifoQueue = queue.LifoQueue(maxsize=size)
        self._timeout = timeout
        self._size = size
        self._created = 0
        self._lock = threading.Lock()

    def _new(self) -> tuple[pymysql.connections.Connection, float]:
        return pymysql.connect(**_connect_kwargs()), time.monotonic()

    def acquire(self) -> tuple[pymysql.connections.Connection, float]:
        try:
            raw, born = self._idle.get_nowait()
        except queue.Empty:
            with self._lock:
                may_create = self._created < self._size
                if may_create:
                    self._created += 1
            if may_create:
                try:
                    return self._new()
                except Exception:
                    with self._lock:
                        self._created -= 1
                    raise
            try:
                raw, born = self._idle.get(timeout=self._timeout)
            except queue.Empty as empty:
                raise RuntimeError("Database connection pool exhausted") from empty
        try:
            raw.ping(reconnect=True)
        except Exception:
            self._discard()
            return self._new_counted()
        return raw, born

    def _new_counted(self) -> tuple[pymysql.connections.Connection, float]:
        with self._lock:
            self._created += 1
        try:
            return self._new()
        except Exception:
            with self._lock:
                self._created -= 1
            raise

    def _discard(self) -> None:
        with self._lock:
            self._created -= 1

    def release(self, raw: pymysql.connections.Connection, born: float, healthy: bool) -> None:
        if not healthy or time.monotonic() - born > self.MAX_AGE_SECONDS:
            self._discard()
            try:
                raw.close()
            except Exception:
                pass
            return
        try:
            self._idle.put_nowait((raw, born))
        except queue.Full:
            self._discard()
            try:
                raw.close()
            except Exception:
                pass

    def close_all(self) -> None:
        while True:
            try:
                raw, _ = self._idle.get_nowait()
            except queue.Empty:
                return
            self._discard()
            try:
                raw.close()
            except Exception:
                pass

    def stats(self) -> dict:
        return {"size": self._size, "open": self._created, "idle": self._idle.qsize()}


_pool = _Pool(config.DB_POOL_SIZE, config.DB_POOL_TIMEOUT_SECONDS)


def pool_stats() -> dict: return _pool.stats()


def close_pool() -> None: _pool.close_all()


class _Connection:
    """Thin wrapper so `with conn() as db: db.execute(...)` returns a cursor
    that is iterable and has fetchone()/fetchall(), and the block commits on clean exit.
    A fresh cursor per execute() so an earlier result set is never clobbered by a later
    statement inside the same block. On exit the connection goes back to the pool rather
    than being closed."""

    def __init__(self, raw: pymysql.connections.Connection, born: float):
        self._raw = raw
        self._born = born

    def cursor(self): return self._raw.cursor()

    def execute(self, sql: str, params: tuple | list = ()):
        cur = self._raw.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self): self._raw.commit()

    def __enter__(self): return self

    def __exit__(self, exc_type, exc, tb):
        healthy = True
        try:
            if exc_type is None:
                self._raw.commit()
            else:
                self._raw.rollback()
        except Exception:
            # The rollback/commit itself failed: the link is not in a state worth reusing.
            healthy = False
        finally:
            _pool.release(self._raw, self._born, healthy and not isinstance(exc, pymysql.Error))
        return False


def conn() -> _Connection:
    raw, born = _pool.acquire()
    return _Connection(raw, born)


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
    # Maps a caller's phone number to the case it belongs to, so a phone check-in is written
    # against the right person instead of a single hardcoded case id. Numbers are stored as a
    # keyed hash, not in the clear: the phone number of someone in a domestic-violence case is
    # among the most sensitive identifiers this system could hold, and a lookup only ever needs
    # equality matching.
    """CREATE TABLE IF NOT EXISTS phone_links (
        id INT AUTO_INCREMENT PRIMARY KEY, phone_hash CHAR(64) NOT NULL UNIQUE,
        case_id VARCHAR(64) NOT NULL, label VARCHAR(64), created_at TEXT, revoked_at TEXT
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS privacy_requests (
        id INT AUTO_INCREMENT PRIMARY KEY, case_id VARCHAR(64) NOT NULL, request_type VARCHAR(64) NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'Open', created_at TEXT
    ) ENGINE=InnoDB""",
    # One row per courtroom-rehearsal practice session. Only the measurements are kept —
    # what the participant said while practising is never stored (see /api/rehearsal/check).
    """CREATE TABLE IF NOT EXISTS rehearsal_sessions (
        id INT AUTO_INCREMENT PRIMARY KEY, case_id VARCHAR(64) NOT NULL, sensor VARCHAR(32) NOT NULL,
        duration_seconds INT, questions_answered INT, levels_completed INT, calm_level INT,
        baseline_bpm INT NULL, peak_bpm INT NULL, recovery_seconds INT NULL, spikes INT, pauses INT,
        self_rating_avg FLOAT NULL, readiness INT, timeline JSON, created_at TEXT
    ) ENGINE=InnoDB""",
]

# Every query this app issues in a request path filters or orders on one of these. Without
# them each /api/cases/{id} view, chat turn and dashboard load is a full table scan, which
# is invisible at demo row counts and fatal at real ones.
# Each entry is (index name, table, column list).
INDEXES = [
    ("idx_events_case_id", "events", "case_id, id"),
    ("idx_events_case_kind", "events", "case_id, kind, created_at(32)"),
    ("idx_alerts_case_id", "alerts", "case_id, id"),
    ("idx_alerts_status", "alerts", "status, id"),
    ("idx_alerts_ack", "alerts", "acknowledged_at, severity(16)"),
    ("idx_interventions_case_id", "interventions", "case_id, id"),
    ("idx_case_notes_case_id", "case_notes", "case_id, id"),
    ("idx_chat_messages_case_id", "chat_messages", "case_id, id"),
    ("idx_chat_feedback_case", "chat_feedback", "case_id, chat_message_id"),
    ("idx_followups_case_id", "followups", "case_id, id"),
    ("idx_consent_case_type", "consent", "case_id, consent_type, id"),
    ("idx_privacy_requests_case", "privacy_requests", "case_id, status"),
    ("idx_audit_log_actor", "audit_log", "actor_email(190), id"),
    ("idx_audit_log_entity", "audit_log", "entity_type(64), entity_id(64), id"),
    ("idx_sessions_user", "sessions", "user_id, expires_at"),
    ("idx_sessions_expiry", "sessions", "expires_at"),
    ("idx_cases_score", "cases", "score"),
    ("idx_phone_links_case", "phone_links", "case_id"),
    ("idx_rehearsal_case", "rehearsal_sessions", "case_id, id"),
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
    # Escalation acknowledgement: who picked the alert up and when. An unacknowledged
    # High/Critical alert is the operational failure this system exists to prevent, so it
    # has to be a queryable column rather than something inferred from the audit log.
    ("alerts", "acknowledged_at", "DATETIME NULL"),
    ("alerts", "acknowledged_by", "VARCHAR(255) NULL"),
    ("alerts", "notified_at", "DATETIME NULL"),
    ("alerts", "notify_attempts", "INT NOT NULL DEFAULT 0"),
    # Sessions are looked up by hash, never by the raw bearer token — see create_session().
    ("sessions", "absolute_expires_at", "DATETIME NULL"),
    ("sessions", "last_seen_at", "DATETIME NULL"),
    # Erasure requests are executed, not just recorded.
    ("privacy_requests", "completed_at", "TEXT"),
    ("privacy_requests", "detail", "TEXT"),
    ("cases", "deleted_at", "TEXT"),
    # A case note is internal to the care team unless the counsellor explicitly shares it.
    ("case_notes", "shared", "BOOLEAN NOT NULL DEFAULT FALSE"),
]

# Sample accounts for local development and the walkthrough. These are created ONLY when
# SEED_DEMO_DATA is on, which config.py refuses to allow in production — their passwords are
# published in the README, so a production database must never contain them. Real accounts
# are created with `python3 -m backend.admin create-user`.
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
        _execute_ddl(cur, f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")


# A weekly check-in trajectory for the seeded demo case, as answers on the same scales the
# model was trained on (mood/sleep 0-3, safety/anxiety 0-2, case_pressure 0-3). The scores
# stored against these are not written here — each row is scored by the real model at seed
# time, exactly as a live check-in would be — so the demo history is genuine model output over
# synthetic answers rather than a hand-drawn curve. Without it the demo case has no history
# spanning real time, and anything that reads a trajectory (the distress trend, the forecast)
# has nothing to work from but whatever test traffic the database has seen.
DEMO_HISTORY = [
    {"mood": 1, "sleep": 1, "safety": 0, "anxiety": 1, "case_pressure": 1},
    {"mood": 1, "sleep": 1, "safety": 0, "anxiety": 1, "case_pressure": 1},
    {"mood": 1, "sleep": 2, "safety": 0, "anxiety": 1, "case_pressure": 2},
    {"mood": 2, "sleep": 2, "safety": 0, "anxiety": 1, "case_pressure": 2},
    {"mood": 2, "sleep": 2, "safety": 1, "anxiety": 2, "case_pressure": 2},
    {"mood": 2, "sleep": 3, "safety": 1, "anxiety": 2, "case_pressure": 3},
    {"mood": 3, "sleep": 3, "safety": 1, "anxiety": 2, "case_pressure": 3},
]
DEMO_HISTORY_CADENCE_DAYS = 9
DEMO_CASE_ID = "7934"


def _seed_demo_history(cur) -> None:
    """Give the seeded demo case a check-in history that spans real weeks.

    Runs once: it is skipped as soon as the case has any check-in older than one cadence, so a
    restart never re-seeds and a database with real history is left alone. Each entry is scored
    by ml.predict.assess() here, so these rows are indistinguishable in kind from a check-in
    submitted through the app — same model, same fields, same explanations.
    """
    cur.execute(
        "SELECT COUNT(*) AS total FROM events WHERE case_id=%s AND kind='checkin' AND created_at < %s",
        (DEMO_CASE_ID, (datetime.now(timezone.utc) - timedelta(days=DEMO_HISTORY_CADENCE_DAYS)).isoformat()),
    )
    row = cur.fetchone()
    if (row["total"] if isinstance(row, dict) else row[0]): return

    from .ml.predict import assess, model_version  # deferred: importing joblib at module load is slow

    version, previous, last = model_version(), 0, None
    for index, answers in enumerate(DEMO_HISTORY):
        offset = (len(DEMO_HISTORY) - index) * DEMO_HISTORY_CADENCE_DAYS
        created = (datetime.now(timezone.utc) - timedelta(days=offset)).isoformat()
        # engagement is held at "in touch" for the seeded weeks — this case did keep checking in,
        # which is exactly what a weekly history means.
        assessment = assess("", answers, previous_score=previous, missed_checkins=0, engagement=3)
        previous, last = assessment["score"], (assessment, created)
        cur.execute(
            "INSERT INTO events(case_id,kind,content,assessment,model_version,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (DEMO_CASE_ID, "checkin", "Seeded demo check-in (synthetic answers, scored by the live model)",
             json.dumps(assessment), version, created),
        )
    # Bring the case row in line with the last seeded check-in, but only if nothing more recent
    # has happened to it — a live case that has since been assessed or overridden keeps its own
    # current score.
    cur.execute("SELECT COUNT(*) AS total FROM events WHERE case_id=%s AND created_at > %s",
                (DEMO_CASE_ID, last[1]))
    newer = cur.fetchone()
    if not (newer["total"] if isinstance(newer, dict) else newer[0]):
        cur.execute("UPDATE cases SET score=%s, risk_band=%s, updated_at=%s, last_checkin_at=%s WHERE id=%s",
                    (last[0]["score"], last[0]["risk_band"], last[1], last[1], DEMO_CASE_ID))


def _add_index_if_missing(cur, name: str, table: str, columns: str) -> None:
    cur.execute(
        "SELECT 1 FROM information_schema.statistics WHERE table_schema=DATABASE() "
        "AND table_name=%s AND index_name=%s LIMIT 1",
        (table, name),
    )
    if cur.fetchone():
        return
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=%s",
        (table,),
    )
    if not cur.fetchone():
        return
    _execute_ddl(cur, f"CREATE INDEX `{name}` ON `{table}` ({columns})")
    log.info("created index", extra={"index": name, "table": table})


# MySQL errors that mean "another process already did this". DDL is not transactional, so
# two workers booting at the same time — which is what every rolling deploy does — can both
# pass the information_schema check and both issue the CREATE. Losing that race is fine;
# crashing the worker over it is not.
_ALREADY_EXISTS = {1060, 1061, 1050, 1091}  # duplicate column, duplicate key, table exists, no such key


def _execute_ddl(cur, sql: str, params: tuple = ()) -> None:
    try:
        cur.execute(sql, params)
    except pymysql.err.OperationalError as failure:
        if failure.args[0] not in _ALREADY_EXISTS:
            raise
        log.debug("DDL already applied by another process", extra={"error": failure.args[1]})


def init_db() -> None:
    """Create the schema, apply additive migrations, and (outside production) seed samples.

    Idempotent and safe to run concurrently: every worker calls it at startup, so the whole
    body runs under a named MySQL advisory lock and each statement tolerates having lost
    the race anyway. Schema changes that are not additive belong in an Alembic revision
    (see alembic/), not here; this function only guarantees the baseline exists.
    """
    with conn() as db:
        # Serialises schema setup across workers and across replicas. A worker that cannot
        # take the lock inside 30s proceeds anyway — the statements are individually safe,
        # and blocking startup forever on a stuck lock would be worse.
        got_lock = db.execute("SELECT GET_LOCK('raahat_schema_init', 30) AS ok").fetchone()["ok"]
        if not got_lock:
            log.warning("schema init proceeding without advisory lock")
        try:
            _init_schema(db)
        finally:
            db.execute("SELECT RELEASE_LOCK('raahat_schema_init')")


def _init_schema(db) -> None:
    """Statements that build the baseline schema. Always called under the advisory lock."""
    cur = db.cursor()
    for statement in SCHEMA:
        _execute_ddl(cur, statement)
    for table, column, definition in MIGRATIONS:
        _add_column_if_missing(cur, table, column, definition)
    for name, table, columns in INDEXES:
        _add_index_if_missing(cur, name, table, columns)
    _upgrade_legacy_password_rows(cur)
    if not config.SEED_DEMO_DATA:
        log.info("schema ready; demo seeding disabled", extra={"app_env": config.APP_ENV})
        return
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
    _seed_demo_history(cur)


def _upgrade_legacy_password_rows(cur) -> None:
    """Re-hash any row still holding a pre-hashing plain-text password. A database created
    after password hashing existed never matches, so this costs one indexed-free query."""
    cur.execute("SELECT id, password FROM users WHERE password NOT LIKE %s", ("scrypt$%",))
    for row in cur.fetchall():
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


# A bearer token IS the credential: whoever holds one is that user until it expires. It is
# therefore stored the way a password is — only a hash goes to the database, so read access
# to the sessions table (a backup, a replica, a SQL-injection elsewhere, the raw-table
# inspector) cannot be turned into account takeover.
#
# Unlike a password this is a 192-bit random value, so it needs no salt and no slow KDF:
# there is nothing to brute-force. SHA-256 with a server-side pepper is the right primitive,
# and it keeps the lookup a single indexed equality match. Rotating SESSION_SECRET
# invalidates every live session, which is what you want after a suspected compromise.
def _token_hash(token: str) -> str:
    return hmac.new(config.SESSION_SECRET.encode() or b"raahat-dev-pepper", token.encode(), hashlib.sha256).hexdigest()


def create_session(user_id: int, ttl_seconds: int, max_lifetime_seconds: int | None = None) -> str:
    """Issue a fresh bearer token. Any session the same user already holds is left alone —
    signing in on a second device must not sign the first one out — but an absolute expiry
    is stamped so no session can be refreshed indefinitely."""
    token = secrets.token_urlsafe(32)
    absolute = max_lifetime_seconds or config.SESSION_MAX_LIFETIME_SECONDS
    with conn() as db:
        db.execute(
            "INSERT INTO sessions (token,user_id,expires_at,absolute_expires_at,last_seen_at) "
            "VALUES (%s,%s, DATE_ADD(NOW(), INTERVAL %s SECOND), DATE_ADD(NOW(), INTERVAL %s SECOND), NOW())",
            (_token_hash(token), user_id, ttl_seconds, absolute),
        )
    return token


def get_session(token: str) -> dict | None:
    """Resolve a bearer token to its user, or None if it is unknown, revoked, or expired.

    `absolute_expires_at IS NULL OR ...` keeps sessions issued before that column existed
    valid until their own expiry rather than logging everyone out on deploy."""
    if not token:
        return None
    with conn() as db:
        row = db.execute(
            """SELECT u.id AS user_id, u.email, u.role, u.name, u.case_id
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token=%s AND s.revoked_at IS NULL AND s.expires_at > NOW()
                 AND (s.absolute_expires_at IS NULL OR s.absolute_expires_at > NOW())
                 AND u.is_active=1""",
            (_token_hash(token),),
        ).fetchone()
    return row


def touch_session(token: str) -> None:
    """Record activity. Kept separate from get_session so the read path stays a pure SELECT
    and the write can be skipped for high-frequency polling routes."""
    with conn() as db:
        db.execute("UPDATE sessions SET last_seen_at=NOW() WHERE token=%s", (_token_hash(token),))


def revoke_session(token: str) -> None:
    with conn() as db:
        db.execute("UPDATE sessions SET revoked_at=NOW() WHERE token=%s AND revoked_at IS NULL", (_token_hash(token),))


def revoke_user_sessions(user_id: int) -> int:
    """Sign a user out everywhere — used when an account is deactivated or a password is
    reset, and the lever on-call pulls when a session is suspected stolen."""
    with conn() as db:
        cur = db.execute("UPDATE sessions SET revoked_at=NOW() WHERE user_id=%s AND revoked_at IS NULL", (user_id,))
        return cur.rowcount


def purge_expired_sessions() -> int:
    """Delete sessions that have been dead for a day. Without this the table grows without
    bound and every backup carries a longer credential history than it needs to."""
    with conn() as db:
        cur = db.execute(
            "DELETE FROM sessions WHERE expires_at < DATE_SUB(NOW(), INTERVAL 1 DAY) "
            "OR (revoked_at IS NOT NULL AND revoked_at < DATE_SUB(NOW(), INTERVAL 1 DAY))"
        )
        return cur.rowcount


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
    return {"case_id": case_id, "messages": messages, "checkins": checkins, "rehearsals": list_rehearsals(case_id),
            "monitoring_consent": consent_status(case_id)}


# Tables holding participant-generated content, in the order they must be cleared. Erasure
# empties these; the case row itself is retained as a tombstone (id, deleted_at, and nothing
# else) because alert and audit history has to remain attributable to *something* for the
# safeguarding record, and because a re-used case id would otherwise silently inherit
# someone else's history.
ERASABLE_TABLES = ["chat_feedback", "chat_messages", "events", "case_notes", "interventions",
                   "followups", "alerts", "consent", "case_assignments", "rehearsal_sessions"]


def request_privacy_action(case_id: str, request_type: str) -> dict:
    """Record an erasure request and carry it out.

    The DPDP Act 2023 (and GDPR Art. 17 for any EU data) give a data principal an
    enforceable right to erasure — a queued row that nothing ever processes does not
    satisfy it. Deletion is executed synchronously inside one transaction so the request
    is either fully honoured or not recorded as honoured at all.
    """
    if request_type != "delete_data":
        raise ValueError(f"Unsupported privacy request type: {request_type}")
    created = now()
    with conn() as db:
        cur = db.execute("INSERT INTO privacy_requests(case_id,request_type,created_at) VALUES (%s,%s,%s)",
                         (case_id, request_type, created))
        new_id = cur.lastrowid
        removed = {}
        for table in ERASABLE_TABLES:
            deleted = db.execute(f"DELETE FROM `{table}` WHERE case_id=%s", (case_id,))
            if deleted.rowcount:
                removed[table] = deleted.rowcount
        # The login for this case can no longer be used, and any live session for it dies.
        db.execute("UPDATE sessions s JOIN users u ON u.id=s.user_id SET s.revoked_at=NOW() "
                   "WHERE u.case_id=%s AND s.revoked_at IS NULL", (case_id,))
        db.execute("UPDATE users SET is_active=0 WHERE case_id=%s", (case_id,))
        # Tombstone: identifiers and free text cleared, the row kept so the id is never reused.
        db.execute("UPDATE cases SET name='[erased]', district='', state='', category='Other', "
                   "score=0, risk_band='Stable', deleted_at=%s, updated_at=%s WHERE id=%s",
                   (created, created, case_id))
        detail = json.dumps(removed)
        db.execute("UPDATE privacy_requests SET status='Completed', completed_at=%s, detail=%s WHERE id=%s",
                   (now(), detail, new_id))
    log.info("erasure request completed", extra={"case_id": case_id, "request_id_db": new_id, "rows": removed})
    return {"id": new_id, "case_id": case_id, "request_type": request_type, "status": "Completed",
            "completed_at": now(), "rows_removed": removed}


# ---------------------------------------------------------------------------
# Phone-number -> case resolution for the IVR transports
# ---------------------------------------------------------------------------

def _phone_hash(number: str) -> str:
    """Keyed hash of an E.164 number. Keyed rather than plain SHA-256 because the space of
    phone numbers is small enough to enumerate exhaustively — an unkeyed digest of a phone
    number is not anonymisation."""
    normalized = "".join(ch for ch in (number or "") if ch.isdigit() or ch == "+")
    return hmac.new((config.SESSION_SECRET or "raahat-dev-pepper").encode(),
                    normalized.encode(), hashlib.sha256).hexdigest()


def link_phone(number: str, case_id: str, label: str = "") -> None:
    with conn() as db:
        db.execute("INSERT INTO phone_links (phone_hash,case_id,label,created_at) VALUES (%s,%s,%s,%s) "
                   "ON DUPLICATE KEY UPDATE case_id=VALUES(case_id), revoked_at=NULL",
                   (_phone_hash(number), case_id, label[:64], now()))


def case_id_for_phone(number: str) -> str | None:
    """Resolve a caller to their case, or None if the number is not registered.

    An unregistered caller must not fall through to some default case: writing one person's
    distress answers into another person's record is both a data breach and a safeguarding
    failure, and it is exactly what a single hardcoded case id produced.
    """
    if not number:
        return None
    with conn() as db:
        row = db.execute(
            "SELECT p.case_id FROM phone_links p JOIN cases c ON c.id = p.case_id "
            "WHERE p.phone_hash=%s AND p.revoked_at IS NULL AND c.deleted_at IS NULL",
            (_phone_hash(number),)).fetchone()
    return row["case_id"] if row else None


def purge_expired_data(retention_days: int) -> dict:
    """Delete participant content older than the retention window.

    Storing distress disclosures forever is a liability, not a feature: retention has to be
    bounded and the bound has to be enforced by something that actually runs. Alert rows are
    kept (they are the safeguarding record) but their free-text reason is cleared.
    """
    if retention_days <= 0:
        return {"skipped": "retention disabled"}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    removed = {}
    with conn() as db:
        for table in ("chat_messages", "events", "case_notes"):
            cur = db.execute(f"DELETE FROM `{table}` WHERE created_at < %s", (cutoff,))
            if cur.rowcount:
                removed[table] = cur.rowcount
        cur = db.execute("UPDATE alerts SET reason='[retention: cleared]' WHERE created_at < %s AND reason <> '[retention: cleared]'", (cutoff,))
        if cur.rowcount:
            removed["alerts_redacted"] = cur.rowcount
    if removed:
        log.info("retention purge", extra={"cutoff": cutoff, "rows": removed})
    return {"cutoff": cutoff, "rows_removed": removed}


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

def case(case_id: str) -> dict:
    """An erased case (deleted_at set) reads as not found — its tombstone row exists only to
    stop the id being re-used, and must not resurface as a case anyone can act on."""
    with conn() as db:
        row = db.execute("SELECT * FROM cases WHERE id=%s AND deleted_at IS NULL", (case_id,)).fetchone()
    return dict(row) if row else {}


def list_cases(limit: int = 100, offset: int = 0, search: str = "", risk_band: str = "") -> dict:
    """Paginated, filterable case list. Returning every row was fine at demo scale and is a
    slow query plus a large response at any real one."""
    limit = max(1, min(limit, 500))
    where = ["deleted_at IS NULL"]
    params: list = []
    if search:
        where.append("(name LIKE %s OR id LIKE %s OR district LIKE %s)")
        params += [f"%{search}%"] * 3
    if risk_band:
        where.append("risk_band=%s")
        params.append(risk_band)
    clause = " AND ".join(where)
    with conn() as db:
        total = db.execute(f"SELECT COUNT(*) AS total FROM cases WHERE {clause}", params).fetchone()["total"]
        rows = [dict(row) for row in db.execute(
            f"SELECT * FROM cases WHERE {clause} ORDER BY score DESC, id LIMIT %s OFFSET %s",
            params + [limit, offset])]
    return {"cases": rows, "total": total, "limit": limit, "offset": offset}


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


def case_events(case_id: str, limit: int = 60) -> list[dict]:
    """This case's recorded events, oldest first, with their assessments decoded. The
    dashboard payload only carries the 30 most recent events *across all cases*, which is
    not enough history to project a trend for any one of them — the forecast reads here."""
    with conn() as db:
        rows = db.execute(
            "SELECT * FROM events WHERE case_id=%s ORDER BY id DESC LIMIT %s", (case_id, limit)
        ).fetchall()
    return [_decode(row) for row in reversed(rows)]


def record(case_id: str, kind: str, content: str, assessment: dict, model_version: str = "") -> dict | None:
    """Persist an event/score update and return {id, severity, reason} if it triggered a new alert, else None."""
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
            cur = db.execute("INSERT INTO alerts(case_id,severity,reason,created_at) VALUES (%s,%s,%s,%s)", (case_id, severity, reason, created))
            # The id travels with the alert so the notification, the acknowledgement, and the
            # unacknowledged-alert sweeper all refer to the same row.
            alert_info = {"id": cur.lastrowid, "severity": severity, "reason": reason}
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


def add_case_note(case_id: str, author: str, text: str, shared: bool = False) -> dict:
    created = now()
    with conn() as db:
        cur = db.execute(
            "INSERT INTO case_notes(case_id,author,text,created_at,shared) VALUES (%s,%s,%s,%s,%s)",
            (case_id, author, text, created, bool(shared)),
        )
        new_id = cur.lastrowid
    return {"id": new_id, "case_id": case_id, "author": author, "text": text, "created_at": created, "shared": bool(shared)}


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
        cur = db.execute("INSERT INTO alerts(case_id,severity,reason,created_at) VALUES (%s,%s,%s,%s)", (case_id, "Critical", reason, created))
        alert_id = cur.lastrowid
    updated = case(case_id)
    updated["alert_id"] = alert_id
    return updated


def acknowledge_alert(alert_id: int, actor_email: str) -> dict:
    """Mark an alert as picked up by a named person.

    The number that matters operationally is time-to-acknowledgement on a High/Critical
    alert; it can only be measured if acknowledgement is an explicit, attributable act
    rather than something inferred from whoever happened to open the dashboard."""
    with conn() as db:
        db.execute("UPDATE alerts SET acknowledged_at=NOW(), acknowledged_by=%s "
                   "WHERE id=%s AND acknowledged_at IS NULL", (actor_email, alert_id))
        row = db.execute("SELECT * FROM alerts WHERE id=%s", (alert_id,)).fetchone()
    return dict(row) if row else {}


def unacknowledged_escalations(older_than_seconds: int) -> list[dict]:
    """High/Critical alerts nobody has picked up inside the acknowledgement window.

    This is the query the escalation ladder runs on: an alert that is raised but never
    seen is the exact failure this system exists to prevent, so it re-notifies instead of
    waiting for someone to refresh a dashboard."""
    with conn() as db:
        return [dict(row) for row in db.execute(
            """SELECT a.*, c.name FROM alerts a LEFT JOIN cases c ON c.id = a.case_id
               WHERE a.acknowledged_at IS NULL AND a.status <> 'Resolved'
                 AND a.severity IN ('High','Critical')
                 AND (a.notified_at IS NULL OR a.notified_at < DATE_SUB(NOW(), INTERVAL %s SECOND))
               ORDER BY a.id DESC LIMIT 50""",
            (older_than_seconds,))]


def mark_alert_notified(alert_id: int) -> None:
    with conn() as db:
        db.execute("UPDATE alerts SET notified_at=NOW(), notify_attempts=notify_attempts+1 WHERE id=%s", (alert_id,))


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


def rehearsal_readiness(calm_level: int, levels_completed: int, questions_answered: int,
                        spikes: int, recovery_seconds: int | None) -> int:
    """Court-readiness score (0-100) for one rehearsal session.

    Deliberately a short, explainable rule rather than a model: a counsellor has to be able to
    say why a witness reads 70, and there is no outcome data a learned score could be fitted to.
    The main term is the highest question intensity the person got through *without* a stress
    spike (20 points per level, 4 levels). Smaller credit for practising enough questions, and
    for recovering quickly — or not needing to — because how fast someone settles after a hard
    question matters in a witness box as much as whether the question rattled them at all.
    """
    score = calm_level * 20
    if questions_answered >= 8:
        score += 10
    if (recovery_seconds is not None and recovery_seconds <= 60) or (spikes == 0 and levels_completed >= 3):
        score += 10
    return max(0, min(100, score))


def save_rehearsal(case_id: str, session: dict) -> dict:
    readiness = rehearsal_readiness(session["calm_level"], session["levels_completed"], session["questions_answered"],
                                    session["spikes"], session["recovery_seconds"])
    created = now()
    columns = ("sensor", "duration_seconds", "questions_answered", "levels_completed", "calm_level", "baseline_bpm",
               "peak_bpm", "recovery_seconds", "spikes", "pauses", "self_rating_avg")
    with conn() as db:
        cur = db.execute(
            f"INSERT INTO rehearsal_sessions(case_id,{','.join(columns)},readiness,timeline,created_at) "
            f"VALUES (%s,{','.join(['%s'] * len(columns))},%s,%s,%s)",
            (case_id, *(session[c] for c in columns), readiness, json.dumps(session["timeline"]), created),
        )
        new_id = cur.lastrowid
    return {"id": new_id, "case_id": case_id, **{c: session[c] for c in columns}, "readiness": readiness,
            "timeline": session["timeline"], "created_at": created}


def list_rehearsals(case_id: str, limit: int = 20) -> list[dict]:
    """This case's rehearsal sessions, oldest first, so a chart reads left to right as progress."""
    with conn() as db:
        rows = db.execute("SELECT * FROM rehearsal_sessions WHERE case_id=%s ORDER BY id DESC LIMIT %s",
                          (case_id, limit)).fetchall()
    sessions = []
    for row in reversed(rows):
        row = dict(row)
        row["timeline"] = _json(row["timeline"]) or []
        sessions.append(row)
    return sessions


def participant_view(case_id: str) -> dict:
    """Everything a participant may see about their own case, fetched scoped.

    The staff dashboard payload used to be built in full and then filtered down in Python
    for a participant session. That loaded every other case's rows into the process to
    answer a single-case request — needless work, and one missed filter away from showing
    someone another participant's data. Scoping it in SQL removes the possibility.
    """
    with conn() as db:
        case_row = db.execute("SELECT * FROM cases WHERE id=%s AND deleted_at IS NULL", (case_id,)).fetchone()
        cases = [dict(case_row)] if case_row else []
        alerts = [dict(row) for row in db.execute(
            "SELECT * FROM alerts WHERE case_id=%s ORDER BY id DESC LIMIT 50", (case_id,))]
        events = [_decode(row) for row in db.execute(
            "SELECT * FROM events WHERE case_id=%s ORDER BY id DESC LIMIT 30", (case_id,))]
        interventions = [dict(row) for row in db.execute(
            "SELECT * FROM interventions WHERE case_id=%s ORDER BY id DESC LIMIT 40", (case_id,))]
        # Only notes the counsellor chose to share. Case notes are clinical working notes
        # ("risk of self-harm discussed") and used to reach the participant in full here.
        notes = [dict(row) for row in db.execute(
            "SELECT * FROM case_notes WHERE case_id=%s AND shared=TRUE ORDER BY id DESC LIMIT 20", (case_id,))]
    return {
        "scope": {"district": cases[0]["district"] if cases else "", "state": cases[0]["state"] if cases else "",
                  "country": "India"},
        "cases": cases, "alerts": alerts, "events": events, "interventions": interventions, "notes": notes,
        # Aggregate metrics describe the whole caseload and are not the participant's to see.
        "metrics": {},
    }


def dashboard() -> dict:
    with conn() as db:
        cases = [dict(row) for row in db.execute(
            "SELECT * FROM cases WHERE deleted_at IS NULL ORDER BY score DESC LIMIT 500")]
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
