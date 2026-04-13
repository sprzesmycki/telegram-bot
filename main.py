from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# Load env before any other project imports
load_dotenv(Path.home() / ".config" / "telegrambot" / ".env")

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from bot.handlers import calories, goals, model, profiles, summary, supplements
from bot.services import db_postgres, db_sqlite
from bot.services.llm import init_llm
from bot.services.scheduler import (
    init_scheduler,
    load_all_reminders,
    register_daily_summary,
    shutdown,
    start,
)
from bot.utils.formatting import format_help
from bot.utils.logging_config import setup_logging

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

setup_logging()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Unknown command handler
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Startup / shutdown hooks
# ---------------------------------------------------------------------------


async def post_init(app: Application) -> None:
    """Run once after the Application has been initialised."""
    await db_sqlite.init_db()
    await db_postgres.init_pg()
    init_llm()

    scheduler = init_scheduler()
    app.bot_data["scheduler"] = scheduler

    await load_all_reminders(scheduler, app.bot)
    register_daily_summary(scheduler, app.bot)
    start(scheduler)

    logger.info("Bot started")


async def post_shutdown(app: Application) -> None:
    """Clean up on shutdown."""
    scheduler = app.bot_data.get("scheduler")
    if scheduler:
        shutdown(scheduler)

    await db_sqlite.close_db()
    await db_postgres.close_pg()
    logger.info("Bot shut down")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


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

    # Register handlers
    profiles.register(app)
    goals.register(app)
    calories.register(app)
    summary.register(app)
    supplements.register(app)
    model.register(app)

    # Catch-all for unknown commands (must be last)
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    # Global error handler
    app.add_error_handler(error_handler)

    logger.info("Starting polling…")
    app.run_polling()


if __name__ == "__main__":
    main()
