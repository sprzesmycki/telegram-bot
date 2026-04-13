from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
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

    cursor = await db.execute("PRAGMA table_info(profiles)")
    prof_columns = {row["name"] for row in await cursor.fetchall()}
    new_prof_cols = {
        "height_cm": "REAL",
        "weight_kg": "REAL",
        "age": "INTEGER",
        "gender": "TEXT",
        "activity_level": "TEXT",
    }
    for col, col_type in new_prof_cols.items():
        if col not in prof_columns:
            await db.execute(f"ALTER TABLE profiles ADD COLUMN {col} {col_type}")
            logger.info("Migration: added profiles.%s", col)

    cursor = await db.execute("PRAGMA table_info(goals)")
    goal_columns = {row["name"] for row in await cursor.fetchall()}
    new_goal_cols = {
        "daily_protein_g": "REAL",
        "daily_carbs_g": "REAL",
        "daily_fat_g": "REAL",
    }
    for col, col_type in new_goal_cols.items():
        if col not in goal_columns:
            await db.execute(f"ALTER TABLE goals ADD COLUMN {col} {col_type}")
            logger.info("Migration: added goals.%s", col)

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS liquids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL REFERENCES profiles(id),
            owner_user_id BIGINT NOT NULL,
            drunk_at DATETIME NOT NULL,
            logged_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            description TEXT NOT NULL,
            amount_ml INTEGER NOT NULL,
            calories INTEGER DEFAULT 0,
            protein_g REAL DEFAULT 0,
            carbs_g REAL DEFAULT 0,
            fat_g REAL DEFAULT 0,
            raw_llm_response TEXT
        )
        """
    )


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


async def update_profile(
    profile_id: int,
    owner_id: int,
    height_cm: float | None = None,
    weight_kg: float | None = None,
    age: int | None = None,
    gender: str | None = None,
    activity_level: str | None = None,
) -> None:
    db = get_db()
    updates = []
    params = []

    if height_cm is not None:
        updates.append("height_cm = ?")
        params.append(height_cm)
    if weight_kg is not None:
        updates.append("weight_kg = ?")
        params.append(weight_kg)
    if age is not None:
        updates.append("age = ?")
        params.append(age)
    if gender is not None:
        updates.append("gender = ?")
        params.append(gender)
    if activity_level is not None:
        updates.append("activity_level = ?")
        params.append(activity_level)

    if not updates:
        return

    params.extend([profile_id, owner_id])
    sql = f"UPDATE profiles SET {', '.join(updates)} WHERE id = ? AND owner_user_id = ?"
    await db.execute(sql, tuple(params))
    await db.commit()


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


# ---------------------------------------------------------------------------
# Liquid helpers
# ---------------------------------------------------------------------------

async def log_liquid(
    profile_id: int,
    owner_id: int,
    drunk_at: datetime,
    description: str,
    amount_ml: int,
    calories: int,
    protein_g: float,
    carbs_g: float,
    fat_g: float,
    raw_llm: str,
) -> int:
    db = get_db()
    cursor = await db.execute(
        """
        INSERT INTO liquids
            (profile_id, owner_user_id, drunk_at, description,
             amount_ml, calories, protein_g, carbs_g, fat_g, raw_llm_response)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (profile_id, owner_id, drunk_at.isoformat(), description,
         amount_ml, calories, protein_g, carbs_g, fat_g, raw_llm),
    )
    await db.commit()
    return cursor.lastrowid


