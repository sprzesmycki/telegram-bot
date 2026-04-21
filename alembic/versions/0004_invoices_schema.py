"""invoices: add pending_invoices and invoices tables

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-21
"""
from __future__ import annotations

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_invoices (
            id              SERIAL PRIMARY KEY,
            owner_user_id   BIGINT NOT NULL,
            tmp_file_path   TEXT NOT NULL,
            parsed          JSONB NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pending_invoices_owner "
        "ON pending_invoices (owner_user_id)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS invoices (
            id                  SERIAL PRIMARY KEY,
            owner_user_id       BIGINT NOT NULL,
            vendor              TEXT,
            invoice_number      TEXT,
            issue_date          DATE,
            due_date            DATE,
            currency            TEXT,
            subtotal            NUMERIC(14, 2),
            tax                 NUMERIC(14, 2),
            total               NUMERIC(14, 2),
            category            TEXT,
            line_items          JSONB NOT NULL DEFAULT '[]',
            notes               TEXT,
            source              TEXT NOT NULL DEFAULT 'manual',
            gmail_message_id    TEXT,
            file_path           TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_invoices_owner "
        "ON invoices (owner_user_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS invoices")
    op.execute("DROP TABLE IF EXISTS pending_invoices")
