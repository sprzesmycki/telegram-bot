"""PostgreSQL data access layer.

Single source of truth. The connection pool pins session timezone to
Europe/Warsaw so ``CURRENT_DATE`` and ``column::date`` use the operator's
local calendar day -- the same semantics the original SQLite queries had via
``date('now', 'localtime')``. Schema is owned by Alembic; this module only
executes queries.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import asyncpg

logger = logging.getLogger(__name__)

WARSAW = ZoneInfo("Europe/Warsaw")

_pool: asyncpg.Pool | None = None


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------


async def init_db() -> None:
    global _pool
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL must be set")

    _pool = await asyncpg.create_pool(
        database_url,
        min_size=1,
        max_size=10,
        server_settings={"timezone": "Europe/Warsaw"},
    )
    logger.info("Postgres pool ready (timezone=Europe/Warsaw)")


async def close_db() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _pool_or_raise() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database not initialised -- call init_db() first")
    return _pool


# ---------------------------------------------------------------------------
# Input coercion helpers
# ---------------------------------------------------------------------------


def _to_dt(value: str | date | datetime | None) -> datetime | None:
    """Coerce a date-ish input to a timezone-aware datetime.

    Naive inputs are assumed to be Europe/Warsaw local time.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=WARSAW)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=WARSAW)
    if isinstance(value, str):
        s = value.replace(" ", "T")
        # Accept both full ISO datetimes and bare dates.
        if "T" in s:
            dt = datetime.fromisoformat(s)
        else:
            dt = datetime.fromisoformat(s[:10] + "T00:00:00")
        return dt if dt.tzinfo else dt.replace(tzinfo=WARSAW)
    raise TypeError(f"Cannot coerce {type(value).__name__} to datetime")


