"""Scheduled jobs for the Gmail module.

Polls for new unread mail at a configurable interval and notifies all profile
owners when new messages arrive.
"""
from __future__ import annotations

import asyncio
import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from bot.config import get_config

logger = logging.getLogger(__name__)

_last_seen_count: int = 0
_gmail_service_cache: dict[str, object] = {}


def register_all(scheduler: AsyncIOScheduler, bot) -> None:
    cfg = get_config().modules.gmail
    _register_mail_check(scheduler, bot, cfg.check_interval_minutes)


def _register_mail_check(scheduler: AsyncIOScheduler, bot, interval_minutes: int) -> None:
    async def _check_new_mail() -> None:
        global _last_seen_count
        from bot.modules.gmail.handlers.emails import format_email
        from bot.services import db
        from bot.services.gmail import fetch_unread, get_unread_count, load_gmail_service

        cfg = get_config()
        credentials_path = os.getenv("GMAIL_CREDENTIALS_PATH", "./credentials.json")
        loop = asyncio.get_event_loop()

        try:
            if credentials_path not in _gmail_service_cache:
                _gmail_service_cache[credentials_path] = await loop.run_in_executor(
                    None, lambda: load_gmail_service(credentials_path)
                )
            service = _gmail_service_cache[credentials_path]
            count = await loop.run_in_executor(
                None, lambda: get_unread_count(service, cfg.modules.gmail.label)
            )
        except Exception:
            logger.error("Gmail scheduler: auth/count error", exc_info=True)
            _gmail_service_cache.pop(credentials_path, None)
            return

        if count <= _last_seen_count:
            _last_seen_count = count
            return

        new_count = count - _last_seen_count
        try:
            emails = await loop.run_in_executor(
                None,
                lambda: fetch_unread(
                    service,
                    cfg.modules.gmail.label,
                    new_count,
                    None,
                    cfg.storage.gmail_attachments_dir,
                ),
            )
        except Exception:
            logger.error("Gmail scheduler: fetch error", exc_info=True)
            return

        owner_ids = await db.get_distinct_profile_owner_ids()
        for owner_id in owner_ids:
            for email_data in emails:
                try:
                    text, kb = format_email(email_data)
                    await bot.send_message(
                        chat_id=owner_id,
                        text=f"📬 New email!\n\n{text}",
                        reply_markup=kb,
                    )
                except Exception:
                    logger.error(
                        "Gmail scheduler: send failed for owner=%s", owner_id, exc_info=True
                    )

        _last_seen_count = count

    scheduler.add_job(
        _check_new_mail,
        IntervalTrigger(minutes=interval_minutes),
        id="gmail_check",
        replace_existing=True,
    )
    logger.info("Gmail: mail check scheduled every %d minutes", interval_minutes)
