"""Scheduled jobs for the calories module.

Registers two cron jobs on startup:
- daily_summary  — sends each profile a nutrition summary at daily_summary_time
- daily_review   — sends each profile an AI-powered day review at daily_review_time

Both times are read from config.yaml modules.calories.schedules.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.config import get_config

logger = logging.getLogger(__name__)


def register_all(scheduler: AsyncIOScheduler, bot) -> None:
    cfg = get_config().modules.calories
    _register_daily_summary(scheduler, bot, cfg.daily_summary_time)
    _register_daily_review(scheduler, bot, cfg.daily_review_time)


def _register_daily_summary(scheduler: AsyncIOScheduler, bot, time_str: str) -> None:
    hour, minute = map(int, time_str.split(":"))

    async def _send_summaries() -> None:
        from bot.modules.calories.handlers.summary import send_daily_summary
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
    logger.info("Calories: daily summary scheduled at %s", time_str)


def _register_daily_review(scheduler: AsyncIOScheduler, bot, time_str: str) -> None:
    hour, minute = map(int, time_str.split(":"))

    async def _send_reviews() -> None:
        from bot.modules.calories.handlers.review import send_daily_review
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
    logger.info("Calories: daily review scheduled at %s", time_str)
