from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.handlers.profiles import resolve_single_profile
from bot.services import db
from bot.utils.formatting import format_report, format_summary, format_week

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


# ---------------------------------------------------------------------------
# /summary
# ---------------------------------------------------------------------------


async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    text = update.message.text or ""

    profile = await resolve_single_profile(owner_id, text)
    if profile is None:
        await update.message.reply_text("Profile not found.")
        return

    meals = await db.get_meals_today(profile["id"], owner_id)
    liquids = await db.get_liquids_today(profile["id"], owner_id)
    totals = await db.get_daily_totals(profile["id"], owner_id)
    goal = await db.get_goal(profile["id"])
    hydration = await db.get_daily_hydration(profile["id"], owner_id)

    reply = format_summary(profile["name"], meals, liquids, totals, goal, hydration)
    await update.message.reply_text(reply)


# ---------------------------------------------------------------------------
# /week
# ---------------------------------------------------------------------------


async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    text = update.message.text or ""

    profile = await resolve_single_profile(owner_id, text)
    if profile is None:
        await update.message.reply_text("Profile not found.")
        return

    today = date.today()
    start = today - timedelta(days=6)
    start_str = start.isoformat()
    end_str = (today + timedelta(days=1)).isoformat()

    meals = await db.get_meals_range(profile["id"], owner_id, start_str, end_str)
    liquids = await db.get_liquids_range(profile["id"], owner_id, start_str, end_str)

    # Aggregate calories per day
    per_day: dict[str, int] = defaultdict(int)
    for meal in meals:
        eaten = meal["eaten_at"]
        if isinstance(eaten, str):
            day_str = eaten[:10]
        else:
            day_str = eaten.strftime("%Y-%m-%d")
        per_day[day_str] += meal["calories"] or 0

    for liquid in liquids:
        drunk = liquid["drunk_at"]
        if isinstance(drunk, str):
            day_str = drunk[:10]
        else:
            day_str = drunk.strftime("%Y-%m-%d")
        per_day[day_str] += liquid["calories"] or 0

    daily_data: list[dict] = []
    for i in range(7):
        d = start + timedelta(days=i)
        ds = d.isoformat()
        daily_data.append({"date": ds, "calories": per_day.get(ds, 0)})

    goal = await db.get_goal(profile["id"])
    reply = format_week(profile["name"], daily_data, goal)
    await update.message.reply_text(reply)


# ---------------------------------------------------------------------------
# /report
# ---------------------------------------------------------------------------


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    text = update.message.text or ""

    profile = await resolve_single_profile(owner_id, text)
    if profile is None:
        await update.message.reply_text("Profile not found.")
        return

    # Parse optional date
    date_match = _DATE_RE.search(text)
    if date_match:
        report_date = date_match.group(0)
    else:
        report_date = date.today().isoformat()

    next_day = (datetime.strptime(report_date, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
    meals = await db.get_meals_range(profile["id"], owner_id, report_date, next_day)
    liquids = await db.get_liquids_range(profile["id"], owner_id, report_date, next_day)

    # Calculate totals
    total = {"calories": 0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
    hydration_ml = 0
    for meal in meals:
        total["calories"] += meal["calories"] or 0
        total["protein_g"] += meal["protein_g"] or 0
        total["carbs_g"] += meal["carbs_g"] or 0
        total["fat_g"] += meal["fat_g"] or 0
    
    for liquid in liquids:
        total["calories"] += liquid["calories"] or 0
        total["protein_g"] += liquid["protein_g"] or 0
        total["carbs_g"] += liquid["carbs_g"] or 0
        total["fat_g"] += liquid["fat_g"] or 0
        hydration_ml += liquid["amount_ml"] or 0

    goal = await db.get_goal(profile["id"])
    total["goal"] = goal

    supplements_scheduled = await db.list_supplements(profile["id"], owner_id)
    supplements_taken = await db.get_supplement_logs_today(profile["id"])

    # Enrich taken logs with supplement names for matching
    taken_with_names: list[dict] = []
    for log in supplements_taken:
        for s in supplements_scheduled:
            if s["id"] == log["supplement_id"]:
                taken_with_names.append({"name": s["name"], "supplement_id": log["supplement_id"]})
                break

    reply = format_report(
        profile["name"], report_date, meals, liquids, total, hydration_ml,
        supplements_scheduled, taken_with_names,
    )
    await update.message.reply_text(reply)


# ---------------------------------------------------------------------------
# Exported for scheduler
# ---------------------------------------------------------------------------


async def send_daily_summary(bot, owner_id: int, profile: dict) -> None:
    """Send the daily summary to *owner_id* for *profile*.

    Called by the scheduler -- not by a user command.
    """
    meals = await db.get_meals_today(profile["id"], owner_id)
    liquids = await db.get_liquids_today(profile["id"], owner_id)
    totals = await db.get_daily_totals(profile["id"], owner_id)
    goal = await db.get_goal(profile["id"])
    hydration = await db.get_daily_hydration(profile["id"], owner_id)

    text = format_summary(profile["name"], meals, liquids, totals, goal, hydration)
    await bot.send_message(chat_id=owner_id, text=text)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


COMMANDS: list[tuple[str, str]] = [
    ("summary", "Show today's meal summary"),
    ("week", "Last 7 days overview"),
    ("report", "Daily report for dietitian"),
]


def register(app) -> None:
    app.add_handler(CommandHandler("summary", summary_cmd))
    app.add_handler(CommandHandler("week", week_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
