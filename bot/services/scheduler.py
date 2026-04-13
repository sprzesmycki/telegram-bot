from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------


def init_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    logger.info("Scheduler created")
    return scheduler


def start(scheduler: AsyncIOScheduler) -> None:
    scheduler.start()
    logger.info("Scheduler started")


def shutdown(scheduler: AsyncIOScheduler) -> None:
    scheduler.shutdown(wait=False)
    logger.info("Scheduler shut down")


# ---------------------------------------------------------------------------
# Supplement reminders — shared helpers
# ---------------------------------------------------------------------------


def _build_reminder_message(supplement: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Build the reminder text and inline keyboard for a supplement."""
    name = supplement["name"]
    dose = supplement.get("dose") or ""
    profile = supplement["profile_name"]
    sup_id = supplement["id"]
    profile_id = supplement["profile_id"]

    dose_str = f" ({dose})" if dose else ""
    text = f"\U0001f48a [{profile}] Time to take {name}{dose_str}!"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("\u2705 Took it", callback_data=f"sd:{sup_id}:{profile_id}"),
        InlineKeyboardButton("\U0001f514 +1h",  callback_data=f"ss:{sup_id}:{profile_id}"),
        InlineKeyboardButton("\u274c Skip",      callback_data=f"sx:{sup_id}:{profile_id}"),
    ]])
    return text, keyboard


async def send_supplement_reminder(bot, supplement: dict, owner_id: int) -> None:
    """Send the supplement reminder message with action buttons."""
    text, keyboard = _build_reminder_message(supplement)
    try:
        await bot.send_message(chat_id=owner_id, text=text, reply_markup=keyboard)
    except Exception:
        logger.error(
            "Failed to send supplement reminder for %s", supplement["name"], exc_info=True
        )


# ---------------------------------------------------------------------------
# Cron reminder registration
# ---------------------------------------------------------------------------


def register_supplement_reminder(
    scheduler: AsyncIOScheduler, bot, supplement: dict,
) -> None:
    """Register a CronTrigger job for a single supplement reminder.

    *supplement* must have keys: id, profile_id, name, dose (optional),
    reminder_time (HH:MM), owner_user_id, profile_name.
    """
    parts = supplement["reminder_time"].split(":")
    hour, minute = int(parts[0]), int(parts[1])
    job_id = f"supplement_{supplement['id']}"
    owner_id = supplement["owner_user_id"]

    async def _send_reminder() -> None:
        await send_supplement_reminder(bot, supplement, owner_id)

    scheduler.add_job(
        _send_reminder,
        CronTrigger(hour=hour, minute=minute),
        id=job_id,
        replace_existing=True,
    )
    logger.debug("Registered reminder job %s at %02d:%02d", job_id, hour, minute)


# ---------------------------------------------------------------------------
# Snooze (one-shot reminder in 1 hour)
# ---------------------------------------------------------------------------


def schedule_snooze_reminder(
    scheduler: AsyncIOScheduler, bot, supplement: dict, owner_id: int,
) -> None:
    """Schedule a one-shot reminder 1 hour from now for *supplement*."""
    run_at = datetime.now() + timedelta(hours=1)
    job_id = f"snooze_{supplement['id']}_{owner_id}"

    async def _send_snooze() -> None:
        await send_supplement_reminder(bot, supplement, owner_id)

    scheduler.add_job(
        _send_snooze,
        DateTrigger(run_date=run_at),
        id=job_id,
        replace_existing=True,
    )
    logger.debug(
        "Snooze scheduled for supplement %d at %s", supplement["id"], run_at.strftime("%H:%M")
    )


def remove_supplement_reminder(
    scheduler: AsyncIOScheduler, supplement_id: int,
) -> None:
    job_id = f"supplement_{supplement_id}"
    try:
        scheduler.remove_job(job_id)
        logger.debug("Removed reminder job %s", job_id)
    except Exception:
        logger.debug("Job %s not found for removal", job_id)


# ---------------------------------------------------------------------------
# Daily summary
# ---------------------------------------------------------------------------


def register_daily_summary(scheduler: AsyncIOScheduler, bot) -> None:
    """Register the daily summary job at DAILY_SUMMARY_TIME."""
    time_str = os.getenv("DAILY_SUMMARY_TIME", "21:00")
    parts = time_str.split(":")
    hour, minute = int(parts[0]), int(parts[1])

    async def _send_summaries() -> None:
        from bot.handlers.summary import send_daily_summary
        from bot.services import db_sqlite

        # Get all distinct owners with active profiles
        profiles = await db_sqlite.get_all_active_supplements()
        seen_owners: set[int] = set()
        for sup in profiles:
            seen_owners.add(sup["owner_user_id"])

        # Also check for owners with profiles but no supplements
        # We iterate all known owners by checking profiles table
        # For simplicity, just use the owners we already have from supplements
        # plus any owner with an active profile
        db = db_sqlite.get_db()
        cursor = await db.execute(
            "SELECT DISTINCT owner_user_id FROM profiles WHERE active = 1"
        )
        rows = await cursor.fetchall()
        for row in rows:
            seen_owners.add(row[0] if isinstance(row, tuple) else row["owner_user_id"])

        for owner_id in seen_owners:
            owner_profiles = await db_sqlite.list_profiles(owner_id)
            for profile in owner_profiles:
                try:
                    await send_daily_summary(bot, owner_id, profile)
                except Exception:
                    logger.error(
                        "Failed to send daily summary for owner=%s profile=%s",
                        owner_id, profile["name"], exc_info=True,
                    )

    scheduler.add_job(
        _send_summaries,
        CronTrigger(hour=hour, minute=minute),
        id="daily_summary",
        replace_existing=True,
    )
    logger.info("Daily summary scheduled at %s", time_str)


# ---------------------------------------------------------------------------
# Bulk load on startup
# ---------------------------------------------------------------------------


async def load_all_reminders(scheduler: AsyncIOScheduler, bot) -> None:
    """Load all active supplements from DB and register reminder jobs."""
    from bot.services import db_sqlite

    supplements = await db_sqlite.get_all_active_supplements()
    for sup in supplements:
        register_supplement_reminder(scheduler, bot, sup)
    logger.info("Loaded %d supplement reminders from DB", len(supplements))
