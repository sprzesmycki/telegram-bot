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
    from zoneinfo import ZoneInfo
    scheduler = AsyncIOScheduler(timezone=ZoneInfo("Europe/Warsaw"))
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
        from bot.services import db

        supplements = await db.get_all_active_supplements()
        seen_owners: set[int] = {sup["owner_user_id"] for sup in supplements}
        seen_owners.update(await db.get_distinct_profile_owner_ids())

        for owner_id in seen_owners:
            owner_profiles = await db.list_profiles(owner_id)
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
# Daily nutrition review
# ---------------------------------------------------------------------------


def register_daily_review(scheduler: AsyncIOScheduler, bot) -> None:
    """Register the daily nutrition review job at DAILY_REVIEW_TIME.

    Runs once per day per profile with data logged today. Skips profiles
    with no logged meals/drinks so we don't nag users who were off that day.
    """
    time_str = os.getenv("DAILY_REVIEW_TIME", "22:00")
    parts = time_str.split(":")
    hour, minute = int(parts[0]), int(parts[1])

    async def _send_reviews() -> None:
        from bot.handlers.review import send_daily_review
        from bot.services import db

        owner_ids = await db.get_distinct_profile_owner_ids()

        for owner_id in owner_ids:
            owner_profiles = await db.list_profiles(owner_id)
            for profile in owner_profiles:
                try:
                    await send_daily_review(bot, owner_id, profile)
                except Exception:
                    logger.error(
                        "Failed to send daily review for owner=%s profile=%s",
                        owner_id, profile["name"], exc_info=True,
                    )

    scheduler.add_job(
        _send_reviews,
        CronTrigger(hour=hour, minute=minute),
        id="daily_review",
        replace_existing=True,
    )
    logger.info("Daily review scheduled at %s", time_str)


# ---------------------------------------------------------------------------
# Piano daily check-in
# ---------------------------------------------------------------------------


def register_piano_checkin(scheduler: AsyncIOScheduler, bot) -> None:
    """Register the daily piano check-in job at PIANO_CHECKIN_TIME.

    Iterates every known piano owner. Skips any owner whose session is
    already logged today so users aren't nagged after they've practised.
    """
    time_str = os.getenv("PIANO_CHECKIN_TIME", "19:00")
    parts = time_str.split(":")
    hour, minute = int(parts[0]), int(parts[1])

    async def _send_checkins() -> None:
        from bot.services import db

        owner_ids = set(await db.get_piano_owners())
        owner_ids.update(await db.get_distinct_profile_owner_ids())

        for owner_id in owner_ids:
            try:
                session = await db.get_piano_session_today(owner_id)
                if session is not None:
                    continue
                await bot.send_message(
                    chat_id=owner_id,
                    text=(
                        "\U0001f3b9 Time for your daily piano check-in! "
                        "Use /piano log to record today's practice, or tell me about it."
                    ),
                )
            except Exception:
                logger.error(
                    "Failed to send piano check-in for owner=%s", owner_id, exc_info=True
                )

    scheduler.add_job(
        _send_checkins,
        CronTrigger(hour=hour, minute=minute),
        id="piano_checkin",
        replace_existing=True,
    )
    logger.info("Piano check-in scheduled at %s", time_str)


# ---------------------------------------------------------------------------
# Bulk load on startup
# ---------------------------------------------------------------------------


async def load_all_reminders(scheduler: AsyncIOScheduler, bot) -> None:
    """Load all active supplements and generic reminders from DB and register jobs."""
    from zoneinfo import ZoneInfo
    from bot.services import db

    supplements = await db.get_all_active_supplements()
    for sup in supplements:
        register_supplement_reminder(scheduler, bot, sup)
    logger.info("Loaded %d supplement reminders from DB", len(supplements))

    now = datetime.now(ZoneInfo("Europe/Warsaw"))
    reminders = await db.get_all_active_reminders()
    loaded = 0
    for reminder in reminders:
        if not reminder.get("repeat", True):
            remind_at = reminder.get("remind_at")
            if remind_at is None or remind_at <= now:
                # One-time reminder whose time already passed — deactivate silently.
                await db.deactivate_reminder(reminder["id"])
                logger.info("Deactivated past one-time reminder #%s", reminder["id"])
                continue
        register_reminder_job(scheduler, bot, reminder)
        loaded += 1
    logger.info("Loaded %d generic reminders from DB", loaded)


