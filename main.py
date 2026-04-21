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

from bot.modules import load_enabled_modules
from bot.services import db
from bot.services.llm import init_llm
from bot.services.scheduler import (
    init_scheduler,
    load_all_reminders,
    shutdown,
    start,
)
from bot.utils.logging_config import setup_logging

setup_logging()
logger = logging.getLogger(__name__)


async def unknown_cmd(update, context) -> None:
    await update.message.reply_text(
        "Unknown command. Use /profile, /remind, /model, or the commands specific to enabled modules."
    )


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


async def post_init(app: Application) -> None:
    """Run once after the Application has been initialised."""
    modules = app.bot_data["modules"]

    await db.init_db()
    init_llm()

    # Collect commands from all loaded modules
    commands = [
        BotCommand(name, desc)
        for mod in modules
        for name, desc in getattr(mod, "COMMANDS", [])
    ]
    await app.bot.set_my_commands(commands)

    scheduler = init_scheduler()
    app.bot_data["scheduler"] = scheduler

    await load_all_reminders(scheduler, app.bot)

    for mod in modules:
        mod.register_scheduled(scheduler, app.bot)

    start(scheduler)
    logger.info(
        "Bot started — modules: %s",
        [type(m).__name__ for m in modules],
    )


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

    modules = load_enabled_modules()

    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["modules"] = modules

    for mod in modules:
        mod.register(app)

    # Catch-all for unknown commands (must be last)
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))
    app.add_error_handler(error_handler)

    logger.info("Starting polling…")
    app.run_polling()


if __name__ == "__main__":
    main()