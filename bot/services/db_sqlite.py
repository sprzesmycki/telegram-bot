from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_db: aiosqlite.Connection | None = None


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

async def init_db() -> None:
    global _db
    db_path = os.getenv("SQLITE_PATH", "./data/caloriebot.db")
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    _db = await aiosqlite.connect(db_path)
    _db.row_factory = aiosqlite.Row

    init_sql = Path(__file__).parent.parent.parent / "migrations" / "init.sql"
    schema = init_sql.read_text()
    await _db.executescript(schema)
    await _apply_migrations(_db)
    await _db.commit()
    logger.info("Database initialised at %s", db_path)


async def _apply_migrations(db: aiosqlite.Connection) -> None:
    """Idempotent additive migrations for already-created databases."""
    cursor = await db.execute("PRAGMA table_info(meals)")
    columns = {row["name"] for row in await cursor.fetchall()}
    if "photo_path" not in columns:
        await db.execute("ALTER TABLE meals ADD COLUMN photo_path TEXT")
        logger.info("Migration: added meals.photo_path")

    cursor = await db.execute("PRAGMA table_info(supplements)")
    sup_columns = {row["name"] for row in await cursor.fetchall()}
    if "dose" not in sup_columns:
        await db.execute("ALTER TABLE supplements ADD COLUMN dose TEXT")
        logger.info("Migration: added supplements.dose")


def get_db() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("Database not initialised – call init_db() first")
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None
        logger.info("Database connection closed")


# ---------------------------------------------------------------------------
# Profile helpers
# ---------------------------------------------------------------------------

async def create_profile(owner_id: int, name: str) -> int:
    db = get_db()
    cursor = await db.execute(
        "INSERT INTO profiles (owner_user_id, name) VALUES (?, ?)",
        (owner_id, name),
    )
    await db.commit()
    return cursor.lastrowid


