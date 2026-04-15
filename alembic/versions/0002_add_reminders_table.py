"""add reminders table

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-15

"""
from __future__ import annotations

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id            SERIAL PRIMARY KEY,
            owner_user_id BIGINT NOT NULL,
            message       TEXT NOT NULL,
            reminder_time TEXT NOT NULL,
            days_of_week  TEXT NOT NULL DEFAULT '*',
            active        BOOLEAN NOT NULL DEFAULT TRUE,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminders_owner ON reminders(owner_user_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_reminders_owner")
    op.execute("DROP TABLE IF EXISTS reminders")
