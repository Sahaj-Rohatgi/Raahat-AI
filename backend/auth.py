"""Demo authentication: email/password login issuing a bearer session token.

Users and sessions are stored in MySQL (see database.py) instead of a hardcoded
dict and an in-memory session store — sessions now survive a server reload/restart,
and a new case created through the app can get a real login row instead of nowhere
to sign in. Every API call still requires a valid session, and a victim session can
only ever act on their own case.

Passwords are stored as per-user salted scrypt hashes (see database.hash_password),
so the users table never holds a readable password.
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException

from . import database

SESSION_TTL_SECONDS = 12 * 3600


def login(email: str, password: str) -> dict:
    user = database.get_user_by_email(email)
    if not user or not database.verify_password(user["password"], password):
        raise HTTPException(401, "Invalid email or password")
    token = database.create_session(user["id"], SESSION_TTL_SECONDS)
    database.touch_login(user["id"])
    return {"token": token, "role": user["role"], "name": user["name"], "case_id": user["case_id"]}


def logout(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "): return
    database.revoke_session(authorization.removeprefix("Bearer ").strip())


def require_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token; please sign in")
    token = authorization.removeprefix("Bearer ").strip()
    session = database.get_session(token)
    if not session:
        raise HTTPException(401, "Session expired or invalid; please sign in again")
    return {"email": session["email"], "role": session["role"], "name": session["name"], "case_id": session["case_id"], "token": token}


def require_staff(user: dict = Depends(require_user)) -> dict:
    if user["role"] not in ("counsellor", "admin"):
        raise HTTPException(403, "Counsellor or admin access required")
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    """Stricter than require_staff: the raw-table inspector exposes every table in the
    schema, so it is limited to administrators rather than the whole care team."""
    if user["role"] != "admin":
        raise HTTPException(403, "Administrator access required")
    return user
