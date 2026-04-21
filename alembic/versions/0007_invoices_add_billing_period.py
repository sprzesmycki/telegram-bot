"""invoices: add billing_period_months column

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-21
"""
from __future__ import annotations

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS billing_period_months INTEGER NOT NULL DEFAULT 1"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE invoices DROP COLUMN IF EXISTS billing_period_months")