# ---------------------------------------------------------------------------
# Generic reminders
# ---------------------------------------------------------------------------


def _build_reminder_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("\u2705 Zrobione", callback_data=f"rd:{reminder_id}"),
        InlineKeyboardButton("\U0001f514 +1h",  callback_data=f"rs:{reminder_id}"),
        InlineKeyboardButton("\u274c Pomiń",    callback_data=f"rx:{reminder_id}"),
    ]])


def register_reminder_job(scheduler: AsyncIOScheduler, bot, reminder: dict) -> None:
    """Register a job for a generic reminder.

    For recurring reminders (repeat=True): CronTrigger.
    For one-time reminders (repeat=False): DateTrigger using remind_at.
    After a one-time reminder fires it is soft-deleted from the DB.
    """
    rid = reminder["id"]
    owner_id = reminder["owner_user_id"]
    message = reminder["message"]
    repeat = reminder.get("repeat", True)
    remind_at = reminder.get("remind_at")
    job_id = f"reminder_{rid}"
    keyboard = _build_reminder_keyboard(rid)

    async def _send() -> None:
        try:
            await bot.send_message(
                chat_id=owner_id,
                text=f"\U0001f514 {message}",
                reply_markup=keyboard,
            )
        except Exception:
            logger.error("Failed to send reminder id=%s", rid, exc_info=True)
        if not repeat:
            from bot.services import db as _db
            await _db.deactivate_reminder(rid)
            logger.debug("One-time reminder #%s deactivated after firing", rid)

    if not repeat and remind_at is not None:
        scheduler.add_job(
            _send,
            DateTrigger(run_date=remind_at),
            id=job_id,
            replace_existing=True,
        )
        logger.debug("Registered one-time reminder job %s at %s", job_id, remind_at)
    else:
        parts = reminder["reminder_time"].split(":")
        hour, minute = int(parts[0]), int(parts[1])
        days = reminder.get("days_of_week") or "*"
        trigger_kwargs: dict = {"hour": hour, "minute": minute}
        if days != "*":
            trigger_kwargs["day_of_week"] = days
        scheduler.add_job(
            _send,
            CronTrigger(**trigger_kwargs),
            id=job_id,
            replace_existing=True,
        )
        logger.debug(
            "Registered recurring reminder job %s at %02d:%02d days=%s",
            job_id, hour, minute, days,
        )


def remove_reminder_job(scheduler: AsyncIOScheduler, reminder_id: int) -> None:
    job_id = f"reminder_{reminder_id}"
    try:
        scheduler.remove_job(job_id)
        logger.debug("Removed reminder job %s", job_id)
    except Exception:
        logger.debug("Job %s not found for removal", job_id)


def schedule_snooze_reminder(
    scheduler: AsyncIOScheduler, bot, reminder: dict, owner_id: int,
) -> None:
    """Schedule a one-shot reminder in 1 hour (snooze)."""
    rid = reminder["id"]
    message = reminder["message"]
    run_at = datetime.now() + timedelta(hours=1)
    job_id = f"snooze_reminder_{rid}_{owner_id}"
    keyboard = _build_reminder_keyboard(rid)

    async def _send_snooze() -> None:
        try:
            await bot.send_message(
                chat_id=owner_id,
                text=f"\U0001f514 {message}",
                reply_markup=keyboard,
            )
        except Exception:
            logger.error("Failed to send snoozed reminder id=%s", rid, exc_info=True)

    scheduler.add_job(
        _send_snooze,
        DateTrigger(run_date=run_at),
        id=job_id,
        replace_existing=True,
    )
    logger.debug("Snooze scheduled for reminder #%s at %s", rid, run_at.strftime("%H:%M"))
