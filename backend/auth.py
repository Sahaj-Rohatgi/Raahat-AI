"""Authentication: email/password sign-in issuing a bearer session token.

Users and sessions live in MySQL (see database.py). Passwords are stored as per-user
salted scrypt hashes and session tokens as keyed hashes, so neither table holds a usable
credential. Every API call requires a valid session, and a participant session can only
ever act on its own case.

Sign-in is deliberately uniform: an unknown email and a wrong password produce the same
message and do the same amount of work, so the endpoint cannot be used to enumerate which
addresses have accounts — which on this service would itself disclose that a named person
is a participant.
"""
from __future__ import annotations

import logging

from fastapi import Depends, Header, HTTPException

from . import config, database

log = logging.getLogger(__name__)

SESSION_TTL_SECONDS = config.SESSION_TTL_SECONDS
# A hash computed against this when the account does not exist, so a miss costs the same
# scrypt work as a hit and the response time does not reveal which happened.
_DUMMY_HASH = database.hash_password("account-enumeration-guard")


def login(email: str, password: str) -> dict:
    user = database.get_user_by_email(email)
    if not user:
        database.verify_password(_DUMMY_HASH, password)
        raise HTTPException(401, "Invalid email or password")
    if not database.verify_password(user["password"], password):
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