def _load_pieces_json(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


async def create_profile(owner_id: int, name: str) -> int:
    pool = _pool_or_raise()
    return await pool.fetchval(
        "INSERT INTO profiles (owner_user_id, name) VALUES ($1, $2) RETURNING id",
        owner_id, name,
    )


async def update_profile(
    profile_id: int,
    owner_id: int,
    height_cm: float | None = None,
    weight_kg: float | None = None,
    age: int | None = None,
    gender: str | None = None,
    activity_level: str | None = None,
) -> None:
    fields = [
        ("height_cm", height_cm),
        ("weight_kg", weight_kg),
        ("age", age),
        ("gender", gender),
        ("activity_level", activity_level),
    ]
    updates: list[str] = []
    params: list[object] = []
    for col, val in fields:
        if val is None:
            continue
        params.append(val)
        updates.append(f"{col} = ${len(params)}")

    if not updates:
        return

    params.extend([profile_id, owner_id])
    sql = (
        f"UPDATE profiles SET {', '.join(updates)} "
        f"WHERE id = ${len(params) - 1} AND owner_user_id = ${len(params)}"
    )
    await _pool_or_raise().execute(sql, *params)


async def list_profiles(owner_id: int) -> list[dict]:
    rows = await _pool_or_raise().fetch(
        "SELECT * FROM profiles WHERE owner_user_id = $1 AND active = TRUE ORDER BY id",
        owner_id,
    )
    return [dict(r) for r in rows]


async def get_active_profile(owner_id: int) -> dict | None:
    row = await _pool_or_raise().fetchrow(
        """
        SELECT p.*
        FROM active_profile ap
        JOIN profiles p ON p.id = ap.profile_id
        WHERE ap.user_id = $1 AND p.active = TRUE
        """,
        owner_id,
    )
    return dict(row) if row else None


async def set_active_profile(owner_id: int, profile_id: int) -> None:
    await _pool_or_raise().execute(
        """
        INSERT INTO active_profile (user_id, profile_id) VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE SET profile_id = EXCLUDED.profile_id
        """,
        owner_id, profile_id,
    )


async def delete_profile(owner_id: int, name: str) -> bool:
    row = await _pool_or_raise().fetchrow(
        "UPDATE profiles SET active = FALSE "
        "WHERE owner_user_id = $1 AND name = $2 AND active = TRUE RETURNING id",
        owner_id, name,
    )
    return row is not None


async def get_profile_by_name(owner_id: int, name: str) -> dict | None:
    row = await _pool_or_raise().fetchrow(
        "SELECT * FROM profiles WHERE owner_user_id = $1 AND name = $2 AND active = TRUE",
        owner_id, name,
    )
    return dict(row) if row else None


async def get_profile_by_id(profile_id: int) -> dict | None:
    row = await _pool_or_raise().fetchrow(
        "SELECT * FROM profiles WHERE id = $1 AND active = TRUE",
        profile_id,
    )
    return dict(row) if row else None


async def ensure_default_profile(owner_id: int) -> dict:
    profiles = await list_profiles(owner_id)
    if not profiles:
        profile_id = await create_profile(owner_id, "Me")
        await set_active_profile(owner_id, profile_id)

    active = await get_active_profile(owner_id)
    if active is None:
        profiles = await list_profiles(owner_id)
        await set_active_profile(owner_id, profiles[0]["id"])
        active = await get_active_profile(owner_id)
    return active


async def get_all_profiles(owner_id: int) -> list[dict]:
    return await list_profiles(owner_id)


async def get_distinct_profile_owner_ids() -> list[int]:
    rows = await _pool_or_raise().fetch(
        "SELECT DISTINCT owner_user_id FROM profiles WHERE active = TRUE"
    )
    return [int(r["owner_user_id"]) for r in rows]


# ---------------------------------------------------------------------------
# Meals
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
    return await _pool_or_raise().fetchval(
        """
        INSERT INTO meals
            (profile_id, owner_user_id, eaten_at, description,
             calories, protein_g, carbs_g, fat_g, raw_llm_response, photo_path)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING id
        """,
        profile_id, owner_id, _to_dt(eaten_at), description,
        calories, protein_g, carbs_g, fat_g, raw_llm, photo_path,
    )


async def get_meals_today(profile_id: int, owner_id: int) -> list[dict]:
    rows = await _pool_or_raise().fetch(
        """
        SELECT * FROM meals
        WHERE eaten_at::date = CURRENT_DATE
          AND profile_id = $1
          AND owner_user_id = $2
        ORDER BY eaten_at
        """,
        profile_id, owner_id,
    )
    return [dict(r) for r in rows]


async def get_meals_range(
    profile_id: int, owner_id: int,
    start: str | date | datetime,
    end: str | date | datetime,
) -> list[dict]:
    rows = await _pool_or_raise().fetch(
        """
        SELECT * FROM meals
        WHERE eaten_at >= $1 AND eaten_at < $2
          AND profile_id = $3
          AND owner_user_id = $4
        ORDER BY eaten_at
        """,
        _to_dt(start), _to_dt(end), profile_id, owner_id,
    )
    return [dict(r) for r in rows]


async def delete_meal(meal_id: int, owner_id: int) -> bool:
    row = await _pool_or_raise().fetchrow(
        "DELETE FROM meals WHERE id = $1 AND owner_user_id = $2 RETURNING id",
        meal_id, owner_id,
    )
    return row is not None


async def get_meal_by_id(meal_id: int, owner_id: int) -> dict | None:
    row = await _pool_or_raise().fetchrow(
        "SELECT * FROM meals WHERE id = $1 AND owner_user_id = $2",
        meal_id, owner_id,
    )
    return dict(row) if row else None


async def get_daily_totals(profile_id: int, owner_id: int) -> dict:
    row = await _pool_or_raise().fetchrow(
        """
        WITH daily_meals AS (
            SELECT
                COALESCE(SUM(calories), 0)::INTEGER AS calories,
                COALESCE(SUM(protein_g), 0)::REAL  AS protein_g,
                COALESCE(SUM(carbs_g), 0)::REAL    AS carbs_g,
                COALESCE(SUM(fat_g), 0)::REAL      AS fat_g
            FROM meals
            WHERE eaten_at::date = CURRENT_DATE
              AND profile_id = $1
              AND owner_user_id = $2
        ),
        daily_liquids AS (
            SELECT
                COALESCE(SUM(calories), 0)::INTEGER AS calories,
                COALESCE(SUM(protein_g), 0)::REAL  AS protein_g,
                COALESCE(SUM(carbs_g), 0)::REAL    AS carbs_g,
                COALESCE(SUM(fat_g), 0)::REAL      AS fat_g
            FROM liquids
            WHERE drunk_at::date = CURRENT_DATE
              AND profile_id = $1
              AND owner_user_id = $2
        )
        SELECT
            m.calories  + l.calories  AS calories,
            m.protein_g + l.protein_g AS protein_g,
            m.carbs_g   + l.carbs_g   AS carbs_g,
            m.fat_g     + l.fat_g     AS fat_g
        FROM daily_meals m, daily_liquids l
        """,
        profile_id, owner_id,
    )
    return dict(row)


# ---------------------------------------------------------------------------
# Liquids
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
    return await _pool_or_raise().fetchval(
        """
        INSERT INTO liquids
            (profile_id, owner_user_id, drunk_at, description,
             amount_ml, calories, protein_g, carbs_g, fat_g, raw_llm_response)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING id
        """,
        profile_id, owner_id, _to_dt(drunk_at), description,
        amount_ml, calories, protein_g, carbs_g, fat_g, raw_llm,
    )


async def get_liquids_today(profile_id: int, owner_id: int) -> list[dict]:
    rows = await _pool_or_raise().fetch(
        """
        SELECT * FROM liquids
        WHERE drunk_at::date = CURRENT_DATE
          AND profile_id = $1
          AND owner_user_id = $2
        ORDER BY drunk_at
        """,
        profile_id, owner_id,
    )
    return [dict(r) for r in rows]


async def get_liquids_range(
    profile_id: int, owner_id: int,
    start: str | date | datetime,
    end: str | date | datetime,
) -> list[dict]:
    rows = await _pool_or_raise().fetch(
        """
        SELECT * FROM liquids
        WHERE drunk_at >= $1 AND drunk_at < $2
          AND profile_id = $3
          AND owner_user_id = $4
        ORDER BY drunk_at
        """,
        _to_dt(start), _to_dt(end), profile_id, owner_id,
    )
    return [dict(r) for r in rows]


async def delete_liquid(liquid_id: int, owner_id: int) -> bool:
    row = await _pool_or_raise().fetchrow(
        "DELETE FROM liquids WHERE id = $1 AND owner_user_id = $2 RETURNING id",
        liquid_id, owner_id,
    )
    return row is not None


async def get_liquid_by_id(liquid_id: int, owner_id: int) -> dict | None:
    row = await _pool_or_raise().fetchrow(
        "SELECT * FROM liquids WHERE id = $1 AND owner_user_id = $2",
        liquid_id, owner_id,
    )
    return dict(row) if row else None


async def get_daily_hydration(profile_id: int, owner_id: int) -> int:
    val = await _pool_or_raise().fetchval(
        """
        SELECT COALESCE(SUM(amount_ml), 0)::INTEGER
        FROM liquids
        WHERE drunk_at::date = CURRENT_DATE
          AND profile_id = $1
          AND owner_user_id = $2
        """,
        profile_id, owner_id,
    )
    return val or 0


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------


async def set_goal(
    profile_id: int,
    daily_calories: int,
    protein_g: float | None = None,
    carbs_g: float | None = None,
    fat_g: float | None = None,
) -> None:
    await _pool_or_raise().execute(
        """
        INSERT INTO goals (profile_id, daily_calories, daily_protein_g, daily_carbs_g, daily_fat_g)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT(profile_id) DO UPDATE SET
            daily_calories  = EXCLUDED.daily_calories,
            daily_protein_g = EXCLUDED.daily_protein_g,
            daily_carbs_g   = EXCLUDED.daily_carbs_g,
            daily_fat_g     = EXCLUDED.daily_fat_g
        """,
        profile_id, daily_calories, protein_g, carbs_g, fat_g,
    )


async def get_goal(profile_id: int) -> dict:
    row = await _pool_or_raise().fetchrow(
        "SELECT * FROM goals WHERE profile_id = $1",
        profile_id,
    )
    if row:
        return dict(row)
    return {
        "daily_calories": 2000,
        "daily_protein_g": None,
        "daily_carbs_g": None,
        "daily_fat_g": None,
    }


# ---------------------------------------------------------------------------
# Supplements
# ---------------------------------------------------------------------------


async def add_supplement(
    profile_id: int, owner_id: int, name: str, reminder_time: str, dose: str | None = None,
) -> int:
    return await _pool_or_raise().fetchval(
        """
        INSERT INTO supplements (profile_id, owner_user_id, name, reminder_time, dose)
        VALUES ($1, $2, $3, $4, $5) RETURNING id
        """,
        profile_id, owner_id, name, reminder_time, dose,
    )


async def list_supplements(profile_id: int, owner_id: int) -> list[dict]:
    rows = await _pool_or_raise().fetch(
        "SELECT * FROM supplements WHERE profile_id = $1 AND owner_user_id = $2 AND active = TRUE",
        profile_id, owner_id,
    )
    return [dict(r) for r in rows]


async def get_all_active_supplements() -> list[dict]:
    rows = await _pool_or_raise().fetch(
        """
        SELECT s.*, p.name AS profile_name
        FROM supplements s
        JOIN profiles p ON p.id = s.profile_id
        WHERE s.active = TRUE
        """
    )
    return [dict(r) for r in rows]


async def remove_supplement(profile_id: int, owner_id: int, name: str) -> bool:
    row = await _pool_or_raise().fetchrow(
        """
        UPDATE supplements SET active = FALSE
        WHERE profile_id = $1 AND owner_user_id = $2 AND name = $3 AND active = TRUE
        RETURNING id
        """,
        profile_id, owner_id, name,
    )
    return row is not None


async def log_supplement_taken(supplement_id: int, profile_id: int) -> None:
    await _pool_or_raise().execute(
        "INSERT INTO supplement_logs (supplement_id, profile_id) VALUES ($1, $2)",
        supplement_id, profile_id,
    )


async def delete_supplement_log_today(supplement_id: int, profile_id: int) -> None:
    await _pool_or_raise().execute(
        """
        DELETE FROM supplement_logs
        WHERE supplement_id = $1
          AND profile_id = $2
          AND taken_at::date = CURRENT_DATE
        """,
        supplement_id,
        profile_id,
    )


async def get_supplement_logs_today(profile_id: int) -> list[dict]:
    rows = await _pool_or_raise().fetch(
        """
        SELECT * FROM supplement_logs
        WHERE taken_at::date = CURRENT_DATE
          AND profile_id = $1
        """,
        profile_id,
    )
    return [dict(r) for r in rows]


async def get_supplement_by_id(supplement_id: int) -> dict | None:
    row = await _pool_or_raise().fetchrow(
        """
        SELECT s.*, p.name AS profile_name
        FROM supplements s
        JOIN profiles p ON p.id = s.profile_id
        WHERE s.id = $1
        """,
        supplement_id,
    )
    return dict(row) if row else None


async def get_supplement_by_name(profile_id: int, owner_id: int, name: str) -> dict | None:
    row = await _pool_or_raise().fetchrow(
        """
        SELECT * FROM supplements
        WHERE profile_id = $1 AND owner_user_id = $2 AND name = $3 AND active = TRUE
        """,
        profile_id, owner_id, name,
    )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Piano: sessions
# ---------------------------------------------------------------------------


async def log_piano_session(
    owner_id: int,
    practiced_at: date,
    duration_minutes: int | None,
    notes: str | None,
    pieces_practiced: list[str],
) -> int:
    return await _pool_or_raise().fetchval(
        """
        INSERT INTO piano_sessions
            (owner_user_id, practiced_at, duration_minutes, notes, pieces_practiced)
        VALUES ($1, $2, $3, $4, $5) RETURNING id
        """,
        owner_id, practiced_at, duration_minutes, notes,
        json.dumps(pieces_practiced or []),
    )


async def start_piano_session(owner_id: int) -> datetime:
    """Start a new active practice session, overwriting any existing one."""
    now = datetime.now(WARSAW)
    await _pool_or_raise().execute(
        """
        INSERT INTO piano_active_sessions (owner_user_id, started_at)
        VALUES ($1, $2)
        ON CONFLICT (owner_user_id) DO UPDATE SET started_at = EXCLUDED.started_at
        """,
        owner_id, now,
    )
    return now


async def get_active_piano_session(owner_id: int) -> datetime | None:
    """Return the start time of the current active session, or None."""
    row = await _pool_or_raise().fetchrow(
        "SELECT started_at FROM piano_active_sessions WHERE owner_user_id = $1",
        owner_id,
    )
    return row["started_at"] if row else None


async def clear_active_piano_session(owner_id: int) -> bool:
    """Remove the active session record for this user."""
    row = await _pool_or_raise().fetchrow(
        "DELETE FROM piano_active_sessions WHERE owner_user_id = $1 RETURNING owner_user_id",
        owner_id,
    )
    return row is not None


def _session_row_to_dict(row: asyncpg.Record) -> dict:
    data = dict(row)
    data["pieces_practiced"] = _load_pieces_json(data.get("pieces_practiced"))
    return data


async def list_piano_sessions(owner_id: int, limit: int = 7) -> list[dict]:
    rows = await _pool_or_raise().fetch(
        """
        SELECT * FROM piano_sessions
        WHERE owner_user_id = $1
        ORDER BY practiced_at DESC, logged_at DESC
        LIMIT $2
        """,
        owner_id, limit,
    )
    return [_session_row_to_dict(r) for r in rows]


async def get_piano_session_today(owner_id: int) -> dict | None:
    row = await _pool_or_raise().fetchrow(
        """
        SELECT * FROM piano_sessions
        WHERE owner_user_id = $1
          AND practiced_at = CURRENT_DATE
        ORDER BY logged_at DESC
        LIMIT 1
        """,
        owner_id,
    )
    return _session_row_to_dict(row) if row else None


async def piano_total_stats(owner_id: int) -> dict:
    row = await _pool_or_raise().fetchrow(
        """
        SELECT
            COUNT(*)::INTEGER AS total_sessions,
            COALESCE(SUM(duration_minutes), 0)::INTEGER AS total_minutes
        FROM piano_sessions
        WHERE owner_user_id = $1
        """,
        owner_id,
    )
    return dict(row) if row else {"total_sessions": 0, "total_minutes": 0}


async def get_piano_owners() -> list[int]:
    rows = await _pool_or_raise().fetch(
        """
        SELECT DISTINCT owner_user_id FROM piano_sessions
        UNION
        SELECT DISTINCT owner_user_id FROM piano_pieces
        """
    )
    return [int(r["owner_user_id"]) for r in rows]


# ---------------------------------------------------------------------------
# Piano: pieces
# ---------------------------------------------------------------------------


async def add_piano_piece(owner_id: int, title: str, composer: str | None = None) -> int:
    return await _pool_or_raise().fetchval(
        """
        INSERT INTO piano_pieces (owner_user_id, title, composer)
        VALUES ($1, $2, $3) RETURNING id
        """,
        owner_id, title, composer,
    )


async def remove_piano_piece(owner_id: int, piece_id: int) -> bool:
    row = await _pool_or_raise().fetchrow(
        "DELETE FROM piano_pieces WHERE id = $1 AND owner_user_id = $2 RETURNING id",
        piece_id, owner_id,
    )
    return row is not None


async def list_piano_pieces(owner_id: int, status: str | None = None) -> list[dict]:
    pool = _pool_or_raise()
    if status is None:
        rows = await pool.fetch(
            "SELECT * FROM piano_pieces WHERE owner_user_id = $1 ORDER BY status, title",
            owner_id,
        )
    else:
        rows = await pool.fetch(
            "SELECT * FROM piano_pieces WHERE owner_user_id = $1 AND status = $2 ORDER BY title",
            owner_id, status,
        )
    return [dict(r) for r in rows]


async def find_piano_piece_by_title(owner_id: int, title: str) -> dict | None:
    pool = _pool_or_raise()
    row = await pool.fetchrow(
        """
        SELECT * FROM piano_pieces
        WHERE owner_user_id = $1 AND LOWER(title) = LOWER($2)
        LIMIT 1
        """,
        owner_id, title,
    )
    if row:
        return dict(row)
    row = await pool.fetchrow(
        """
        SELECT * FROM piano_pieces
        WHERE owner_user_id = $1 AND LOWER(title) LIKE LOWER($2)
        ORDER BY LENGTH(title)
        LIMIT 1
        """,
        owner_id, f"%{title}%",
    )
    return dict(row) if row else None


async def update_piano_piece_status(owner_id: int, piece_id: int, status: str) -> bool:
    row = await _pool_or_raise().fetchrow(
        "UPDATE piano_pieces SET status = $1 WHERE id = $2 AND owner_user_id = $3 RETURNING id",
        status, piece_id, owner_id,
    )
    return row is not None


async def update_piano_piece_note(owner_id: int, piece_id: int, notes: str) -> bool:
    row = await _pool_or_raise().fetchrow(
        "UPDATE piano_pieces SET notes = $1 WHERE id = $2 AND owner_user_id = $3 RETURNING id",
        notes, piece_id, owner_id,
    )
    return row is not None


async def touch_piano_piece_last_practiced(
    owner_id: int, piece_id: int, practiced_at: date,
) -> None:
    await _pool_or_raise().execute(
        "UPDATE piano_pieces SET last_practiced_at = $1 WHERE id = $2 AND owner_user_id = $3",
        practiced_at, piece_id, owner_id,
    )


async def most_practiced_piece(owner_id: int) -> dict | None:
    pool = _pool_or_raise()
    row = await pool.fetchrow(
        """
        SELECT title, COUNT(*) AS cnt
        FROM piano_sessions,
             jsonb_array_elements_text(pieces_practiced) AS title
        WHERE owner_user_id = $1
          AND title <> ''
        GROUP BY title
        ORDER BY cnt DESC
        LIMIT 1
        """,
        owner_id,
    )
    if row is None:
        return None

    top_title = row["title"]
    top_count = int(row["cnt"])
    piece = await find_piano_piece_by_title(owner_id, top_title)
    return {
        "title": piece["title"] if piece else top_title,
        "composer": piece["composer"] if piece else None,
        "count": top_count,
    }


# ---------------------------------------------------------------------------
# Piano: streak
# ---------------------------------------------------------------------------


async def get_piano_streak(owner_id: int) -> dict:
    row = await _pool_or_raise().fetchrow(
        "SELECT * FROM piano_streak WHERE owner_user_id = $1",
        owner_id,
    )
    if row:
        return dict(row)
    return {
        "owner_user_id": owner_id,
        "current_streak": 0,
        "longest_streak": 0,
        "last_practiced_date": None,
        "freeze_credits": 0,
        "freeze_until": None,
    }


async def upsert_piano_streak(
    owner_id: int,
    current_streak: int,
    longest_streak: int,
    last_practiced_date: date | None,
    freeze_credits: int = 0,
    freeze_until: date | None = None,
) -> None:
    await _pool_or_raise().execute(
        """
        INSERT INTO piano_streak
            (owner_user_id, current_streak, longest_streak, last_practiced_date,
             freeze_credits, freeze_until)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT(owner_user_id) DO UPDATE SET
            current_streak      = EXCLUDED.current_streak,
            longest_streak      = EXCLUDED.longest_streak,
            last_practiced_date = EXCLUDED.last_practiced_date,
            freeze_credits      = EXCLUDED.freeze_credits,
            freeze_until        = EXCLUDED.freeze_until
        """,
        owner_id, current_streak, longest_streak, last_practiced_date,
        freeze_credits, freeze_until,
    )


async def get_streak_minutes(owner_id: int, streak_start: date) -> int:
    val = await _pool_or_raise().fetchval(
        """
        SELECT COALESCE(SUM(duration_minutes), 0)::INTEGER
        FROM piano_sessions
        WHERE owner_user_id = $1 AND practiced_at >= $2
        """,
        owner_id, streak_start,
    )
    return int(val or 0)


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
    return await _pool_or_raise().fetchval(
        """
        INSERT INTO piano_recordings
            (owner_user_id, piece_id, file_path, duration_seconds,
             feedback_summary, raw_analysis)
        VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
        """,
        owner_id, piece_id, file_path, duration_seconds, feedback_summary, raw_analysis,
    )


async def list_piano_recordings(
    owner_id: int, piece_id: int | None = None, limit: int = 10,
) -> list[dict]:
    pool = _pool_or_raise()
    if piece_id is None:
        rows = await pool.fetch(
            "SELECT * FROM piano_recordings WHERE owner_user_id = $1 ORDER BY recorded_at DESC LIMIT $2",
            owner_id, limit,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT * FROM piano_recordings
            WHERE owner_user_id = $1 AND piece_id = $2
            ORDER BY recorded_at DESC LIMIT $3
            """,
            owner_id, piece_id, limit,
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------


async def add_reminder(
    owner_id: int,
    message: str,
    reminder_time: str,
    days_of_week: str = "*",
    repeat: bool = True,
    remind_at: datetime | None = None,
) -> int:
    return await _pool_or_raise().fetchval(
        """
        INSERT INTO reminders
            (owner_user_id, message, reminder_time, days_of_week, repeat, remind_at)
        VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
        """,
        owner_id, message, reminder_time, days_of_week, repeat, remind_at,
    )


async def list_reminders(owner_id: int) -> list[dict]:
    rows = await _pool_or_raise().fetch(
        """
        SELECT * FROM reminders
        WHERE owner_user_id = $1 AND active = TRUE
        ORDER BY COALESCE(remind_at::time, reminder_time::time), id
        """,
        owner_id,
    )
    return [dict(r) for r in rows]


async def get_reminder_by_id(owner_id: int, reminder_id: int) -> dict | None:
    row = await _pool_or_raise().fetchrow(
        "SELECT * FROM reminders WHERE id = $1 AND owner_user_id = $2 AND active = TRUE",
        reminder_id, owner_id,
    )
    return dict(row) if row else None


async def remove_reminder(owner_id: int, reminder_id: int) -> bool:
    row = await _pool_or_raise().fetchrow(
        "UPDATE reminders SET active = FALSE WHERE id = $1 AND owner_user_id = $2 AND active = TRUE RETURNING id",
        reminder_id, owner_id,
    )
    return row is not None


async def deactivate_reminder(reminder_id: int) -> None:
    """Soft-delete a reminder after it fires (used for one-time reminders)."""
    await _pool_or_raise().execute(
        "UPDATE reminders SET active = FALSE WHERE id = $1",
        reminder_id,
    )


async def get_all_active_reminders() -> list[dict]:
    rows = await _pool_or_raise().fetch(
        "SELECT * FROM reminders WHERE active = TRUE ORDER BY owner_user_id, reminder_time"
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


def _load_jsonb(raw) -> list | dict:
    if raw is None:
        return []
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return []


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def create_pending_invoice(
    owner_id: int, tmp_file_path: str, parsed: dict
) -> int:
    return await _pool_or_raise().fetchval(
        """
        INSERT INTO pending_invoices (owner_user_id, tmp_file_path, parsed)
        VALUES ($1, $2, $3::jsonb) RETURNING id
        """,
        owner_id, tmp_file_path, json.dumps(parsed),
    )


async def get_pending_invoice(pending_id: int, owner_id: int) -> dict | None:
    row = await _pool_or_raise().fetchrow(
        "SELECT * FROM pending_invoices WHERE id = $1 AND owner_user_id = $2",
        pending_id, owner_id,
    )
    if not row:
        return None
    d = dict(row)
    d["parsed"] = _load_jsonb(d.get("parsed"))
    return d


async def delete_pending_invoice(pending_id: int) -> None:
    await _pool_or_raise().execute(
        "DELETE FROM pending_invoices WHERE id = $1", pending_id
    )


async def cleanup_stale_pending_invoices(max_age_hours: int = 24) -> list[str]:
    """Delete pending_invoices older than max_age_hours. Returns tmp file paths to remove."""
    rows = await _pool_or_raise().fetch(
        """
        DELETE FROM pending_invoices
        WHERE created_at < NOW() - ($1 || ' hours')::INTERVAL
        RETURNING tmp_file_path
        """,
        str(max_age_hours),
    )
    return [r["tmp_file_path"] for r in rows]


async def log_invoice(
    owner_id: int,
    parsed: dict,
    file_path: str,
    source: str = "manual",
    gmail_message_id: str | None = None,
    original_filename: str | None = None,
) -> int:
    billing_period = int(parsed.get("billing_period_months") or 1)
    if billing_period not in (1, 3, 12):
        billing_period = 1
    return await _pool_or_raise().fetchval(
        """
        INSERT INTO invoices (
            owner_user_id, vendor, invoice_number, issue_date, due_date,
            currency, subtotal, tax, total, category, subcategory, recurring,
            billing_period_months, line_items, notes, source, gmail_message_id,
            file_path, original_filename
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb,$15,$16,$17,$18,$19)
        RETURNING id
        """,
        owner_id,
        parsed.get("vendor"),
        parsed.get("invoice_number"),
        _parse_date(parsed.get("issue_date")),
        _parse_date(parsed.get("due_date")),
        parsed.get("currency"),
        _to_float(parsed.get("subtotal")),
        _to_float(parsed.get("tax")),
        _to_float(parsed.get("total")),
        parsed.get("category"),
        parsed.get("subcategory"),
        bool(parsed.get("recurring", False)),
        billing_period,
        json.dumps(parsed.get("line_items") or []),
        parsed.get("notes"),
        source,
        gmail_message_id,
        file_path,
        original_filename,
    )


async def find_duplicate_invoice(
    owner_id: int,
    invoice_number: str | None,
    original_filename: str | None,
) -> dict | None:
    """Return an existing invoice matching invoice_number or original_filename, or None."""
    if not invoice_number and not original_filename:
        return None

    conditions: list[str] = []
    params: list = [owner_id]

    if invoice_number:
        params.append(invoice_number)
        conditions.append(f"invoice_number = ${len(params)}")
    if original_filename:
        params.append(original_filename)
        conditions.append(f"original_filename = ${len(params)}")

    sql = (
        f"SELECT * FROM invoices WHERE owner_user_id = $1 "
        f"AND ({' OR '.join(conditions)}) LIMIT 1"
    )
    row = await _pool_or_raise().fetchrow(sql, *params)
    if not row:
        return None
    d = dict(row)
    d["line_items"] = _load_jsonb(d.get("line_items"))
    return d


async def get_processed_filenames(owner_id: int) -> set[str]:
    rows = await _pool_or_raise().fetch(
        "SELECT original_filename FROM invoices WHERE owner_user_id = $1 AND original_filename IS NOT NULL",
        owner_id,
    )
    return {row["original_filename"] for row in rows}


async def list_invoices(owner_id: int, limit: int = 10) -> list[dict]:
    rows = await _pool_or_raise().fetch(
        """
        SELECT * FROM invoices
        WHERE owner_user_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        owner_id, limit,
    )
    result = []
    for r in rows:
        d = dict(r)
        d["line_items"] = _load_jsonb(d.get("line_items"))
        result.append(d)
    return result


async def get_invoices_for_month(owner_id: int, year: int, month: int) -> list[dict]:
    rows = await _pool_or_raise().fetch(
        """
        SELECT * FROM invoices
        WHERE owner_user_id = $1
          AND issue_date IS NOT NULL
          AND EXTRACT(YEAR FROM issue_date) = $2
          AND EXTRACT(MONTH FROM issue_date) = $3
        ORDER BY issue_date
        """,
        owner_id, year, month,
    )
    result = []
    for r in rows:
        d = dict(r)
        d["line_items"] = _load_jsonb(d.get("line_items"))
        result.append(d)
    return result


async def get_invoices_for_range(owner_id: int, start: date, end: date) -> list[dict]:
    """Return all invoices where issue_date >= start and issue_date < end."""
    rows = await _pool_or_raise().fetch(
        """
        SELECT * FROM invoices
        WHERE owner_user_id = $1
          AND issue_date IS NOT NULL
          AND issue_date >= $2
          AND issue_date < $3
        ORDER BY issue_date
        """,
        owner_id, start, end,
    )
    result = []
    for r in rows:
        d = dict(r)
        d["line_items"] = _load_jsonb(d.get("line_items"))
        result.append(d)
    return result


async def delete_invoice(owner_id: int, invoice_id: int) -> bool:
    row = await _pool_or_raise().fetchrow(
        "DELETE FROM invoices WHERE id = $1 AND owner_user_id = $2 RETURNING id",
        invoice_id, owner_id,
    )
    return row is not None


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


async def create_subscription(
    owner_id: int,
    name: str,
    vendor: str | None,
    category: str,
    subcategory: str | None,
    amount: float,
    currency: str,
    billing_period_months: int,
    notes: str | None,
) -> int:
    return await _pool_or_raise().fetchval(
        """
        INSERT INTO subscriptions
            (owner_user_id, name, vendor, category, subcategory, amount, currency,
             billing_period_months, notes)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id
        """,
        owner_id, name, vendor, category, subcategory, amount, currency,
        billing_period_months, notes,
    )


async def get_subscription(sub_id: int, owner_id: int) -> dict | None:
    row = await _pool_or_raise().fetchrow(
        "SELECT * FROM subscriptions WHERE id = $1 AND owner_user_id = $2",
        sub_id, owner_id,
    )
    return dict(row) if row else None


async def list_subscriptions(owner_id: int, active_only: bool = True) -> list[dict]:
    if active_only:
        rows = await _pool_or_raise().fetch(
            "SELECT * FROM subscriptions WHERE owner_user_id = $1 AND active = TRUE ORDER BY name",
            owner_id,
        )
    else:
        rows = await _pool_or_raise().fetch(
            "SELECT * FROM subscriptions WHERE owner_user_id = $1 ORDER BY active DESC, name",
            owner_id,
        )
    return [dict(r) for r in rows]


async def update_subscription_price(owner_id: int, sub_id: int, new_amount: float) -> int | None:
    """Deactivate old subscription and create a new one with updated price starting today.

    Returns the new subscription id, or None if sub_id was not found.
    """
    today = date.today()
    yesterday = date(today.year, today.month, today.day - 1) if today.day > 1 else date(
        today.year if today.month > 1 else today.year - 1,
        today.month - 1 if today.month > 1 else 12,
        28,  # safe last day of any month
    )
    async with _pool_or_raise().acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchrow(
                "SELECT * FROM subscriptions WHERE id = $1 AND owner_user_id = $2",
                sub_id, owner_id,
            )
            if not old:
                return None
            await conn.execute(
                "UPDATE subscriptions SET active = FALSE, end_date = $3 WHERE id = $1 AND owner_user_id = $2",
                sub_id, owner_id, yesterday,
            )
            new_id = await conn.fetchval(
                """
                INSERT INTO subscriptions
                    (owner_user_id, name, vendor, category, subcategory, amount, currency,
                     billing_period_months, active, start_date, notes)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, TRUE, $9, $10)
                RETURNING id
                """,
                owner_id, old["name"], old["vendor"], old["category"], old["subcategory"],
                new_amount, old["currency"], old["billing_period_months"], today, old["notes"],
            )
            return new_id


async def set_subscription_active(owner_id: int, sub_id: int, active: bool) -> bool:
    today = date.today()
    if active:
        row = await _pool_or_raise().fetchrow(
            "UPDATE subscriptions SET active = TRUE, end_date = NULL WHERE id = $1 AND owner_user_id = $2 RETURNING id",
            sub_id, owner_id,
        )
    else:
        row = await _pool_or_raise().fetchrow(
            "UPDATE subscriptions SET active = FALSE, end_date = $3 WHERE id = $1 AND owner_user_id = $2 RETURNING id",
            sub_id, owner_id, today,
        )
    return row is not None


async def delete_subscription(owner_id: int, sub_id: int) -> bool:
    row = await _pool_or_raise().fetchrow(
        "DELETE FROM subscriptions WHERE id = $1 AND owner_user_id = $2 RETURNING id",
        sub_id, owner_id,
    )
    return row is not None


async def get_subscriptions_active_in_month(owner_id: int, year: int, month: int) -> list[dict]:
    from calendar import monthrange
    last_day = monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, last_day)
    rows = await _pool_or_raise().fetch(
        """
        SELECT * FROM subscriptions
        WHERE owner_user_id = $1
          AND start_date <= $2
          AND (end_date IS NULL OR end_date >= $3)
        ORDER BY name
        """,
        owner_id, month_end, month_start,
    )
    return [dict(r) for r in rows]


async def get_subscriptions_active_in_range(owner_id: int, start: date, end: date) -> list[dict]:
    """Return all subscriptions that overlap with [start, end) range."""
    rows = await _pool_or_raise().fetch(
        """
        SELECT * FROM subscriptions
        WHERE owner_user_id = $1
          AND start_date < $3
          AND (end_date IS NULL OR end_date >= $2)
        ORDER BY start_date, name
        """,
        owner_id, start, end,
    )
    return [dict(r) for r in rows]
