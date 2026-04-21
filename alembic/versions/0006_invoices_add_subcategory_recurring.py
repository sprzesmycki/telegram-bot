"""invoices: add subcategory and recurring columns

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-21
"""
from __future__ import annotations

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS subcategory TEXT")
    op.execute(
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS recurring BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE invoices DROP COLUMN IF EXISTS recurring")
    op.execute("ALTER TABLE invoices DROP COLUMN IF EXISTS subcategory")
