from __future__ import annotations

import logging
import os

import asyncpg

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------

_pool: asyncpg.Pool | None = None


def _get_pg_schema() -> str:
    """Return PostgreSQL CREATE TABLE statements for the calorie-bot schema."""
    return """\
CREATE TABLE IF NOT EXISTS profiles (
    id          SERIAL PRIMARY KEY,
    owner_user_id BIGINT NOT NULL,
    name        TEXT NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    height_cm   REAL,
    weight_kg   REAL,
    age         INTEGER,
    gender      TEXT,
    activity_level TEXT,
    UNIQUE (owner_user_id, name)
);

CREATE TABLE IF NOT EXISTS active_profile (
    user_id     BIGINT PRIMARY KEY,
    profile_id  INTEGER REFERENCES profiles(id)
);

CREATE TABLE IF NOT EXISTS meals (
    id              SERIAL PRIMARY KEY,
    profile_id      INTEGER,
    owner_user_id   BIGINT,
    eaten_at        TIMESTAMPTZ,
    logged_at       TIMESTAMPTZ DEFAULT NOW(),
    description     TEXT,
    calories        INTEGER,
    protein_g       REAL,
    carbs_g         REAL,
    fat_g           REAL,
    raw_llm_response TEXT,
    photo_path      TEXT
);

CREATE TABLE IF NOT EXISTS goals (
    profile_id      INTEGER PRIMARY KEY REFERENCES profiles(id),
    daily_calories  INTEGER DEFAULT 2000,
    daily_protein_g REAL,
    daily_carbs_g   REAL,
    daily_fat_g     REAL
);

CREATE TABLE IF NOT EXISTS supplements (
    id              SERIAL PRIMARY KEY,
    profile_id      INTEGER,
    owner_user_id   BIGINT,
    name            TEXT,
    reminder_time   TEXT,
    dose            TEXT,
    active          BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS supplement_logs (
    id              SERIAL PRIMARY KEY,
    supplement_id   INTEGER,
    profile_id      INTEGER,
    taken_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS piano_sessions (
    id                SERIAL PRIMARY KEY,
    owner_user_id     BIGINT NOT NULL,
    practiced_at      DATE NOT NULL,
    duration_minutes  INTEGER,
    notes             TEXT,
    pieces_practiced  TEXT,
    logged_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS piano_pieces (
    id                 SERIAL PRIMARY KEY,
    owner_user_id      BIGINT NOT NULL,
    title              TEXT NOT NULL,
    composer           TEXT,
    status             TEXT NOT NULL DEFAULT 'learning',
    added_at           DATE DEFAULT CURRENT_DATE,
    last_practiced_at  DATE,
    notes              TEXT
);

CREATE TABLE IF NOT EXISTS piano_recordings (
    id                SERIAL PRIMARY KEY,
    owner_user_id     BIGINT NOT NULL,
    piece_id          INTEGER REFERENCES piano_pieces(id),
    recorded_at       TIMESTAMPTZ DEFAULT NOW(),
    file_path         TEXT,
    duration_seconds  INTEGER,
    feedback_summary  TEXT,
    raw_analysis      TEXT
);

CREATE TABLE IF NOT EXISTS piano_streak (
    owner_user_id        BIGINT PRIMARY KEY,
    current_streak       INTEGER NOT NULL DEFAULT 0,
    longest_streak       INTEGER NOT NULL DEFAULT 0,
    last_practiced_date  DATE
);

CREATE TABLE IF NOT EXISTS liquids (
    id              SERIAL PRIMARY KEY,
    profile_id      INTEGER,
    owner_user_id   BIGINT,
    drunk_at        TIMESTAMPTZ,
    logged_at       TIMESTAMPTZ DEFAULT NOW(),
    description     TEXT,
    amount_ml       INTEGER,
    calories        INTEGER,
    protein_g       REAL,
    carbs_g         REAL,
    fat_g           REAL,
    raw_llm_response TEXT
);
"""


