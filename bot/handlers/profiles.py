from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.services import db_sqlite, db_postgres
from bot.utils.formatting import format_profile_list, parse_target

logger = logging.getLogger(__name__)

USAGE = (
    "Usage:\n"
    "/profile add <name>\n"
    "/profile list\n"
    "/profile switch <name>\n"
    "/profile delete <name>"
)


# ---------------------------------------------------------------------------
# /profile command
# ---------------------------------------------------------------------------


async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    args = context.args or []

    if not args:
        await update.message.reply_text(USAGE)
        return

    sub = args[0].lower()

    if sub == "add" and len(args) >= 2:
        name = args[1]
        existing = await db_sqlite.list_profiles(owner_id)
        existing_names = {p["name"] for p in existing}

        if name in existing_names:
            await update.message.reply_text(f"Profile '{name}' already exists.")
            return

        # Auto-create "Me" as default if this is the first profile
        # and the user isn't explicitly adding "Me" themselves.
        if not existing and name != "Me":
            me_id = await db_sqlite.create_profile(owner_id, "Me")
            await db_sqlite.set_active_profile(owner_id, me_id)
            await db_postgres.mirror_create_profile(me_id, owner_id, "Me")
            await db_postgres.mirror_set_active_profile(owner_id, me_id)

        profile_id = await db_sqlite.create_profile(owner_id, name)
        await db_postgres.mirror_create_profile(profile_id, owner_id, name)

        # If the user's first profile is the one they named themselves,
        # make it active.
        if not existing:
            await db_sqlite.set_active_profile(owner_id, profile_id)
            await db_postgres.mirror_set_active_profile(owner_id, profile_id)

        await update.message.reply_text(f"Profile '{name}' created.")

    elif sub == "list":
        profiles = await db_sqlite.list_profiles(owner_id)
        active = await db_sqlite.get_active_profile(owner_id)
        active_id = active["id"] if active else None
        text = format_profile_list(profiles, active_id) if profiles else "No profiles yet."
        await update.message.reply_text(text)

    elif sub == "switch" and len(args) >= 2:
        name = args[1]
        profile = await db_sqlite.get_profile_by_name(owner_id, name)
        if profile is None:
            await update.message.reply_text(f"Profile '{name}' not found.")
            return
        await db_sqlite.set_active_profile(owner_id, profile["id"])
        await db_postgres.mirror_set_active_profile(owner_id, profile["id"])
        await update.message.reply_text(f"Switched to profile '{name}'.")

    elif sub == "delete" and len(args) >= 2:
        name = args[1]
        profiles = await db_sqlite.list_profiles(owner_id)
        if len(profiles) <= 1:
            await update.message.reply_text("Cannot delete your last profile.")
            return
        deleted = await db_sqlite.delete_profile(owner_id, name)
        if deleted:
            await db_postgres.mirror_delete_profile(owner_id, name)
            await update.message.reply_text(f"Profile '{name}' deleted.")
        else:
            await update.message.reply_text(f"Profile '{name}' not found.")

    else:
        await update.message.reply_text(USAGE)


# ---------------------------------------------------------------------------
# Helper for other handlers
# ---------------------------------------------------------------------------


async def get_target_profiles(owner_id: int, text: str) -> list[dict]:
    """Resolve target profiles from ``@name`` / ``@both`` in *text*.

    Returns a list of profile dicts.
    """
    profile_name, is_both = parse_target(text)

    if is_both:
        return await db_sqlite.get_all_profiles(owner_id)

    if profile_name is not None:
        profile = await db_sqlite.get_profile_by_name(owner_id, profile_name)
        if profile is None:
            return []
        return [profile]

    active = await db_sqlite.ensure_default_profile(owner_id)
    return [active]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(app) -> None:
    app.add_handler(CommandHandler("profile", profile_cmd))
