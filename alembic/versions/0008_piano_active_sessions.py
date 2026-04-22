"""piano active sessions

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-21

"""
from __future__ import annotations

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None

def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS piano_active_sessions (
            owner_user_id BIGINT PRIMARY KEY,
            started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS piano_active_sessions")