_ADDITIVE_MIGRATIONS = (
    "ALTER TABLE meals      ADD COLUMN IF NOT EXISTS photo_path      TEXT",
    "ALTER TABLE supplements ADD COLUMN IF NOT EXISTS dose            TEXT",
    "ALTER TABLE profiles   ADD COLUMN IF NOT EXISTS height_cm       REAL",
    "ALTER TABLE profiles   ADD COLUMN IF NOT EXISTS weight_kg       REAL",
    "ALTER TABLE profiles   ADD COLUMN IF NOT EXISTS age             INTEGER",
    "ALTER TABLE profiles   ADD COLUMN IF NOT EXISTS gender          TEXT",
    "ALTER TABLE profiles   ADD COLUMN IF NOT EXISTS activity_level  TEXT",
    "ALTER TABLE goals      ADD COLUMN IF NOT EXISTS daily_protein_g REAL",
    "ALTER TABLE goals      ADD COLUMN IF NOT EXISTS daily_carbs_g   REAL",
    "ALTER TABLE goals      ADD COLUMN IF NOT EXISTS daily_fat_g     REAL",
)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def init_pg() -> None:
    """Initialise the PostgreSQL connection pool.

    Reads DATABASE_URL from the environment. If the variable is unset or the
    connection fails, the pool stays ``None`` and every mirror function becomes
    a silent no-op -- the bot continues with SQLite only.
    """
    global _pool

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logger.warning("DATABASE_URL not set -- PostgreSQL mirror disabled")
        return

    try:
        _pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
        async with _pool.acquire() as conn:
            await conn.execute(_get_pg_schema())
            for stmt in _ADDITIVE_MIGRATIONS:
                await conn.execute(stmt)
        logger.info("PostgreSQL mirror initialised")
    except Exception as exc:
        logger.warning("PostgreSQL mirror unavailable: %s", exc)
        _pool = None


def is_available() -> bool:
    """Return True when the PostgreSQL pool is ready."""
    return _pool is not None