async def get_liquids_today(profile_id: int, owner_id: int) -> list[dict]:
    db = get_db()
    cursor = await db.execute(
        """
        SELECT * FROM liquids
        WHERE date(drunk_at) = date('now', 'localtime')
          AND profile_id = ?
          AND owner_user_id = ?
        ORDER BY drunk_at
        """,
        (profile_id, owner_id),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def delete_meal(meal_id: int, owner_id: int) -> bool:
    db = get_db()
    cursor = await db.execute(
        "DELETE FROM meals WHERE id = ? AND owner_user_id = ?",
        (meal_id, owner_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def delete_liquid(liquid_id: int, owner_id: int) -> bool:
    db = get_db()
    cursor = await db.execute(
        "DELETE FROM liquids WHERE id = ? AND owner_user_id = ?",
        (liquid_id, owner_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_meal_by_id(meal_id: int, owner_id: int) -> dict | None:
    db = get_db()
    cursor = await db.execute(
        "SELECT * FROM meals WHERE id = ? AND owner_user_id = ?",
        (meal_id, owner_id),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_liquid_by_id(liquid_id: int, owner_id: int) -> dict | None:
    db = get_db()
    cursor = await db.execute(
        "SELECT * FROM liquids WHERE id = ? AND owner_user_id = ?",
        (liquid_id, owner_id),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_liquids_range(
    profile_id: int, owner_id: int, start: str, end: str
) -> list[dict]:
    db = get_db()
    cursor = await db.execute(
        """
        SELECT * FROM liquids
        WHERE drunk_at BETWEEN ? AND ?
          AND profile_id = ?
          AND owner_user_id = ?
        ORDER BY drunk_at
        """,
        (start, end, profile_id, owner_id),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_daily_hydration(profile_id: int, owner_id: int) -> int:
    """Return total ml drunk today."""
    db = get_db()
    cursor = await db.execute(
        """
        SELECT COALESCE(SUM(amount_ml), 0)
        FROM liquids
        WHERE date(drunk_at) = date('now', 'localtime')
          AND profile_id = ?
          AND owner_user_id = ?
        """,
        (profile_id, owner_id),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


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
        WITH daily_meals AS (
            SELECT
                COALESCE(SUM(calories), 0)   AS calories,
                COALESCE(SUM(protein_g), 0)  AS protein_g,
                COALESCE(SUM(carbs_g), 0)    AS carbs_g,
                COALESCE(SUM(fat_g), 0)      AS fat_g
            FROM meals
            WHERE date(eaten_at) = date('now', 'localtime')
              AND profile_id = ?
              AND owner_user_id = ?
        ),
        daily_liquids AS (
            SELECT
                COALESCE(SUM(calories), 0)   AS calories,
                COALESCE(SUM(protein_g), 0)  AS protein_g,
                COALESCE(SUM(carbs_g), 0)    AS carbs_g,
                COALESCE(SUM(fat_g), 0)      AS fat_g
            FROM liquids
            WHERE date(drunk_at) = date('now', 'localtime')
              AND profile_id = ?
              AND owner_user_id = ?
        )
        SELECT
            m.calories + l.calories AS calories,
            m.protein_g + l.protein_g AS protein_g,
            m.carbs_g + l.carbs_g AS carbs_g,
            m.fat_g + l.fat_g AS fat_g
        FROM daily_meals m, daily_liquids l
        """,
        (profile_id, owner_id, profile_id, owner_id),
    )
    row = await cursor.fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# Goal helpers
# ---------------------------------------------------------------------------

async def set_goal(
    profile_id: int,
    daily_calories: int,
    protein_g: float | None = None,
    carbs_g: float | None = None,
    fat_g: float | None = None,
) -> None:
    db = get_db()
    await db.execute(
        """
        INSERT INTO goals (profile_id, daily_calories, daily_protein_g, daily_carbs_g, daily_fat_g)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(profile_id) DO UPDATE SET
            daily_calories = excluded.daily_calories,
            daily_protein_g = excluded.daily_protein_g,
            daily_carbs_g = excluded.daily_carbs_g,
            daily_fat_g = excluded.daily_fat_g
        """,
        (profile_id, daily_calories, protein_g, carbs_g, fat_g),
    )
    await db.commit()


async def get_goal(profile_id: int) -> dict:
    db = get_db()
    cursor = await db.execute(
        "SELECT * FROM goals WHERE profile_id = ?",
        (profile_id,),
    )
    row = await cursor.fetchone()
    if row:
        return dict(row)
    return {
        "daily_calories": 2000,
        "daily_protein_g": None,
        "daily_carbs_g": None,
        "daily_fat_g": None,
    }


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


# ---------------------------------------------------------------------------
# Piano: sessions
# ---------------------------------------------------------------------------

def _load_pieces_json(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


async def log_piano_session(
    owner_id: int,
    practiced_at: date,
    duration_minutes: int | None,
    notes: str | None,
    pieces_practiced: list[str],
) -> int:
    db = get_db()
    cursor = await db.execute(
        """
        INSERT INTO piano_sessions
            (owner_user_id, practiced_at, duration_minutes, notes, pieces_practiced)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            owner_id,
            practiced_at.isoformat(),
            duration_minutes,
            notes,
            json.dumps(pieces_practiced or []),
        ),
    )
    await db.commit()
    return cursor.lastrowid


def _session_row_to_dict(row: aiosqlite.Row) -> dict:
    data = dict(row)
    data["pieces_practiced"] = _load_pieces_json(data.get("pieces_practiced"))
    return data


async def list_piano_sessions(owner_id: int, limit: int = 7) -> list[dict]:
    db = get_db()
    cursor = await db.execute(
        """
        SELECT * FROM piano_sessions
        WHERE owner_user_id = ?
        ORDER BY practiced_at DESC, logged_at DESC
        LIMIT ?
        """,
        (owner_id, limit),
    )
    rows = await cursor.fetchall()
    return [_session_row_to_dict(row) for row in rows]


async def get_piano_session_today(owner_id: int) -> dict | None:
    db = get_db()
    cursor = await db.execute(
        """
        SELECT * FROM piano_sessions
        WHERE owner_user_id = ?
          AND practiced_at = date('now', 'localtime')
        ORDER BY logged_at DESC
        LIMIT 1
        """,
        (owner_id,),
    )
    row = await cursor.fetchone()
    return _session_row_to_dict(row) if row else None


async def piano_total_stats(owner_id: int) -> dict:
    db = get_db()
    cursor = await db.execute(
        """
        SELECT
            COUNT(*) AS total_sessions,
            COALESCE(SUM(duration_minutes), 0) AS total_minutes
        FROM piano_sessions
        WHERE owner_user_id = ?
        """,
        (owner_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else {"total_sessions": 0, "total_minutes": 0}


async def get_piano_owners() -> list[int]:
    """Return distinct owner IDs that have ever logged a piano session OR piece."""
    db = get_db()
    cursor = await db.execute(
        """
        SELECT DISTINCT owner_user_id FROM piano_sessions
        UNION
        SELECT DISTINCT owner_user_id FROM piano_pieces
        """
    )
    rows = await cursor.fetchall()
    return [int(row[0] if isinstance(row, tuple) else row["owner_user_id"]) for row in rows]


# ---------------------------------------------------------------------------
# Piano: pieces
# ---------------------------------------------------------------------------

async def add_piano_piece(
    owner_id: int, title: str, composer: str | None = None
) -> int:
    db = get_db()
    cursor = await db.execute(
        """
        INSERT INTO piano_pieces (owner_user_id, title, composer)
        VALUES (?, ?, ?)
        """,
        (owner_id, title, composer),
    )
    await db.commit()
    return cursor.lastrowid


async def remove_piano_piece(owner_id: int, piece_id: int) -> bool:
    db = get_db()
    cursor = await db.execute(
        "DELETE FROM piano_pieces WHERE id = ? AND owner_user_id = ?",
        (piece_id, owner_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def list_piano_pieces(
    owner_id: int, status: str | None = None
) -> list[dict]:
    db = get_db()
    if status is None:
        cursor = await db.execute(
            """
            SELECT * FROM piano_pieces
            WHERE owner_user_id = ?
            ORDER BY status, title
            """,
            (owner_id,),
        )
    else:
        cursor = await db.execute(
            """
            SELECT * FROM piano_pieces
            WHERE owner_user_id = ? AND status = ?
            ORDER BY title
            """,
            (owner_id, status),
        )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def find_piano_piece_by_title(
    owner_id: int, title: str
) -> dict | None:
    db = get_db()
    cursor = await db.execute(
        """
        SELECT * FROM piano_pieces
        WHERE owner_user_id = ? AND LOWER(title) = LOWER(?)
        LIMIT 1
        """,
        (owner_id, title),
    )
    row = await cursor.fetchone()
    if row:
        return dict(row)
    cursor = await db.execute(
        """
        SELECT * FROM piano_pieces
        WHERE owner_user_id = ? AND LOWER(title) LIKE LOWER(?)
        ORDER BY LENGTH(title)
        LIMIT 1
        """,
        (owner_id, f"%{title}%"),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def update_piano_piece_status(
    owner_id: int, piece_id: int, status: str
) -> bool:
    db = get_db()
    cursor = await db.execute(
        """
        UPDATE piano_pieces SET status = ?
        WHERE id = ? AND owner_user_id = ?
        """,
        (status, piece_id, owner_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def update_piano_piece_note(
    owner_id: int, piece_id: int, notes: str
) -> bool:
    db = get_db()
    cursor = await db.execute(
        """
        UPDATE piano_pieces SET notes = ?
        WHERE id = ? AND owner_user_id = ?
        """,
        (notes, piece_id, owner_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def touch_piano_piece_last_practiced(
    owner_id: int, piece_id: int, practiced_at: date
) -> None:
    db = get_db()
    await db.execute(
        """
        UPDATE piano_pieces SET last_practiced_at = ?
        WHERE id = ? AND owner_user_id = ?
        """,
        (practiced_at.isoformat(), piece_id, owner_id),
    )
    await db.commit()


async def most_practiced_piece(owner_id: int) -> dict | None:
    """Return the piece that appears most often across piano_sessions.pieces_practiced.

    Counts occurrences in Python — piano data volume is tiny, so no need for
    JSON-aware SQL.
    """
    sessions = await list_piano_sessions(owner_id, limit=1000)
    if not sessions:
        return None

    counts: dict[str, int] = {}
    for session in sessions:
        for name in session["pieces_practiced"] or []:
            key = name.strip()
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1

    if not counts:
        return None

    top_title, top_count = max(counts.items(), key=lambda kv: kv[1])
    piece = await find_piano_piece_by_title(owner_id, top_title)
    return {
        "title": (piece["title"] if piece else top_title),
        "composer": piece["composer"] if piece else None,
        "count": top_count,
    }


# ---------------------------------------------------------------------------
# Piano: streak
# ---------------------------------------------------------------------------

async def get_piano_streak(owner_id: int) -> dict:
    db = get_db()
    cursor = await db.execute(
        "SELECT * FROM piano_streak WHERE owner_user_id = ?",
        (owner_id,),
    )
    row = await cursor.fetchone()
    if row:
        return dict(row)
    return {
        "owner_user_id": owner_id,
        "current_streak": 0,
        "longest_streak": 0,
        "last_practiced_date": None,
    }


async def upsert_piano_streak(
    owner_id: int,
    current_streak: int,
    longest_streak: int,
    last_practiced_date: date | None,
) -> None:
    db = get_db()
    await db.execute(
        """
        INSERT INTO piano_streak
            (owner_user_id, current_streak, longest_streak, last_practiced_date)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(owner_user_id) DO UPDATE SET
            current_streak = excluded.current_streak,
            longest_streak = excluded.longest_streak,
            last_practiced_date = excluded.last_practiced_date
        """,
        (
            owner_id,
            current_streak,
            longest_streak,
            last_practiced_date.isoformat() if last_practiced_date else None,
        ),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Piano: recordings
# ---------------------------------------------------------------------------

async def add_piano_recording(
    owner_id: int,
    piece_id: int | None,
    file_path: str | None,
    duration_seconds: int | None,
    feedback_summary: str | None,
    raw_analysis: str | None,
) -> int:
    db = get_db()
    cursor = await db.execute(
        """
        INSERT INTO piano_recordings
            (owner_user_id, piece_id, file_path, duration_seconds,
             feedback_summary, raw_analysis)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (owner_id, piece_id, file_path, duration_seconds, feedback_summary, raw_analysis),
    )
    await db.commit()
    return cursor.lastrowid


async def list_piano_recordings(
    owner_id: int, piece_id: int | None = None, limit: int = 10
) -> list[dict]:
    db = get_db()
    if piece_id is None:
        cursor = await db.execute(
            """
            SELECT * FROM piano_recordings
            WHERE owner_user_id = ?
            ORDER BY recorded_at DESC
            LIMIT ?
            """,
            (owner_id, limit),
        )
    else:
        cursor = await db.execute(
            """
            SELECT * FROM piano_recordings
            WHERE owner_user_id = ? AND piece_id = ?
            ORDER BY recorded_at DESC
            LIMIT ?
            """,
            (owner_id, piece_id, limit),
        )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]
