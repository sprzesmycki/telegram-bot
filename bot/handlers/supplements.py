from __future__ import annotations

import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from bot.handlers.profiles import resolve_single_profile
from bot.services import db
from bot.utils.formatting import format_supplement_list

logger = logging.getLogger(__name__)

_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")

USAGE = (
    "Usage:\n"
    "/supplement add <name> <HH:MM> [dose] [@profile]\n"
    "/supplement list [@profile]\n"
    "/supplement today [@profile]\n"
    "/supplement done <name> [@profile]\n"
    "/supplement remove <name> [@profile]"
)


# ---------------------------------------------------------------------------
# /supplement command
# ---------------------------------------------------------------------------


async def supplement_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    args = context.args or []
    text = update.message.text or ""

    if not args:
        await update.message.reply_text(USAGE)
        return

    sub = args[0].lower()

    if sub == "add":
        await _supplement_add(update, context, owner_id, args, text)
    elif sub == "list":
        await _supplement_list(update, owner_id, text)
    elif sub == "today":
        await _supplement_today(update, owner_id, text)
    elif sub == "done":
        await _supplement_done(update, owner_id, args, text)
    elif sub == "remove":
        await _supplement_remove(update, context, owner_id, args, text)
    else:
        await update.message.reply_text(USAGE)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


async def _supplement_add(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
    args: list[str],
    text: str,
) -> None:
    # /supplement add <name> <HH:MM> [dose] [@profile]
    if len(args) < 3:
        await update.message.reply_text("Usage: /supplement add <name> <HH:MM> [dose] [@profile]")
        return

    name = args[1]
    reminder_time = args[2]

    if not _TIME_RE.match(reminder_time):
        await update.message.reply_text("Invalid time format. Use HH:MM (e.g. 09:00).")
        return

    # Optional dose: next arg if present and not a @profile token
    dose = args[3] if len(args) >= 4 and not args[3].startswith("@") else None

    profile = await resolve_single_profile(owner_id, text)
    if profile is None:
        await update.message.reply_text("Profile not found.")
        return

    supplement_id = await db.add_supplement(
        profile["id"], owner_id, name, reminder_time, dose,
    )

    # Register scheduler job if scheduler is available
    scheduler = context.bot_data.get("scheduler")
    if scheduler is not None:
        try:
            from bot.services.scheduler import register_supplement_reminder

            supplement_dict = {
                "id": supplement_id,
                "profile_id": profile["id"],
                "name": name,
                "reminder_time": reminder_time,
                "dose": dose,
                "owner_user_id": owner_id,
                "profile_name": profile["name"],
            }
            register_supplement_reminder(scheduler, context.bot, supplement_dict)
        except Exception:
            logger.warning("Could not register scheduler reminder", exc_info=True)

    dose_part = f" ({dose})" if dose else ""
    await update.message.reply_text(
        f"Supplement '{name}'{dose_part} added at {reminder_time} for {profile['name']}."
    )


async def _supplement_list(
    update: Update, owner_id: int, text: str,
) -> None:
    profile = await resolve_single_profile(owner_id, text)
    if profile is None:
        await update.message.reply_text("Profile not found.")
        return

    supplements = await db.list_supplements(profile["id"], owner_id)
    if not supplements:
        await update.message.reply_text(f"No supplements for {profile['name']}.")
        return

    reply = format_supplement_list(supplements)
    await update.message.reply_text(reply)


async def _supplement_today(
    update: Update, owner_id: int, text: str,
) -> None:
    profile = await resolve_single_profile(owner_id, text)
    if profile is None:
        await update.message.reply_text("Profile not found.")
        return

    supplements = await db.list_supplements(profile["id"], owner_id)
    if not supplements:
        await update.message.reply_text(f"No supplements for {profile['name']}.")
        return

    logs_today = await db.get_supplement_logs_today(profile["id"])
    taken_ids = {log["supplement_id"] for log in logs_today}

    msg, keyboard = _build_today_view(supplements, taken_ids, profile["name"])
    await update.message.reply_text(msg, reply_markup=keyboard)


def _build_today_view(
    supplements: list[dict], taken_ids: set[int], profile_name: str,
) -> tuple[str, InlineKeyboardMarkup]:
    header = f"Supplements — {profile_name} — today:"
    rows = []
    for sup in supplements:
        checked = sup["id"] in taken_ids
        icon = "\u2705" if checked else "\u2b1c"
        dose = f" ({sup['dose']})" if sup.get("dose") else ""
        label = f"{icon} {sup['name']}{dose} \u2014 {sup['reminder_time']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"st:{sup['id']}:{sup['profile_id']}")])
    return header, InlineKeyboardMarkup(rows)


