"""piano streak freeze columns

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-30

"""
from __future__ import annotations

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE piano_streak
            ADD COLUMN IF NOT EXISTS freeze_credits INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS freeze_until   DATE
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE piano_streak DROP COLUMN IF EXISTS freeze_until")
    op.execute("ALTER TABLE piano_streak DROP COLUMN IF EXISTS freeze_credits")
