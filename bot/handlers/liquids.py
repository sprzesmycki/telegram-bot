from __future__ import annotations

import json
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from bot.handlers.profiles import get_target_profiles
from bot.services import db_postgres, db_sqlite
from bot.services.llm import analyze_liquid
from bot.utils.formatting import (
    format_liquid_logged,
    format_liquid_preview,
    parse_time,
    strip_command_args,
)

logger = logging.getLogger(__name__)


async def _log_and_reply(
    update: Update,
    owner_id: int,
    profiles: list[dict],
    drunk_at: datetime,
    description: str,
    amount_ml: int,
    calories: int,
    protein_g: float,
    carbs_g: float,
    fat_g: float,
    raw_llm: str,
) -> None:
    """Log a drink for each profile, mirror to PG, and send a reply."""
    reply_parts: list[str] = []

    for profile in profiles:
        profile_id = profile["id"]
        profile_name = profile["name"]

        liquid_id = await db_sqlite.log_liquid(
            profile_id=profile_id,
            owner_id=owner_id,
            drunk_at=drunk_at,
            description=description,
            amount_ml=amount_ml,
            calories=calories,
            protein_g=protein_g,
            carbs_g=carbs_g,
            fat_g=fat_g,
            raw_llm=raw_llm,
        )
        await db_postgres.mirror_log_liquid(
            liquid_id=liquid_id,
            profile_id=profile_id,
            owner_id=owner_id,
            drunk_at=drunk_at,
            description=description,
            amount_ml=amount_ml,
            calories=calories,
            protein_g=protein_g,
            carbs_g=carbs_g,
            fat_g=fat_g,
            raw_llm=raw_llm,
        )

        totals = await db_sqlite.get_daily_totals(profile_id, owner_id)
        goal = await db_sqlite.get_goal(profile_id)
        hydration = await db_sqlite.get_daily_hydration(profile_id, owner_id)

        reply_parts.append(
            format_liquid_logged(
                profile_name=profile_name,
                description=description,
                amount_ml=amount_ml,
                cals=calories,
                protein=protein_g,
                carbs=carbs_g,
                fat=fat_g,
                daily_total=totals,
                goal=goal,
                hydration_ml=hydration,
            )
        )

    await update.message.reply_text("\n\n".join(reply_parts))


async def _send_liquid_preview(update: Update, pending: dict) -> None:
    """Format and send the current pending_liquid preview."""
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
    text = (update.message.text or "")
    if text.lower().startswith("/liquid"):
        text = text[7:].strip()
    elif text.lower().startswith("/pij"):
        text = text[4:].strip()

    if not text:
        await update.message.reply_text(
            "Usage: /liquid <description and amount> [@name] [at HH:MM]\n"
            "Example: /liquid 500ml water\n"
            "Example: /pij kawa z mlekiem 250ml"
        )
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
        # Reusing the error handler logic would be good, but for now just raise or handle simply
        logger.error("analyze_liquid failed", exc_info=True)
        await update.message.reply_text(f"Error analyzing liquid: {exc}")
        return

    context.user_data["pending_meal"] = {
        "kind": "liquid",
        "owner_id": owner_id,
        "description": description,
        "drunk_at": drunk_at,
        "profiles": profiles,
        "result": result,
    }

    await _send_liquid_preview(update, context.user_data["pending_meal"])


def register(app) -> None:
    app.add_handler(CommandHandler(["liquid", "pij"], liquid_cmd))
