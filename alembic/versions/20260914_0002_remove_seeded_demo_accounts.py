"""Remove seeded demo accounts from a production database.

The three demo logins (victim@demo.in, counsellor@demo.in, admin@demo.in) have passwords
published in the README and were created unconditionally on every boot, including on a
production database. One of them is an administrator.

This deletes them, and their sessions, from any environment that is not explicitly seeding
demo data — a development database keeps them so the walkthrough still works.

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

import sys
from pathlib import Path

from alembic import op

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend import config  # noqa: E402

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

DEMO_EMAILS = ("victim@demo.in", "counsellor@demo.in", "admin@demo.in")


def upgrade() -> None:
    if config.SEED_DEMO_DATA:
        print("SEED_DEMO_DATA is on; leaving demo accounts in place.")
        return
    emails = ",".join(f"'{email}'" for email in DEMO_EMAILS)
    op.execute(f"DELETE s FROM sessions s JOIN users u ON u.id = s.user_id WHERE u.email IN ({emails})")
    op.execute(f"DELETE FROM users WHERE email IN ({emails})")


def downgrade() -> None:
    # Demo accounts are re-created by init_db() when SEED_DEMO_DATA is on.
    pass
