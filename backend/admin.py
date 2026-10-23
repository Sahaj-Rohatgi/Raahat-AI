"""Operational CLI: account management, session revocation, retention, preflight.

Production has no seeded accounts (see config.SEED_DEMO_DATA), so the first administrator
is created here. Passwords are generated rather than accepted on the command line by
default, because a password typed as an argument ends up in shell history and in the
process list.

    python3 -m backend.admin create-user --email ops@example.org --role admin --name "Ops"
    python3 -m backend.admin reset-password --email ops@example.org
    python3 -m backend.admin deactivate --email leaver@example.org
    python3 -m backend.admin link-phone --number +919000000000 --case-id 7934
    python3 -m backend.admin purge-sessions
    python3 -m backend.admin purge-data --days 365
    python3 -m backend.admin preflight
"""
from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from . import config, database, observability

ROLES = ("victim", "counsellor", "admin")


def _password(prompt: str) -> tuple[str, bool]:
    """Returns (password, generated). An empty prompt response generates one."""
    if not sys.stdin.isatty():
        return secrets.token_urlsafe(18), True
    typed = getpass.getpass(f"{prompt} (blank to generate): ")
    if not typed:
        return secrets.token_urlsafe(18), True
    if len(typed) < 12:
        raise SystemExit("Password must be at least 12 characters.")
    again = getpass.getpass("Repeat: ")
    if typed != again:
        raise SystemExit("Passwords did not match.")
    return typed, False


def create_user(args) -> None:
    if args.role not in ROLES:
        raise SystemExit(f"--role must be one of {', '.join(ROLES)}")
    if args.role == "victim" and not args.case_id:
        raise SystemExit("A victim account must be tied to a case with --case-id")
    email = args.email.strip().lower()
    if database.get_user_by_email(email):
        raise SystemExit(f"{email} already exists. Use reset-password to change its credentials.")
    password, generated = _password("Password")
    with database.conn() as db:
        db.execute(
            "INSERT INTO users (email,password,role,name,case_id,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (email, database.hash_password(password), args.role, args.name, args.case_id, database.now()),
        )
    print(f"Created {args.role} {email}")
    if generated:
        print(f"Generated password: {password}")
        print("Deliver it out of band and require a change on first sign-in.")


def reset_password(args) -> None:
    user = database.get_user_by_email(args.email.strip().lower())
    if not user:
        raise SystemExit("No such user.")
    password, generated = _password("New password")
    with database.conn() as db:
        db.execute("UPDATE users SET password=%s WHERE id=%s", (database.hash_password(password), user["id"]))
    revoked = database.revoke_user_sessions(user["id"])
    print(f"Password reset; {revoked} live session(s) revoked.")
    if generated:
        print(f"Generated password: {password}")


def deactivate(args) -> None:
    user = database.get_user_by_email(args.email.strip().lower())
    if not user:
        raise SystemExit("No such user.")
    with database.conn() as db:
        db.execute("UPDATE users SET is_active=0 WHERE id=%s", (user["id"],))
    revoked = database.revoke_user_sessions(user["id"])
    print(f"Deactivated {user['email']}; {revoked} live session(s) revoked.")


def link_phone(args) -> None:
    """Register a phone number against a case so its IVR calls are attributed correctly.

    Without a link, a call from that number is refused — which is the safe default: writing
    one person's check-in answers into another person's record is a data breach as well as
    a safeguarding failure.
    """
    if not database.case(args.case_id):
        raise SystemExit(f"No such case: {args.case_id}")
    database.link_phone(args.number, args.case_id, args.label or "")
    # The number itself is not printed back: it is stored hashed precisely so it does not
    # sit in logs and terminal scrollback.
    print(f"Linked the supplied number to case {args.case_id}.")


def purge_sessions(_args) -> None:
    print(f"Deleted {database.purge_expired_sessions()} expired/revoked session rows.")


def purge_data(args) -> None:
    print(database.purge_expired_data(args.days or config.DATA_RETENTION_DAYS))


def preflight(_args) -> None:
    config.validate()
    with database.conn() as db:
        db.execute("SELECT 1").fetchone()
    missing = []
    if config.REQUIRE_FRONTEND_BUNDLE and not config.FRONTEND_BUNDLE.exists():
        missing.append("frontend bundle missing — run `npm run build`")
    if config.IS_PRODUCTION and not config.ALERT_WEBHOOK_URL:
        missing.append("ALERT_WEBHOOK_URL unset — escalations would only reach the log")
    for problem in missing:
        print(f"WARNING: {problem}", file=sys.stderr)
    print(f"Preflight OK (APP_ENV={config.APP_ENV}, db reachable, pool={database.pool_stats()}).")


def main() -> None:
    observability.configure()
    parser = argparse.ArgumentParser(prog="backend.admin", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-user")
    create.add_argument("--email", required=True)
    create.add_argument("--role", required=True, choices=ROLES)
    create.add_argument("--name", required=True)
    create.add_argument("--case-id", dest="case_id", default=None)
    create.set_defaults(func=create_user)

    reset = sub.add_parser("reset-password")
    reset.add_argument("--email", required=True)
    reset.set_defaults(func=reset_password)

    off = sub.add_parser("deactivate")
    off.add_argument("--email", required=True)
    off.set_defaults(func=deactivate)

    link = sub.add_parser("link-phone")
    link.add_argument("--number", required=True, help="E.164, e.g. +919000000000")
    link.add_argument("--case-id", dest="case_id", required=True)
    link.add_argument("--label", default="")
    link.set_defaults(func=link_phone)

    sub.add_parser("purge-sessions").set_defaults(func=purge_sessions)

    purge = sub.add_parser("purge-data")
    purge.add_argument("--days", type=int, default=0)
    purge.set_defaults(func=purge_data)

    sub.add_parser("preflight").set_defaults(func=preflight)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
