"""Invoice reading and classification handler.

Uses local gemma4:26b via Ollama. Send an invoice photo or document and the
bot will extract vendor, amount, date, category, and line items.

This is a stub — full implementation pending.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logger = logging.getLogger(__name__)


async def invoice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stub: acknowledge the command and explain next steps."""
    await update.message.reply_text(
        "\U0001f9fe Invoice module is coming soon!\n\n"
        "Send me a photo or document of an invoice and I will extract:\n"
        "- Vendor, date, amount\n"
        "- Category (utilities, food, services, …)\n"
        "- Individual line items\n\n"
        "Powered by local gemma4:26b."
    )


COMMANDS: list[tuple[str, str]] = [
    ("invoice", "Read and classify an invoice (coming soon)"),
]


def register(app: Application) -> None:
    app.add_handler(CommandHandler("invoice", invoice_cmd))
