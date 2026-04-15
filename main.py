from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# Load env before any other project imports. Historical path for host runs,
# plus CWD (the compose container sets env via env_file so both no-op there).
load_dotenv(Path.home() / ".config" / "telegrambot" / ".env", override=False)
load_dotenv(override=False)

from telegram import BotCommand
from telegram.ext import Application, MessageHandler, filters

from bot.handlers import (
    calories,
    goals,
    liquids,
    model,
    piano,
    profiles,
    reminders,
    review,
    summary,
    supplements,
)
from bot.services import db
from bot.services.llm import init_llm
from bot.services.scheduler import (
    init_scheduler,
    load_all_reminders,
    register_daily_review,
    register_daily_summary,
    register_piano_checkin,
    shutdown,
    start,
)
from bot.utils.formatting import format_help
from bot.utils.logging_config import setup_logging

setup_logging()
logger = logging.getLogger(__name__)


# Registration order matters: piano's text handler must run before calories'
# refine handler so /piano log's pending_piano_log state wins. The catch-all
# unknown-command handler is attached after this loop.
HANDLER_MODULES = (
    profiles,
    goals,
    piano,
    calories,
    liquids,
    summary,
    supplements,
    reminders,
    model,
    review
)


async def unknown_cmd(update, context) -> None:
    await update.message.reply_text(format_help())


async def error_handler(update, context) -> None:
    """Catch-all error handler so the user always gets feedback."""
    logger.error("Unhandled exception", exc_info=context.error)
    if update and getattr(update, "message", None):
        try:
            await update.message.reply_text(
                f"Something went wrong: {context.error}"
            )
        except Exception:
            pass


def _collect_bot_commands() -> list[BotCommand]:
    """Flatten the per-module COMMANDS lists into BotCommand objects."""
    commands: list[BotCommand] = []
    for module in HANDLER_MODULES:
        for name, desc in getattr(module, "COMMANDS", []):
            commands.append(BotCommand(name, desc))
    return commands


async def post_init(app: Application) -> None:
    """Run once after the Application has been initialised."""
    await db.init_db()
    init_llm()

    await app.bot.set_my_commands(_collect_bot_commands())

    scheduler = init_scheduler()
    app.bot_data["scheduler"] = scheduler

    await load_all_reminders(scheduler, app.bot)
    register_daily_summary(scheduler, app.bot)
    register_daily_review(scheduler, app.bot)
    register_piano_checkin(scheduler, app.bot)
    start(scheduler)

    logger.info("Bot started")


async def post_shutdown(app: Application) -> None:
    """Clean up on shutdown."""
    scheduler = app.bot_data.get("scheduler")
    if scheduler:
        shutdown(scheduler)

    await db.close_db()
    logger.info("Bot shut down")


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set")

    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    for module in HANDLER_MODULES:
        module.register(app)

    # Catch-all for unknown commands (must be last)
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))
    app.add_error_handler(error_handler)

    logger.info("Starting polling…")
    app.run_polling()


if __name__ == "__main__":
    main()
