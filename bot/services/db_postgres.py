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
    daily_calories  INTEGER DEFAULT 2000
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
"""


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def init_pg() -> None:
    """Initialise the PostgreSQL connection pool.

    Reads DATABASE_URL from the environment.  If the variable is unset or the
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
            # Idempotent migrations for already-created databases
            await conn.execute(
                "ALTER TABLE meals ADD COLUMN IF NOT EXISTS photo_path TEXT"
            )
            await conn.execute(
                "ALTER TABLE supplements ADD COLUMN IF NOT EXISTS dose TEXT"
            )
        logger.info("PostgreSQL mirror initialised")
    except (ConnectionRefusedError, asyncpg.PostgresError, OSError, Exception) as exc:
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
# Mirror functions
# ---------------------------------------------------------------------------


async def mirror_create_profile(profile_id: int, owner_id: int, name: str) -> None:
    if _pool is None:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO profiles (id, owner_user_id, name, active) "
                "VALUES ($1, $2, $3, TRUE) ON CONFLICT DO NOTHING",
                profile_id,
                owner_id,
                name,
            )
    except Exception:
        logger.error("pg mirror_create_profile failed", exc_info=True)


async def mirror_set_active_profile(owner_id: int, profile_id: int) -> None:
    if _pool is None:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO active_profile (user_id, profile_id) "
                "VALUES ($1, $2) "
                "ON CONFLICT (user_id) DO UPDATE SET profile_id = $2",
                owner_id,
                profile_id,
            )
    except Exception:
        logger.error("pg mirror_set_active_profile failed", exc_info=True)


async def mirror_delete_profile(owner_id: int, name: str) -> None:
    if _pool is None:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                "UPDATE profiles SET active = FALSE "
                "WHERE owner_user_id = $1 AND name = $2",
                owner_id,
                name,
            )
    except Exception:
        logger.error("pg mirror_delete_profile failed", exc_info=True)


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
    if _pool is None:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO meals "
                "(id, profile_id, owner_user_id, eaten_at, description, "
                "calories, protein_g, carbs_g, fat_g, raw_llm_response, photo_path) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) "
                "ON CONFLICT DO NOTHING",
                meal_id,
                profile_id,
                owner_id,
                eaten_at,
                description,
                calories,
                protein_g,
                carbs_g,
                fat_g,
                raw_llm,
                photo_path,
            )
    except Exception:
        logger.error("pg mirror_log_meal failed", exc_info=True)


async def mirror_set_goal(profile_id: int, daily_calories: int) -> None:
    if _pool is None:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO goals (profile_id, daily_calories) "
                "VALUES ($1, $2) "
                "ON CONFLICT (profile_id) DO UPDATE SET daily_calories = $2",
                profile_id,
                daily_calories,
            )
    except Exception:
        logger.error("pg mirror_set_goal failed", exc_info=True)


async def mirror_add_supplement(
    supplement_id: int,
    profile_id: int,
    owner_id: int,
    name: str,
    reminder_time: str,
    dose: str | None = None,
) -> None:
    if _pool is None:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO supplements "
                "(id, profile_id, owner_user_id, name, reminder_time, dose, active) "
                "VALUES ($1, $2, $3, $4, $5, $6, TRUE) "
                "ON CONFLICT DO NOTHING",
                supplement_id,
                profile_id,
                owner_id,
                name,
                reminder_time,
                dose,
            )
    except Exception:
        logger.error("pg mirror_add_supplement failed", exc_info=True)


async def mirror_remove_supplement(
    profile_id: int, owner_id: int, name: str
) -> None:
    if _pool is None:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                "UPDATE supplements SET active = FALSE "
                "WHERE profile_id = $1 AND owner_user_id = $2 AND name = $3",
                profile_id,
                owner_id,
                name,
            )
    except Exception:
        logger.error("pg mirror_remove_supplement failed", exc_info=True)


async def mirror_log_supplement_taken(
    log_id: int, supplement_id: int, profile_id: int
) -> None:
    if _pool is None:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO supplement_logs (id, supplement_id, profile_id) "
                "VALUES ($1, $2, $3) "
                "ON CONFLICT DO NOTHING",
                log_id,
                supplement_id,
                profile_id,
            )
    except Exception:
        logger.error("pg mirror_log_supplement_taken failed", exc_info=True)
