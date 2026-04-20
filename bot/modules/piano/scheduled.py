"""Scheduled jobs for the piano module.

Registers a single cron job:
- piano_checkin — prompts each owner who hasn't logged today at checkin_time

Time is read from config.yaml modules.piano.schedules.checkin_time.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.config import get_config

logger = logging.getLogger(__name__)


def register_all(scheduler: AsyncIOScheduler, bot) -> None:
    cfg = get_config().modules.piano
    _register_piano_checkin(scheduler, bot, cfg.checkin_time)


def _register_piano_checkin(scheduler: AsyncIOScheduler, bot, time_str: str) -> None:
    hour, minute = map(int, time_str.split(":"))

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
    logger.info("Piano: check-in scheduled at %s", time_str)
