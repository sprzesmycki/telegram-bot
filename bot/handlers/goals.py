from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.services import db
from bot.utils.formatting import parse_target

logger = logging.getLogger(__name__)

USAGE = "Usage: /goal <calories> [@profile_name]"


# ---------------------------------------------------------------------------
# /goal command
# ---------------------------------------------------------------------------


async def goal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    args = context.args or []
    text = update.message.text or ""

    # Find the first numeric argument.
    kcal: int | None = None
    for arg in args:
        try:
            kcal = int(arg)
            break
        except ValueError:
            continue

    if kcal is None:
        await update.message.reply_text(USAGE)
        return

    # Resolve target profile.
    profile_name, _is_both = parse_target(text)

    if profile_name is not None:
        profile = await db.get_profile_by_name(owner_id, profile_name)
        if profile is None:
            await update.message.reply_text(f"Profile '{profile_name}' not found.")
            return
    else:
        profile = await db.ensure_default_profile(owner_id)

    await db.set_goal(profile["id"], kcal, protein_g=None, carbs_g=None, fat_g=None)
    await update.message.reply_text(
        f"Goal set to {kcal} kcal/day for {profile['name']} (macros reset)"
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


COMMANDS: list[tuple[str, str]] = [
    ("goal", "Set daily calorie target"),
]


def register(app) -> None:
    app.add_handler(CommandHandler("goal", goal_cmd))
