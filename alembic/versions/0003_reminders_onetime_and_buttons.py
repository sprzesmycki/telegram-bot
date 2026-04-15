"""reminders: add repeat flag and remind_at for one-time reminders

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-15

"""
from __future__ import annotations

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # repeat=TRUE  → recurring (CronTrigger), existing rows stay recurring
    # repeat=FALSE → one-time (DateTrigger), remind_at holds the exact fire time
    op.execute(
        "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS repeat BOOLEAN NOT NULL DEFAULT TRUE"
    )
    op.execute(
        "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS remind_at TIMESTAMPTZ"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE reminders DROP COLUMN IF EXISTS remind_at")
    op.execute("ALTER TABLE reminders DROP COLUMN IF EXISTS repeat")
