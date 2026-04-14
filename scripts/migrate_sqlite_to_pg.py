"""One-shot SQLite -> PostgreSQL import.

Reads rows from the legacy SQLite database (path from SQLITE_PATH, default
./data/caloriebot.db) and writes them into Postgres (DATABASE_URL). Preserves
every row's primary key and resets the SERIAL sequence afterwards so new
inserts don't collide.

Naive SQLite datetimes are treated as Europe/Warsaw local time and converted
to tz-aware UTC before being stored in TIMESTAMPTZ columns.

Prerequisites:
  1. Postgres schema exists (run `alembic upgrade head` first).
  2. SQLite DB is reachable at SQLITE_PATH.

This script TRUNCATES the target tables before importing -- it is destructive
on the Postgres side by design. It is safe to re-run.

Usage:

  # From the host (connecting to dockerised postgres on 127.0.0.1:5432):
  DATABASE_URL=postgresql://caloriebot:caloriebot@127.0.0.1:5432/caloriebot \\
  SQLITE_PATH=./data/caloriebot.db \\
      uv run python scripts/migrate_sqlite_to_pg.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate")

WARSAW = ZoneInfo("Europe/Warsaw")


# Order matters: children before parents on truncate, parents before children on insert.
TABLES_IN_INSERT_ORDER: tuple[str, ...] = (
    "profiles",
    "active_profile",
    "meals",
    "goals",
    "supplements",
    "supplement_logs",
    "piano_sessions",
    "piano_pieces",
    "piano_recordings",
    "piano_streak",
    "liquids",
)

# Per-table: (column_list, list_of_datetime_cols, list_of_date_cols)
TABLE_SPECS: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "profiles": (
        ("id", "owner_user_id", "name", "active", "height_cm",
         "weight_kg", "age", "gender", "activity_level"),
        (),
        (),
    ),
    "active_profile": (
        ("user_id", "profile_id"),
        (),
        (),
    ),
    "meals": (
        ("id", "profile_id", "owner_user_id", "eaten_at", "logged_at",
         "description", "calories", "protein_g", "carbs_g", "fat_g",
         "raw_llm_response", "photo_path"),
        ("eaten_at", "logged_at"),
        (),
    ),
    "goals": (
        ("profile_id", "daily_calories", "daily_protein_g",
         "daily_carbs_g", "daily_fat_g"),
        (),
        (),
    ),
    "supplements": (
        ("id", "profile_id", "owner_user_id", "name", "reminder_time",
         "dose", "active"),
        (),
        (),
    ),
    "supplement_logs": (
        ("id", "supplement_id", "profile_id", "taken_at"),
        ("taken_at",),
        (),
    ),
    "piano_sessions": (
        ("id", "owner_user_id", "practiced_at", "duration_minutes",
         "notes", "pieces_practiced", "logged_at"),
        ("logged_at",),
        ("practiced_at",),
    ),
    "piano_pieces": (
        ("id", "owner_user_id", "title", "composer", "status",
         "added_at", "last_practiced_at", "notes"),
        (),
        ("added_at", "last_practiced_at"),
    ),
    "piano_recordings": (
        ("id", "owner_user_id", "piece_id", "recorded_at", "file_path",
         "duration_seconds", "feedback_summary", "raw_analysis"),
        ("recorded_at",),
        (),
    ),
    "piano_streak": (
        ("owner_user_id", "current_streak", "longest_streak",
         "last_practiced_date"),
        (),
        ("last_practiced_date",),
    ),
    "liquids": (
        ("id", "profile_id", "owner_user_id", "drunk_at", "logged_at",
         "description", "amount_ml", "calories", "protein_g", "carbs_g",
         "fat_g", "raw_llm_response"),
        ("drunk_at", "logged_at"),
        (),
    ),
}

# Tables whose PK is `id` and driven by a SERIAL sequence.
SERIAL_TABLES: tuple[str, ...] = (
    "profiles", "meals", "supplements", "supplement_logs",
    "piano_sessions", "piano_pieces", "piano_recordings", "liquids",
)


def _parse_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.replace(" ", "T"))
    else:
        raise TypeError(f"Unexpected datetime value: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=WARSAW)
    return dt


def _parse_date(value: object) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise TypeError(f"Unexpected date value: {value!r}")


def _coerce_row(
    row: sqlite3.Row,
    columns: tuple[str, ...],
    datetime_cols: tuple[str, ...],
    date_cols: tuple[str, ...],
) -> list[object | None]:
    out: list[object | None] = []
    for col in columns:
        raw = row[col] if col in row.keys() else None
        if col in datetime_cols:
            out.append(_parse_datetime(raw))
        elif col in date_cols:
            out.append(_parse_date(raw))
        elif col == "active" and isinstance(raw, int):
            out.append(bool(raw))
        else:
            out.append(raw)
    return out


async def _copy_table(
    sqlite_conn: sqlite3.Connection,
    pg_conn: asyncpg.Connection,
    table: str,
) -> int:
    columns, datetime_cols, date_cols = TABLE_SPECS[table]
    cur = sqlite_conn.execute(f"SELECT {', '.join(columns)} FROM {table}")
    rows = cur.fetchall()
    if not rows:
        return 0

    coerced = [_coerce_row(row, columns, datetime_cols, date_cols) for row in rows]
    placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({placeholders})"
    )
    await pg_conn.executemany(sql, coerced)
    return len(rows)


async def _reset_sequence(pg_conn: asyncpg.Connection, table: str) -> None:
    # setval(..., value, is_called=true): next nextval() returns value+1.
    # When the table is empty MAX(id) is NULL; fall back to 1 with is_called=false
    # so the first generated id is 1.
    await pg_conn.execute(
        f"""
        SELECT setval(
            pg_get_serial_sequence('{table}', 'id'),
            COALESCE((SELECT MAX(id) FROM {table}), 1),
            (SELECT COUNT(*) > 0 FROM {table})
        )
        """
    )


async def _truncate_all(pg_conn: asyncpg.Connection) -> None:
    tables = ", ".join(TABLES_IN_INSERT_ORDER)
    await pg_conn.execute(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")


async def main() -> None:
    sqlite_path = Path(os.getenv("SQLITE_PATH", "./data/caloriebot.db"))
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL not set")
    if not sqlite_path.exists():
        raise SystemExit(f"SQLite DB not found at {sqlite_path}")

    logger.info("Reading SQLite from %s", sqlite_path)
    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    logger.info("Connecting to Postgres...")
    pg_conn = await asyncpg.connect(database_url)
    try:
        await pg_conn.execute("SET TIME ZONE 'Europe/Warsaw'")
        async with pg_conn.transaction():
            logger.info("Truncating target tables")
            await _truncate_all(pg_conn)

            for table in TABLES_IN_INSERT_ORDER:
                count = await _copy_table(sqlite_conn, pg_conn, table)
                logger.info("  %-20s %d rows", table, count)

            for table in SERIAL_TABLES:
                await _reset_sequence(pg_conn, table)
            logger.info("Sequences reset")
    finally:
        await pg_conn.close()
        sqlite_conn.close()

    logger.info("Migration complete")


if __name__ == "__main__":
    asyncio.run(main())