async def close_pg() -> None:
    """Gracefully close the connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# Mirror helper
# ---------------------------------------------------------------------------


async def _exec(label: str, sql: str, *args) -> None:
    """Run *sql* on the mirror pool, silently no-op'ing when it's unavailable.

    Mirrors are best-effort: we never raise back to the caller — SQLite is the
    source of truth. *label* is used solely for error logs.
    """
    if _pool is None:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(sql, *args)
    except Exception:
        logger.error("pg %s failed", label, exc_info=True)


# ---------------------------------------------------------------------------
# Mirror functions — profiles
# ---------------------------------------------------------------------------


async def mirror_create_profile(profile_id: int, owner_id: int, name: str) -> None:
    await _exec(
        "mirror_create_profile",
        "INSERT INTO profiles (id, owner_user_id, name, active) "
        "VALUES ($1, $2, $3, TRUE) ON CONFLICT DO NOTHING",
        profile_id, owner_id, name,
    )


async def mirror_set_active_profile(owner_id: int, profile_id: int) -> None:
    await _exec(
        "mirror_set_active_profile",
        "INSERT INTO active_profile (user_id, profile_id) VALUES ($1, $2) "
        "ON CONFLICT (user_id) DO UPDATE SET profile_id = $2",
        owner_id, profile_id,
    )


async def mirror_delete_profile(owner_id: int, name: str) -> None:
    await _exec(
        "mirror_delete_profile",
        "UPDATE profiles SET active = FALSE "
        "WHERE owner_user_id = $1 AND name = $2",
        owner_id, name,
    )


async def mirror_update_profile(
    profile_id: int,
    owner_id: int,
    height_cm: float | None = None,
    weight_kg: float | None = None,
    age: int | None = None,
    gender: str | None = None,
    activity_level: str | None = None,
) -> None:
    # Dynamic SET clause — only touch fields the caller actually provided.
    fields = [
        ("height_cm", height_cm),
        ("weight_kg", weight_kg),
        ("age", age),
        ("gender", gender),
        ("activity_level", activity_level),
    ]
    updates: list[str] = []
    params: list[object] = []
    for column, value in fields:
        if value is None:
            continue
        params.append(value)
        updates.append(f"{column} = ${len(params)}")

    if not updates:
        return

    params.extend([profile_id, owner_id])
    sql = (
        f"UPDATE profiles SET {', '.join(updates)} "
        f"WHERE id = ${len(params) - 1} AND owner_user_id = ${len(params)}"
    )
    await _exec("mirror_update_profile", sql, *params)


# ---------------------------------------------------------------------------
# Mirror functions — meals & liquids
# ---------------------------------------------------------------------------


async def mirror_log_meal(
    meal_id: int,
    profile_id: int,
    owner_id: int,
    eaten_at,
    description: str,
    calories: int,
    protein_g: float,
    carbs_g: float,
    fat_g: float,
    raw_llm: str,
    photo_path: str | None = None,
) -> None:
    await _exec(
        "mirror_log_meal",
        "INSERT INTO meals "
        "(id, profile_id, owner_user_id, eaten_at, description, "
        "calories, protein_g, carbs_g, fat_g, raw_llm_response, photo_path) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) "
        "ON CONFLICT DO NOTHING",
        meal_id, profile_id, owner_id, eaten_at, description,
        calories, protein_g, carbs_g, fat_g, raw_llm, photo_path,
    )


async def mirror_log_liquid(
    liquid_id: int,
    profile_id: int,
    owner_id: int,
    drunk_at,
    description: str,
    amount_ml: int,
    calories: int,
    protein_g: float,
    carbs_g: float,
    fat_g: float,
    raw_llm: str,
) -> None:
    await _exec(
        "mirror_log_liquid",
        "INSERT INTO liquids "
        "(id, profile_id, owner_user_id, drunk_at, description, "
        "amount_ml, calories, protein_g, carbs_g, fat_g, raw_llm_response) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) "
        "ON CONFLICT DO NOTHING",
        liquid_id, profile_id, owner_id, drunk_at, description,
        amount_ml, calories, protein_g, carbs_g, fat_g, raw_llm,
    )


async def mirror_delete_meal(meal_id: int, owner_id: int) -> None:
    await _exec(
        "mirror_delete_meal",
        "DELETE FROM meals WHERE id = $1 AND owner_user_id = $2",
        meal_id, owner_id,
    )


async def mirror_delete_liquid(liquid_id: int, owner_id: int) -> None:
    await _exec(
        "mirror_delete_liquid",
        "DELETE FROM liquids WHERE id = $1 AND owner_user_id = $2",
        liquid_id, owner_id,
    )


# ---------------------------------------------------------------------------
# Mirror functions — goals & supplements
# ---------------------------------------------------------------------------


async def mirror_set_goal(
    profile_id: int,
    daily_calories: int,
    protein_g: float | None = None,
    carbs_g: float | None = None,
    fat_g: float | None = None,
) -> None:
    await _exec(
        "mirror_set_goal",
        "INSERT INTO goals "
        "(profile_id, daily_calories, daily_protein_g, daily_carbs_g, daily_fat_g) "
        "VALUES ($1, $2, $3, $4, $5) "
        "ON CONFLICT (profile_id) DO UPDATE SET "
        "daily_calories = $2, daily_protein_g = $3, "
        "daily_carbs_g = $4, daily_fat_g = $5",
        profile_id, daily_calories, protein_g, carbs_g, fat_g,
    )


async def mirror_add_supplement(
    supplement_id: int,
    profile_id: int,
    owner_id: int,
    name: str,
    reminder_time: str,
    dose: str | None = None,
) -> None:
    await _exec(
        "mirror_add_supplement",
        "INSERT INTO supplements "
        "(id, profile_id, owner_user_id, name, reminder_time, dose, active) "
        "VALUES ($1, $2, $3, $4, $5, $6, TRUE) "
        "ON CONFLICT DO NOTHING",
        supplement_id, profile_id, owner_id, name, reminder_time, dose,
    )


async def mirror_remove_supplement(
    profile_id: int, owner_id: int, name: str
) -> None:
    await _exec(
        "mirror_remove_supplement",
        "UPDATE supplements SET active = FALSE "
        "WHERE profile_id = $1 AND owner_user_id = $2 AND name = $3",
        profile_id, owner_id, name,
    )


async def mirror_log_supplement_taken(
    log_id: int, supplement_id: int, profile_id: int
) -> None:
    await _exec(
        "mirror_log_supplement_taken",
        "INSERT INTO supplement_logs (id, supplement_id, profile_id) "
        "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
        log_id, supplement_id, profile_id,
    )


# ---------------------------------------------------------------------------
# Mirror functions — piano
# ---------------------------------------------------------------------------


async def mirror_log_piano_session(
    session_id: int,
    owner_id: int,
    practiced_at,
    duration_minutes: int | None,
    notes: str | None,
    pieces_practiced_json: str,
) -> None:
    await _exec(
        "mirror_log_piano_session",
        "INSERT INTO piano_sessions "
        "(id, owner_user_id, practiced_at, duration_minutes, notes, pieces_practiced) "
        "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING",
        session_id, owner_id, practiced_at,
        duration_minutes, notes, pieces_practiced_json,
    )


async def mirror_add_piano_piece(
    piece_id: int,
    owner_id: int,
    title: str,
    composer: str | None,
) -> None:
    await _exec(
        "mirror_add_piano_piece",
        "INSERT INTO piano_pieces "
        "(id, owner_user_id, title, composer, status) "
        "VALUES ($1, $2, $3, $4, 'learning') ON CONFLICT DO NOTHING",
        piece_id, owner_id, title, composer,
    )


async def mirror_remove_piano_piece(owner_id: int, piece_id: int) -> None:
    await _exec(
        "mirror_remove_piano_piece",
        "DELETE FROM piano_pieces WHERE id = $1 AND owner_user_id = $2",
        piece_id, owner_id,
    )


async def mirror_update_piano_piece_status(
    owner_id: int, piece_id: int, status: str
) -> None:
    await _exec(
        "mirror_update_piano_piece_status",
        "UPDATE piano_pieces SET status = $1 "
        "WHERE id = $2 AND owner_user_id = $3",
        status, piece_id, owner_id,
    )


async def mirror_update_piano_piece_note(
    owner_id: int, piece_id: int, notes: str
) -> None:
    await _exec(
        "mirror_update_piano_piece_note",
        "UPDATE piano_pieces SET notes = $1 "
        "WHERE id = $2 AND owner_user_id = $3",
        notes, piece_id, owner_id,
    )


async def mirror_touch_piano_piece_last_practiced(
    owner_id: int, piece_id: int, practiced_at
) -> None:
    await _exec(
        "mirror_touch_piano_piece_last_practiced",
        "UPDATE piano_pieces SET last_practiced_at = $1 "
        "WHERE id = $2 AND owner_user_id = $3",
        practiced_at, piece_id, owner_id,
    )


async def mirror_upsert_piano_streak(
    owner_id: int,
    current_streak: int,
    longest_streak: int,
    last_practiced_date,
) -> None:
    await _exec(
        "mirror_upsert_piano_streak",
        "INSERT INTO piano_streak "
        "(owner_user_id, current_streak, longest_streak, last_practiced_date) "
        "VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (owner_user_id) DO UPDATE SET "
        "current_streak = $2, longest_streak = $3, last_practiced_date = $4",
        owner_id, current_streak, longest_streak, last_practiced_date,
    )


async def mirror_add_piano_recording(
    recording_id: int,
    owner_id: int,
    piece_id: int | None,
    file_path: str | None,
    duration_seconds: int | None,
    feedback_summary: str | None,
    raw_analysis: str | None,
) -> None:
    await _exec(
        "mirror_add_piano_recording",
        "INSERT INTO piano_recordings "
        "(id, owner_user_id, piece_id, file_path, duration_seconds, "
        "feedback_summary, raw_analysis) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT DO NOTHING",
        recording_id, owner_id, piece_id, file_path,
        duration_seconds, feedback_summary, raw_analysis,
    )
