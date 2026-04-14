"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-14

"""
from __future__ import annotations

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS profiles (
            id             SERIAL PRIMARY KEY,
            owner_user_id  BIGINT NOT NULL,
            name           TEXT NOT NULL,
            active         BOOLEAN NOT NULL DEFAULT TRUE,
            height_cm      REAL,
            weight_kg      REAL,
            age            INTEGER,
            gender         TEXT,
            activity_level TEXT,
            UNIQUE (owner_user_id, name)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS active_profile (
            user_id    BIGINT PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES profiles(id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS meals (
            id               SERIAL PRIMARY KEY,
            profile_id       INTEGER NOT NULL REFERENCES profiles(id),
            owner_user_id    BIGINT NOT NULL,
            eaten_at         TIMESTAMPTZ NOT NULL,
            logged_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            description      TEXT NOT NULL,
            calories         INTEGER,
            protein_g        REAL,
            carbs_g          REAL,
            fat_g            REAL,
            raw_llm_response TEXT,
            photo_path       TEXT
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS goals (
            profile_id      INTEGER PRIMARY KEY REFERENCES profiles(id),
            daily_calories  INTEGER NOT NULL DEFAULT 2000,
            daily_protein_g REAL,
            daily_carbs_g   REAL,
            daily_fat_g     REAL
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS supplements (
            id            SERIAL PRIMARY KEY,
            profile_id    INTEGER NOT NULL REFERENCES profiles(id),
            owner_user_id BIGINT NOT NULL,
            name          TEXT NOT NULL,
            reminder_time TEXT NOT NULL,
            dose          TEXT,
            active        BOOLEAN NOT NULL DEFAULT TRUE
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS supplement_logs (
            id            SERIAL PRIMARY KEY,
            supplement_id INTEGER NOT NULL REFERENCES supplements(id),
            profile_id    INTEGER NOT NULL,
            taken_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS piano_sessions (
            id               SERIAL PRIMARY KEY,
            owner_user_id    BIGINT NOT NULL,
            practiced_at     DATE NOT NULL,
            duration_minutes INTEGER,
            notes            TEXT,
            pieces_practiced TEXT,
            logged_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS piano_pieces (
            id                SERIAL PRIMARY KEY,
            owner_user_id     BIGINT NOT NULL,
            title             TEXT NOT NULL,
            composer          TEXT,
            status            TEXT NOT NULL DEFAULT 'learning',
            added_at          DATE NOT NULL DEFAULT CURRENT_DATE,
            last_practiced_at DATE,
            notes             TEXT
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS piano_recordings (
            id               SERIAL PRIMARY KEY,
            owner_user_id    BIGINT NOT NULL,
            piece_id         INTEGER REFERENCES piano_pieces(id),
            recorded_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            file_path        TEXT,
            duration_seconds INTEGER,
            feedback_summary TEXT,
            raw_analysis     TEXT
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS piano_streak (
            owner_user_id       BIGINT PRIMARY KEY,
            current_streak      INTEGER NOT NULL DEFAULT 0,
            longest_streak      INTEGER NOT NULL DEFAULT 0,
            last_practiced_date DATE
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS liquids (
            id               SERIAL PRIMARY KEY,
            profile_id       INTEGER NOT NULL REFERENCES profiles(id),
            owner_user_id    BIGINT NOT NULL,
            drunk_at         TIMESTAMPTZ NOT NULL,
            logged_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            description      TEXT NOT NULL,
            amount_ml        INTEGER NOT NULL,
            calories         INTEGER NOT NULL DEFAULT 0,
            protein_g        REAL NOT NULL DEFAULT 0,
            carbs_g          REAL NOT NULL DEFAULT 0,
            fat_g            REAL NOT NULL DEFAULT 0,
            raw_llm_response TEXT
        )
        """
    )

    op.execute("CREATE INDEX IF NOT EXISTS idx_meals_profile_eaten ON meals (profile_id, eaten_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_meals_owner ON meals (owner_user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_liquids_profile_drunk ON liquids (profile_id, drunk_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_liquids_owner ON liquids (owner_user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_supplement_logs_profile_taken ON supplement_logs (profile_id, taken_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_piano_sessions_owner_practiced ON piano_sessions (owner_user_id, practiced_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_piano_pieces_owner ON piano_pieces (owner_user_id)")


def downgrade() -> None:
    for table in (
        "liquids",
        "piano_recordings",
        "piano_streak",
        "piano_pieces",
        "piano_sessions",
        "supplement_logs",
        "supplements",
        "goals",
        "meals",
        "active_profile",
        "profiles",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
