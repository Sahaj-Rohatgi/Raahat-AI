"""Invalidate pre-hashing session rows.

Session tokens are now stored as a keyed hash rather than in the clear (see
database._token_hash). Rows written before that change hold a raw bearer token: they can
no longer authenticate anything, and every one of them is a live credential sitting in the
table and in every backup taken since. Delete them.

Everyone signs in again once. That is the correct cost for retiring a plaintext credential
store, and it is the same cost as any secret rotation.

Revision ID: 0001
Revises:
"""
from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM sessions")


def downgrade() -> None:
    # Nothing to restore: the deleted rows were unusable under the new scheme by definition.
    pass
