"""Dual-write orchestration for meal / liquid entries.

Each function here writes SQLite (source of truth) then mirrors to Postgres
(best-effort). Callers get a single async function to await, which keeps the
dual-DB pattern a one-liner at the handler level.
"""
from __future__ import annotations

from datetime import datetime

from bot.services import db_postgres, db_sqlite


async def record_meal(
    *,
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
    """Persist a meal row and mirror it. Returns the new SQLite id."""
    meal_id = await db_sqlite.log_meal(
        profile_id=profile_id,
        owner_id=owner_id,
        eaten_at=eaten_at,
        description=description,
        calories=calories,
        protein_g=protein_g,
        carbs_g=carbs_g,
        fat_g=fat_g,
        raw_llm=raw_llm,
        photo_path=photo_path,
    )
    await db_postgres.mirror_log_meal(
        meal_id=meal_id,
        profile_id=profile_id,
        owner_id=owner_id,
        eaten_at=eaten_at,
        description=description,
        calories=calories,
        protein_g=protein_g,
        carbs_g=carbs_g,
        fat_g=fat_g,
        raw_llm=raw_llm,
        photo_path=photo_path,
    )
    return meal_id


async def record_liquid(
    *,
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
    """Persist a liquid row and mirror it. Returns the new SQLite id."""
    liquid_id = await db_sqlite.log_liquid(
        profile_id=profile_id,
        owner_id=owner_id,
        drunk_at=drunk_at,
        description=description,
        amount_ml=amount_ml,
        calories=calories,
        protein_g=protein_g,
        carbs_g=carbs_g,
        fat_g=fat_g,
        raw_llm=raw_llm,
    )
    await db_postgres.mirror_log_liquid(
        liquid_id=liquid_id,
        profile_id=profile_id,
        owner_id=owner_id,
        drunk_at=drunk_at,
        description=description,
        amount_ml=amount_ml,
        calories=calories,
        protein_g=protein_g,
        carbs_g=carbs_g,
        fat_g=fat_g,
        raw_llm=raw_llm,
    )
    return liquid_id


async def delete_meal(meal_id: int, owner_id: int) -> bool:
    """Delete a meal row and mirror the delete. Returns True if the row existed."""
    removed = await db_sqlite.delete_meal(meal_id, owner_id)
    if removed:
        await db_postgres.mirror_delete_meal(meal_id, owner_id)
    return removed


async def delete_liquid(liquid_id: int, owner_id: int) -> bool:
    """Delete a liquid row and mirror the delete. Returns True if the row existed."""
    removed = await db_sqlite.delete_liquid(liquid_id, owner_id)
    if removed:
        await db_postgres.mirror_delete_liquid(liquid_id, owner_id)
    return removed
