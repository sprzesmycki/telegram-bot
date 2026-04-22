"""subscriptions table

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-21

"""
from __future__ import annotations

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE subscriptions (
            id                    SERIAL PRIMARY KEY,
            owner_user_id         BIGINT NOT NULL,
            name                  TEXT NOT NULL,
            vendor                TEXT,
            category              TEXT DEFAULT 'subscriptions',
            subcategory           TEXT,
            amount                NUMERIC(14,2) NOT NULL,
            currency              TEXT NOT NULL DEFAULT 'PLN',
            billing_period_months INTEGER NOT NULL DEFAULT 1,
            active                BOOLEAN NOT NULL DEFAULT TRUE,
            start_date            DATE NOT NULL DEFAULT CURRENT_DATE,
            end_date              DATE,
            notes                 TEXT,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_subscriptions_owner ON subscriptions (owner_user_id, active)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS subscriptions")
