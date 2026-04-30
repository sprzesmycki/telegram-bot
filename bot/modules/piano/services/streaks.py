from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from bot.services import db

logger = logging.getLogger(__name__)

_MAX_FREEZE_DAYS = 14
_MAX_CREDITS = 2


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def calculate_streak(
    current_streak: int,
    longest_streak: int,
    freeze_credits: int,
    freeze_until: date | None,
    last: date | None,
    practiced_at: date,
) -> dict:
    """Pure streak calculation — no DB access.

    Protection hierarchy (highest priority first):
      1. Active travel freeze (freeze_until covers the gap)
      2. Free day: gap of exactly 1 missed day is always forgiven
      3. Credits: each covers one additional missed day (gap - 1 credits needed)
      4. Reset: not enough protection → current resets to 1, credits preserved

    Returns a dict with new_current, new_longest, freeze_credits, freeze_until.
    """
    new_freeze_until: date | None = freeze_until
    if freeze_until and practiced_at >= freeze_until:
        new_freeze_until = None

    if last is None:
        new_current = 1
    else:
        delta_days = (practiced_at - last).days

        if delta_days <= 0:
            new_current = current_streak or 1
        elif delta_days == 1:
            new_current = current_streak + 1
        else:
            gap_days = delta_days - 1

            if freeze_until and freeze_until >= last + timedelta(days=1):
                new_current = current_streak + 1
                if practiced_at >= freeze_until:
                    new_freeze_until = None
            elif gap_days == 1:
                new_current = current_streak + 1
            elif freeze_credits >= gap_days - 1:
                freeze_credits -= gap_days - 1
                new_current = current_streak + 1
            else:
                new_current = 1

    new_longest = max(longest_streak, new_current)

    if new_current % 7 == 0 and freeze_credits < _MAX_CREDITS:
        freeze_credits += 1

    return {
        "new_current": new_current,
        "new_longest": new_longest,
        "freeze_credits": freeze_credits,
        "freeze_until": new_freeze_until,
    }


async def compute_and_update_streak(owner_id: int, practiced_at: date) -> dict:
    """Update the streak for *owner_id* given a practice on *practiced_at*."""
    current = await db.get_piano_streak(owner_id)
    last = _as_date(current.get("last_practiced_date"))
    current_streak = int(current.get("current_streak") or 0)
    longest_streak = int(current.get("longest_streak") or 0)
    freeze_credits = int(current.get("freeze_credits") or 0)
    freeze_until = _as_date(current.get("freeze_until"))

    # Same-day re-log: idempotent (clear expired freeze if practice happened)
    if last == practiced_at:
        if freeze_until and practiced_at >= freeze_until:
            await db.upsert_piano_streak(
                owner_id=owner_id,
                current_streak=current_streak,
                longest_streak=longest_streak,
                last_practiced_date=last,
                freeze_credits=freeze_credits,
                freeze_until=None,
            )
            return {**current, "freeze_until": None}
        return current

    result = calculate_streak(
        current_streak, longest_streak, freeze_credits, freeze_until, last, practiced_at,
    )

    await db.upsert_piano_streak(
        owner_id=owner_id,
        current_streak=result["new_current"],
        longest_streak=result["new_longest"],
        last_practiced_date=practiced_at,
        freeze_credits=result["freeze_credits"],
        freeze_until=result["freeze_until"],
    )

    return {
        "owner_user_id": owner_id,
        "current_streak": result["new_current"],
        "longest_streak": result["new_longest"],
        "last_practiced_date": practiced_at.isoformat(),
        "freeze_credits": result["freeze_credits"],
        "freeze_until": result["freeze_until"].isoformat() if result["freeze_until"] else None,
    }


async def activate_freeze(owner_id: int, days: int) -> dict:
    """Set a travel freeze for *days* days (max 14). Replaces any existing freeze."""
    days = max(1, min(days, _MAX_FREEZE_DAYS))
    freeze_until = date.today() + timedelta(days=days)
    current = await db.get_piano_streak(owner_id)
    await db.upsert_piano_streak(
        owner_id=owner_id,
        current_streak=int(current.get("current_streak") or 0),
        longest_streak=int(current.get("longest_streak") or 0),
        last_practiced_date=_as_date(current.get("last_practiced_date")),
        freeze_credits=int(current.get("freeze_credits") or 0),
        freeze_until=freeze_until,
    )
    return {**current, "freeze_until": freeze_until.isoformat()}
