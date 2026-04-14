from __future__ import annotations

import logging
from datetime import date, datetime

from bot.services import db

logger = logging.getLogger(__name__)


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


async def compute_and_update_streak(owner_id: int, practiced_at: date) -> dict:
    """Update the streak for *owner_id* given a practice on *practiced_at*.

    Rules:
      - same-day re-log: no change (idempotent)
      - consecutive day (gap == 1): current += 1
      - gap > 1 or no prior history: reset current to 1
    Always bumps longest to max(longest, current) and updates last_practiced_date.
    """
    current = await db.get_piano_streak(owner_id)
    last = _as_date(current.get("last_practiced_date"))
    current_streak = int(current.get("current_streak") or 0)
    longest_streak = int(current.get("longest_streak") or 0)

    if last == practiced_at:
        return current

    if last is None:
        new_current = 1
    else:
        delta_days = (practiced_at - last).days
        if delta_days == 1:
            new_current = current_streak + 1
        elif delta_days <= 0:
            # Backdated log — don't corrupt state, just keep current and bump longest.
            new_current = current_streak or 1
        else:
            new_current = 1

    new_longest = max(longest_streak, new_current)

    await db.upsert_piano_streak(
        owner_id=owner_id,
        current_streak=new_current,
        longest_streak=new_longest,
        last_practiced_date=practiced_at,
    )

    return {
        "owner_user_id": owner_id,
        "current_streak": new_current,
        "longest_streak": new_longest,
        "last_practiced_date": practiced_at.isoformat(),
    }
