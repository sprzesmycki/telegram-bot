from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.services.llm import get_provider_info, switch_provider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# /model command
# ---------------------------------------------------------------------------


async def model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []

    if not args:
        info = get_provider_info()
        await update.message.reply_text(
            f"Provider: {info['provider']}\n"
            f"Model: {info['model']}\n"
            f"Base URL: {info['base_url']}"
        )
        return

    provider = args[0].lower()
    if provider not in ("openrouter", "local", "custom"):
        await update.message.reply_text(
            "Unknown provider. Use: openrouter, local, or custom."
        )
        return

    model_override = args[1] if len(args) >= 2 else None
    switch_provider(provider, model_override)

    info = get_provider_info()
    await update.message.reply_text(
        f"Switched to {info['provider']}\n"
        f"Model: {info['model']}\n"
        f"Base URL: {info['base_url']}"
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(app) -> None:
    app.add_handler(CommandHandler("model", model_cmd))