async def supplement_today_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle today's taken status for a supplement (inline button on /supplement today)."""
    query = update.callback_query
    await query.answer()

    try:
        _, sup_id_str, profile_id_str = query.data.split(":")
        supplement_id = int(sup_id_str)
        profile_id = int(profile_id_str)
    except (ValueError, IndexError):
        logger.error("Invalid supplement today callback data: %s", query.data)
        return

    owner_id = update.effective_user.id

    logs_today = await db.get_supplement_logs_today(profile_id)
    taken_ids = {log["supplement_id"] for log in logs_today}

    if supplement_id in taken_ids:
        await db.delete_supplement_log_today(supplement_id, profile_id)
    else:
        await db.log_supplement_taken(supplement_id, profile_id)

    # Re-fetch and re-render in place
    supplements = await db.list_supplements(profile_id, owner_id)
    profile = await db.get_profile_by_id(profile_id)
    profile_name = profile["name"] if profile else str(profile_id)

    logs_today = await db.get_supplement_logs_today(profile_id)
    taken_ids = {log["supplement_id"] for log in logs_today}

    msg, keyboard = _build_today_view(supplements, taken_ids, profile_name)
    await query.edit_message_text(msg, reply_markup=keyboard)


async def _supplement_done(
    update: Update, owner_id: int, args: list[str], text: str,
) -> None:
    if len(args) < 2:
        await update.message.reply_text("Usage: /supplement done <name> [@profile]")
        return

    name = args[1]
    profile = await resolve_single_profile(owner_id, text)
    if profile is None:
        await update.message.reply_text("Profile not found.")
        return

    supplement = await db.get_supplement_by_name(profile["id"], owner_id, name)
    if supplement is None:
        await update.message.reply_text(f"Supplement '{name}' not found.")
        return

    await db.log_supplement_taken(supplement["id"], profile["id"])

    await update.message.reply_text(
        f"{name} marked as taken for {profile['name']}."
    )


async def _supplement_remove(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
    args: list[str],
    text: str,
) -> None:
    if len(args) < 2:
        await update.message.reply_text("Usage: /supplement remove <name> [@profile]")
        return

    name = args[1]
    profile = await resolve_single_profile(owner_id, text)
    if profile is None:
        await update.message.reply_text("Profile not found.")
        return

    supplement = await db.get_supplement_by_name(profile["id"], owner_id, name)
    if supplement is None:
        await update.message.reply_text(f"Supplement '{name}' not found.")
        return

    removed = await db.remove_supplement(profile["id"], owner_id, name)
    if removed:
        # Remove scheduler job
        scheduler = context.bot_data.get("scheduler")
        if scheduler is not None:
            try:
                from bot.services.scheduler import remove_supplement_reminder

                remove_supplement_reminder(scheduler, supplement["id"])
            except Exception:
                logger.warning("Could not remove scheduler reminder", exc_info=True)

        await update.message.reply_text(
            f"Supplement '{name}' removed for {profile['name']}."
        )
    else:
        await update.message.reply_text(f"Supplement '{name}' not found.")


# ---------------------------------------------------------------------------
# Inline-button callback  (✅ Took it / 🔔 +1h / ❌ Skip)
# ---------------------------------------------------------------------------


async def supplement_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button presses on supplement reminder messages."""
    query = update.callback_query
    await query.answer()

    try:
        parts = query.data.split(":")
        action = parts[0]
        supplement_id = int(parts[1])
        profile_id = int(parts[2])
    except (IndexError, ValueError):
        logger.error("Invalid supplement callback data: %s", query.data)
        return

    owner_id = update.effective_user.id

    if action == "sd":
        await db.log_supplement_taken(supplement_id, profile_id)
        await query.edit_message_text("\u2705 Marked as taken!")

    elif action == "ss":
        supplement = await db.get_supplement_by_id(supplement_id)
        if supplement is None:
            await query.edit_message_text("Supplement not found.")
            return
        scheduler = context.bot_data.get("scheduler")
        if scheduler is not None:
            from bot.services.scheduler import schedule_snooze_reminder
            schedule_snooze_reminder(scheduler, context.bot, supplement, owner_id)
            await query.edit_message_text("\U0001f514 Will remind you in 1 hour.")
        else:
            await query.edit_message_text("Scheduler unavailable — please try again later.")

    elif action == "sx":
        await query.edit_message_text("\u274c Skipped.")


# ---------------------------------------------------------------------------
# Exported for startup
# ---------------------------------------------------------------------------


async def register_existing_reminders(scheduler, bot) -> None:
    """Load all active supplements from the DB and register scheduler jobs.

    Called once at bot startup.
    """
    from bot.services.scheduler import register_supplement_reminder

    supplements = await db.get_all_active_supplements()
    for sup in supplements:
        register_supplement_reminder(scheduler, bot, sup)
    logger.info("Registered %d supplement reminders from DB", len(supplements))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


COMMANDS: list[tuple[str, str]] = [
    ("supplement", "Manage supplements (add/list/today/done/remove)"),
]


def register(app) -> None:
    app.add_handler(CommandHandler("supplement", supplement_cmd))
    app.add_handler(CallbackQueryHandler(supplement_callback, pattern=r"^s[dsx]:"))
    app.add_handler(CallbackQueryHandler(supplement_today_callback, pattern=r"^st:"))
