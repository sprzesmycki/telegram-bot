"""/liquid and /pij entry points.

Both commands feed into the same confirmation path as ``/cal``: the analysed
result is stashed in ``context.user_data["pending_meal"]`` with ``kind="liquid"``
so ``yes_cmd`` / ``refine_handler`` in ``calories.py`` can take over.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.handlers._common import handle_llm_error, strip_command
from bot.handlers.profiles import get_target_profiles
from bot.services.llm import analyze_liquid
from bot.utils.formatting import format_liquid_preview, parse_time, strip_command_args

logger = logging.getLogger(__name__)

USAGE = (
    "Usage: /liquid <description and amount> [@name] [at HH:MM]\n"
    "Example: /liquid 500ml water\n"
    "Example: /pij kawa z mlekiem 250ml"
)


async def _send_preview(update: Update, pending: dict) -> None:
    result = pending["result"]
    await update.message.reply_text(
        format_liquid_preview(
            description=result["description"],
            amount_ml=result["amount_ml"],
            cals=result["calories"],
            protein=result["protein_g"],
            carbs=result["carbs_g"],
            fat=result["fat_g"],
            profile_names=[p["name"] for p in pending["profiles"]],
            drunk_at=pending["drunk_at"],
        )
    )


async def liquid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    raw = update.message.text or ""
    text = strip_command(strip_command(raw, "liquid"), "pij")

    if not text:
        await update.message.reply_text(USAGE)
        return

    drunk_at = parse_time(text) or datetime.now()
    description = strip_command_args(text)
    profiles = await get_target_profiles(owner_id, text)

    if not profiles:
        await update.message.reply_text("Profile not found.")
        return

    try:
        result = await analyze_liquid(description)
    except Exception as exc:
        msg = handle_llm_error(exc)
        if msg is None:
            logger.error("analyze_liquid failed", exc_info=True)
            msg = f"Error analyzing liquid: {exc}"
        await update.message.reply_text(msg)
        return

    pending = {
        "kind": "liquid",
        "owner_id": owner_id,
        "description": description,
        "drunk_at": drunk_at,
        "profiles": profiles,
        "result": result,
        "_ts": time.time(),
    }
    context.user_data["pending_meal"] = pending
    await _send_preview(update, pending)


COMMANDS: list[tuple[str, str]] = [
    ("liquid", "Log a drink (amount and type)"),
]


def register(app) -> None:
    app.add_handler(CommandHandler(["liquid", "pij"], liquid_cmd))
