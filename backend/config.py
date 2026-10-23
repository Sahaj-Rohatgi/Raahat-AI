"""Central configuration and environment validation.

Every environment-dependent switch in the app is resolved here once, at import time,
so that a misconfigured production deployment fails at startup with a readable message
instead of silently running with demo defaults.

The two things this file exists to prevent:

  * a production database being seeded with the documented demo logins, and
  * a production process starting without the secrets that protect it.

`APP_ENV=production` makes every default strict; anything else keeps the local
development ergonomics (seed data on, API docs on, missing secrets tolerated).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

TRUE_VALUES = {"1", "true", "yes", "on"}


class ConfigError(RuntimeError):
    """Raised at import time when a production process is missing required configuration."""


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in TRUE_VALUES


def _csv(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV == "production"

DATABASE_URL = os.getenv("DATABASE_URL", "" if IS_PRODUCTION else "mysql://root:root@localhost:3306/raahat_dw")
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))
DB_POOL_TIMEOUT_SECONDS = float(os.getenv("DB_POOL_TIMEOUT_SECONDS", "5"))

# Pepper mixed into the session-token hash. Rotating it invalidates every live session,
# which is the correct behaviour after a suspected token-store compromise.
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", str(12 * 3600)))
# Absolute lifetime: a session is re-authenticated after this regardless of activity.
SESSION_MAX_LIFETIME_SECONDS = int(os.getenv("SESSION_MAX_LIFETIME_SECONDS", str(24 * 3600)))

REDIS_URL = os.getenv("REDIS_URL", "").strip()

# Browsers are only allowed to call the API from these origins. The app serves its own
# frontend same-origin, so an empty list (no cross-origin access at all) is the correct
# production default; set it only when a separate frontend host is in play.
ALLOWED_ORIGINS = _csv("ALLOWED_ORIGINS")
# Hosts this app will answer to. Blocks Host-header poisoning behind a proxy.
ALLOWED_HOSTS = _csv("ALLOWED_HOSTS") or (["*"] if not IS_PRODUCTION else [])
# Only trust X-Forwarded-* headers when a known proxy is in front, otherwise a client can
# forge its own source IP and defeat the login rate limiter.
TRUST_PROXY_HEADERS = _flag("TRUST_PROXY_HEADERS", False)

# Demo/sample content. Never enabled by default in production: the seeded accounts have
# passwords published in the README.
SEED_DEMO_DATA = _flag("SEED_DEMO_DATA", not IS_PRODUCTION)
# Raw-table inspector (/api/db). Exposes participant chat transcripts to any admin, so it
# is off unless deliberately switched on for an internal environment.
ENABLE_DB_INSPECTOR = _flag("ENABLE_DB_INSPECTOR", not IS_PRODUCTION)
ENABLE_API_DOCS = _flag("ENABLE_API_DOCS", not IS_PRODUCTION)
# HTTP Strict-Transport-Security. On by default in production; TLS is terminated upstream.
ENABLE_HSTS = _flag("ENABLE_HSTS", IS_PRODUCTION)

IVR_API_KEY = os.getenv("IVR_API_KEY", "")
ENABLE_IVR = _flag("ENABLE_IVR", bool(IVR_API_KEY))
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
ENABLE_TWILIO = _flag("ENABLE_TWILIO", bool(TWILIO_AUTH_TOKEN))
TWILIO_VOICE = os.getenv("TWILIO_VOICE", "Polly.Aditi")
# Case a phone call maps to when the caller's number is not registered. Unset means an
# unknown caller is told to use a registered number rather than writing to someone's case.
IVR_FALLBACK_CASE_ID = os.getenv("IVR_FALLBACK_CASE_ID", "").strip()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
# Local speech-to-text (faster-whisper). On by default when the package is installed; a voice
# note is then transcribed on this server and only falls back to Gemini if that fails.
LOCAL_STT_ENABLED = _flag("LOCAL_STT_ENABLED", True)
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "").strip()
# How long an escalation may sit unacknowledged before it is re-notified.
ESCALATION_ACK_SECONDS = int(os.getenv("ESCALATION_ACK_SECONDS", "300"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.getenv("LOG_FORMAT", "json" if IS_PRODUCTION else "text").lower()
SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()

# Frontend build. In production the app serves a pre-transpiled bundle; in development it
# may fall back to the in-browser Babel path so the single HTML file stays editable.
FRONTEND_BUNDLE = Path(__file__).resolve().parent.parent / "static" / "app.js"
REQUIRE_FRONTEND_BUNDLE = _flag("REQUIRE_FRONTEND_BUNDLE", IS_PRODUCTION)

DATA_RETENTION_DAYS = int(os.getenv("DATA_RETENTION_DAYS", "0"))  # 0 disables the purge job


def _production_problems() -> list[str]:
    problems = []
    if not DATABASE_URL:
        problems.append("DATABASE_URL is required in production.")
    elif "@localhost" in DATABASE_URL and ":root@" in DATABASE_URL:
        problems.append("DATABASE_URL still points at the local root account.")
    if len(SESSION_SECRET) < 32:
        problems.append("SESSION_SECRET must be set to at least 32 random characters "
                        "(python3 -c 'import secrets; print(secrets.token_urlsafe(48))').")
    if SEED_DEMO_DATA:
        problems.append("SEED_DEMO_DATA must be off in production: it creates the accounts "
                        "whose passwords are published in the README.")
    if ENABLE_DB_INSPECTOR:
        problems.append("ENABLE_DB_INSPECTOR exposes participant chat transcripts; keep it off "
                        "in production unless a documented internal review requires it.")
    if not ALLOWED_HOSTS:
        problems.append("ALLOWED_HOSTS is required in production (comma-separated hostnames).")
    if ENABLE_IVR and len(IVR_API_KEY) < 32:
        problems.append("IVR_API_KEY must be at least 32 random characters, or set ENABLE_IVR=0.")
    if ENABLE_TWILIO and not TWILIO_AUTH_TOKEN:
        problems.append("TWILIO_AUTH_TOKEN is required when ENABLE_TWILIO=1: without it webhook "
                        "signatures are not verified and anyone can post to /api/twilio/*.")
    return problems


def validate() -> None:
    """Fail fast on a misconfigured production process."""
    if not IS_PRODUCTION:
        return
    problems = _production_problems()
    if problems:
        raise ConfigError("Refusing to start in production:\n  - " + "\n  - ".join(problems))


if __name__ == "__main__":
    # `python3 -m backend.config` is the deployment preflight check.
    try:
        validate()
    except ConfigError as failure:
        print(failure, file=sys.stderr)
        raise SystemExit(1)
    print(f"Configuration OK (APP_ENV={APP_ENV}).")