async def list_profiles(owner_id: int) -> list[dict]:
    db = get_db()
    cursor = await db.execute(
        "SELECT * FROM profiles WHERE owner_user_id = ? AND active = 1",
        (owner_id,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_active_profile(owner_id: int) -> dict | None:
    db = get_db()
    cursor = await db.execute(
        """
        SELECT p.*
        FROM active_profile ap
        JOIN profiles p ON p.id = ap.profile_id
        WHERE ap.user_id = ? AND p.active = 1
        """,
        (owner_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def set_active_profile(owner_id: int, profile_id: int) -> None:
    db = get_db()
    await db.execute(
        "INSERT OR REPLACE INTO active_profile (user_id, profile_id) VALUES (?, ?)",
        (owner_id, profile_id),
    )
    await db.commit()


async def delete_profile(owner_id: int, name: str) -> bool:
    db = get_db()
    cursor = await db.execute(
        "UPDATE profiles SET active = 0 WHERE owner_user_id = ? AND name = ?",
        (owner_id, name),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_profile_by_name(owner_id: int, name: str) -> dict | None:
    db = get_db()
    cursor = await db.execute(
        "SELECT * FROM profiles WHERE owner_user_id = ? AND name = ? AND active = 1",
        (owner_id, name),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def ensure_default_profile(owner_id: int) -> dict:
    profiles = await list_profiles(owner_id)
    if not profiles:
        profile_id = await create_profile(owner_id, "Me")
        await set_active_profile(owner_id, profile_id)

    active = await get_active_profile(owner_id)
    if active is None:
        # Profiles exist but none is set as active – pick the first one.
        profiles = await list_profiles(owner_id)
        await set_active_profile(owner_id, profiles[0]["id"])
        active = await get_active_profile(owner_id)
    return active


async def get_all_profiles(owner_id: int) -> list[dict]:
    return await list_profiles(owner_id)


# ---------------------------------------------------------------------------
# Meal helpers
# ---------------------------------------------------------------------------

async def log_meal(
    profile_id: int,
    owner_id: int,
    eaten_at: datetime,
    description: str,
    calories: int,
    protein_g: float,
    carbs_g: float,
    fat_g: float,
    raw_llm: str,
    photo_path: str | None = None,
) -> int:
    db = get_db()
    cursor = await db.execute(
        """
        INSERT INTO meals
            (profile_id, owner_user_id, eaten_at, description,
             calories, protein_g, carbs_g, fat_g, raw_llm_response, photo_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (profile_id, owner_id, eaten_at.isoformat(), description,
         calories, protein_g, carbs_g, fat_g, raw_llm, photo_path),
    )
    await db.commit()
    return cursor.lastrowid


async def get_meals_today(profile_id: int, owner_id: int) -> list[dict]:
    db = get_db()
    cursor = await db.execute(
        """
        SELECT * FROM meals
        WHERE date(eaten_at) = date('now', 'localtime')
          AND profile_id = ?
          AND owner_user_id = ?
        ORDER BY eaten_at
        """,
        (profile_id, owner_id),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_meals_range(
    profile_id: int, owner_id: int, start: str, end: str
) -> list[dict]:
    db = get_db()
    cursor = await db.execute(
        """
        SELECT * FROM meals
        WHERE eaten_at BETWEEN ? AND ?
          AND profile_id = ?
          AND owner_user_id = ?
        ORDER BY eaten_at
        """,
        (start, end, profile_id, owner_id),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_daily_totals(profile_id: int, owner_id: int) -> dict:
    db = get_db()
    cursor = await db.execute(
        """
        SELECT
            COALESCE(SUM(calories), 0)   AS calories,
            COALESCE(SUM(protein_g), 0)  AS protein_g,
            COALESCE(SUM(carbs_g), 0)    AS carbs_g,
            COALESCE(SUM(fat_g), 0)      AS fat_g
        FROM meals
        WHERE date(eaten_at) = date('now', 'localtime')
          AND profile_id = ?
          AND owner_user_id = ?
        """,
        (profile_id, owner_id),
    )
    row = await cursor.fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# Goal helpers
# ---------------------------------------------------------------------------

async def set_goal(profile_id: int, daily_calories: int) -> None:
    db = get_db()
    await db.execute(
        "INSERT OR REPLACE INTO goals (profile_id, daily_calories) VALUES (?, ?)",
        (profile_id, daily_calories),
    )
    await db.commit()


async def get_goal(profile_id: int) -> int:
    db = get_db()
    cursor = await db.execute(
        "SELECT daily_calories FROM goals WHERE profile_id = ?",
        (profile_id,),
    )
    row = await cursor.fetchone()
    return row["daily_calories"] if row else 2000


# ---------------------------------------------------------------------------
# Supplement helpers
# ---------------------------------------------------------------------------

async def add_supplement(
    profile_id: int, owner_id: int, name: str, reminder_time: str, dose: str | None = None
) -> int:
    db = get_db()
    cursor = await db.execute(
        """
        INSERT INTO supplements (profile_id, owner_user_id, name, reminder_time, dose)
        VALUES (?, ?, ?, ?, ?)
        """,
        (profile_id, owner_id, name, reminder_time, dose),
    )
    await db.commit()
    return cursor.lastrowid


async def list_supplements(profile_id: int, owner_id: int) -> list[dict]:
    db = get_db()
    cursor = await db.execute(
        """
        SELECT * FROM supplements
        WHERE profile_id = ? AND owner_user_id = ? AND active = 1
        """,
        (profile_id, owner_id),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_all_active_supplements() -> list[dict]:
    db = get_db()
    cursor = await db.execute(
        """
        SELECT s.*, p.name AS profile_name
        FROM supplements s
        JOIN profiles p ON p.id = s.profile_id
        WHERE s.active = 1
        """,
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def remove_supplement(
    profile_id: int, owner_id: int, name: str
) -> bool:
    db = get_db()
    cursor = await db.execute(
        """
        UPDATE supplements SET active = 0
        WHERE profile_id = ? AND owner_user_id = ? AND name = ?
        """,
        (profile_id, owner_id, name),
    )
    await db.commit()
    return cursor.rowcount > 0


async def log_supplement_taken(supplement_id: int, profile_id: int) -> None:
    db = get_db()
    await db.execute(
        "INSERT INTO supplement_logs (supplement_id, profile_id) VALUES (?, ?)",
        (supplement_id, profile_id),
    )
    await db.commit()


async def get_supplement_logs_today(profile_id: int) -> list[dict]:
    db = get_db()
    cursor = await db.execute(
        """
        SELECT * FROM supplement_logs
        WHERE date(taken_at) = date('now', 'localtime')
          AND profile_id = ?
        """,
        (profile_id,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_supplement_by_id(supplement_id: int) -> dict | None:
    db = get_db()
    cursor = await db.execute(
        """
        SELECT s.*, p.name AS profile_name
        FROM supplements s
        JOIN profiles p ON p.id = s.profile_id
        WHERE s.id = ?
        """,
        (supplement_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_supplement_by_name(
    profile_id: int, owner_id: int, name: str
) -> dict | None:
    db = get_db()
    cursor = await db.execute(
        """
        SELECT * FROM supplements
        WHERE profile_id = ? AND owner_user_id = ? AND name = ? AND active = 1
        """,
        (profile_id, owner_id, name),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None
