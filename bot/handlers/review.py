from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.handlers.profiles import get_target_profiles
from bot.services import db_sqlite
from bot.services.llm import review_day

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


async def _gather_day_data(
    profile: dict, owner_id: int, review_date: str,
) -> dict:
    """Fetch everything needed to review *review_date* for *profile*.

    For today, uses the fast "today" helpers. For any other date, aggregates
    from range queries (matches the /report pattern). Supplement compliance
    is only included for today because the per-date helper doesn't exist.
    """
    today_str = date.today().isoformat()
    is_today = review_date == today_str

    if is_today:
        meals = await db_sqlite.get_meals_today(profile["id"], owner_id)
        liquids = await db_sqlite.get_liquids_today(profile["id"], owner_id)
        totals = await db_sqlite.get_daily_totals(profile["id"], owner_id)
        hydration = await db_sqlite.get_daily_hydration(profile["id"], owner_id)
        supplements_scheduled = await db_sqlite.list_supplements(profile["id"], owner_id)
        supplement_logs = await db_sqlite.get_supplement_logs_today(profile["id"])
        taken_names: list[str] = []
        for log in supplement_logs:
            for s in supplements_scheduled:
                if s["id"] == log["supplement_id"]:
                    taken_names.append(s["name"])
                    break
    else:
        next_day = (
            datetime.strptime(review_date, "%Y-%m-%d").date() + timedelta(days=1)
        ).isoformat()
        meals = await db_sqlite.get_meals_range(
            profile["id"], owner_id, review_date, next_day
        )
        liquids = await db_sqlite.get_liquids_range(
            profile["id"], owner_id, review_date, next_day
        )
        totals = {"calories": 0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
        hydration = 0
        for m in meals:
            totals["calories"] += m.get("calories") or 0
            totals["protein_g"] += m.get("protein_g") or 0
            totals["carbs_g"] += m.get("carbs_g") or 0
            totals["fat_g"] += m.get("fat_g") or 0
        for l in liquids:
            totals["calories"] += l.get("calories") or 0
            totals["protein_g"] += l.get("protein_g") or 0
            totals["carbs_g"] += l.get("carbs_g") or 0
            totals["fat_g"] += l.get("fat_g") or 0
            hydration += l.get("amount_ml") or 0
        supplements_scheduled = []
        taken_names = []

    goal = await db_sqlite.get_goal(profile["id"])

    return {
        "meals": meals,
        "liquids": liquids,
        "totals": totals,
        "goal": goal,
        "hydration": hydration,
        "supplements_scheduled": supplements_scheduled,
        "supplements_taken_names": taken_names,
    }


# ---------------------------------------------------------------------------
# /review
# ---------------------------------------------------------------------------


async def review_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    text = update.message.text or ""

    profiles = await get_target_profiles(owner_id, text)
    if not profiles:
        await update.message.reply_text("Profile not found.")
        return
    profile = profiles[0]

    date_match = _DATE_RE.search(text)
    review_date = date_match.group(0) if date_match else date.today().isoformat()

    data = await _gather_day_data(profile, owner_id, review_date)
    if not data["meals"] and not data["liquids"]:
        await update.message.reply_text(
            f"Nothing logged for {profile['name']} on {review_date} \u2014 nothing to review."
        )
        return

    await update.message.reply_text(
        f"\U0001f9e0 Reviewing {profile['name']} \u2014 {review_date}\u2026"
    )

    try:
        review_text = await review_day(
            profile_name=profile["name"],
            review_date=review_date,
            meals=data["meals"],
            liquids=data["liquids"],
            totals=data["totals"],
            goal=data["goal"],
            hydration_ml=data["hydration"],
            supplements_scheduled=data["supplements_scheduled"],
            supplements_taken_names=data["supplements_taken_names"],
        )
    except Exception as exc:
        logger.error("review_day failed", exc_info=True)
        await update.message.reply_text(f"Review failed: {exc}")
        return

    header = f"Daily Review \u2014 {profile['name']} ({review_date})\n\n"
    await update.message.reply_text(header + review_text)


# ---------------------------------------------------------------------------
# Exported for scheduler
# ---------------------------------------------------------------------------


async def send_daily_review(bot, owner_id: int, profile: dict) -> None:
    """Send the scheduled daily review to *owner_id* for *profile* (today).

    Silent no-op when the day has nothing logged so we don't spam users who
    skipped the bot that day.
    """
    today_str = date.today().isoformat()
    data = await _gather_day_data(profile, owner_id, today_str)
    if not data["meals"] and not data["liquids"]:
        return

    try:
        review_text = await review_day(
            profile_name=profile["name"],
            review_date=today_str,
            meals=data["meals"],
            liquids=data["liquids"],
            totals=data["totals"],
            goal=data["goal"],
            hydration_ml=data["hydration"],
            supplements_scheduled=data["supplements_scheduled"],
            supplements_taken_names=data["supplements_taken_names"],
        )
    except Exception:
        logger.error(
            "Scheduled review_day failed for owner=%s profile=%s",
            owner_id, profile["name"], exc_info=True,
        )
        return

    header = f"Daily Review \u2014 {profile['name']} ({today_str})\n\n"
    await bot.send_message(chat_id=owner_id, text=header + review_text)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(app) -> None:
    app.add_handler(CommandHandler("review", review_cmd))
