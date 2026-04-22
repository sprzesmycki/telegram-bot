from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from bot.services import db

logger = logging.getLogger(__name__)

WARSAW = ZoneInfo("Europe/Warsaw")

_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_VALID_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_DAY_ALIASES: dict[str, str] = {
    "daily": "*",
    "weekdays": "mon,tue,wed,thu,fri",
    "weekends": "sat,sun",
}
_DAYS_DISPLAY: dict[str, str] = {
    "*": "daily",
    "mon,tue,wed,thu,fri": "weekdays",
    "sat,sun": "weekends",
}

USAGE = (
    "Usage:\n"
    "/remind add <HH:MM> [days] <message>          — recurring reminder\n"
    "/remind add once <HH:MM> <message>             — one-time (today or tomorrow)\n"
    "/remind add once tomorrow <HH:MM> <message>    — one-time tomorrow\n"
    "/remind add once YYYY-MM-DD <HH:MM> <message>  — one-time on specific date\n"
    "/remind list\n"
    "/remind remove <id>\n"
    "\n"
    "days: daily (default), weekdays, weekends, or e.g. mon,wed,fri"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_days_token(token: str) -> str | None:
    """Return APScheduler days_of_week string if *token* looks like a day spec."""
    lower = token.lower()
    if lower in _DAY_ALIASES:
        return _DAY_ALIASES[lower]
    parts = lower.split(",")
    if parts and all(p in _VALID_DAYS for p in parts):
        return lower
    return None


def _days_label(days: str) -> str:
    return _DAYS_DISPLAY.get(days, days)


def _build_remind_at(target_date: date, hour: int, minute: int) -> datetime:
    return datetime(target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=WARSAW)


# ---------------------------------------------------------------------------
# /remind dispatch
# ---------------------------------------------------------------------------


async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    args = context.args or []

    if not args:
        await update.message.reply_text(USAGE)
        return

    sub = args[0].lower()

    if sub == "add":
        await _remind_add(update, context, owner_id, args)
    elif sub == "list":
        await _remind_list(update, owner_id)
    elif sub == "remove":
        await _remind_remove(update, context, owner_id, args)
    else:
        await update.message.reply_text(USAGE)


# ---------------------------------------------------------------------------
# Subcommand: add
# ---------------------------------------------------------------------------


async def _remind_add(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
    args: list[str],
) -> None:
    if len(args) < 3:
        await update.message.reply_text(USAGE)
        return

    # One-time branch: /remind add once ...
    if args[1].lower() == "once":
        await _remind_add_once(update, context, owner_id, args[2:])
        return

    # Recurring branch: /remind add <HH:MM> [days] <message>
    reminder_time = args[1]
    if not _TIME_RE.match(reminder_time):
        await update.message.reply_text("Invalid time format. Use HH:MM (e.g. 09:00).")
        return

    if len(args) >= 4:
        days_candidate = _parse_days_token(args[2])
        if days_candidate is not None:
            days = days_candidate
            message = " ".join(args[3:])
        else:
            days = "*"
            message = " ".join(args[2:])
    else:
        days_candidate = _parse_days_token(args[2])
        if days_candidate is not None:
            await update.message.reply_text("Please provide a reminder message after the day spec.")
            return
        days = "*"
        message = args[2]

    if not message.strip():
        await update.message.reply_text("Reminder message cannot be empty.")
        return

    reminder_id = await db.add_reminder(
        owner_id, message, reminder_time, days_of_week=days, repeat=True,
    )
    _register(context, {
        "id": reminder_id,
        "owner_user_id": owner_id,
        "message": message,
        "reminder_time": reminder_time,
        "days_of_week": days,
        "repeat": True,
        "remind_at": None,
    })
    await update.message.reply_text(
        f"Reminder #{reminder_id} set: '{message}' at {reminder_time} ({_days_label(days)})."
    )


async def _remind_add_once(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
    rest: list[str],
) -> None:
    """Parse one-time reminder args (everything after 'once')."""
    # rest[0] is: YYYY-MM-DD | tomorrow | HH:MM
    now_w = datetime.now(WARSAW)
    today = now_w.date()

    if not rest:
        await update.message.reply_text(USAGE)
        return

    # Determine target_date and the index of the HH:MM token
    if rest[0].lower() == "tomorrow":
        target_date = today + timedelta(days=1)
        time_idx = 1
    elif _DATE_RE.match(rest[0]):
        try:
            target_date = date.fromisoformat(rest[0])
        except ValueError:
            await update.message.reply_text(f"Invalid date '{rest[0]}'. Use YYYY-MM-DD.")
            return
        time_idx = 1
    else:
        target_date = None  # decide after parsing the time
        time_idx = 0

    if time_idx >= len(rest):
        await update.message.reply_text(USAGE)
        return

    reminder_time_str = rest[time_idx]
    if not _TIME_RE.match(reminder_time_str):
        await update.message.reply_text("Invalid time format. Use HH:MM (e.g. 09:00).")
        return

    message = " ".join(rest[time_idx + 1:])
    if not message.strip():
        await update.message.reply_text("Reminder message cannot be empty.")
        return

    h, m = map(int, reminder_time_str.split(":"))

    if target_date is None:
        # No explicit date — fire today if time is still ahead, else tomorrow.
        candidate = _build_remind_at(today, h, m)
        target_date = today if candidate > now_w else today + timedelta(days=1)

    remind_at = _build_remind_at(target_date, h, m)

    if remind_at <= now_w:
        await update.message.reply_text(
            f"That time ({target_date} {reminder_time_str}) is already in the past."
        )
        return

    reminder_id = await db.add_reminder(
        owner_id, message, reminder_time_str,
        days_of_week="*", repeat=False, remind_at=remind_at,
    )
    _register(context, {
        "id": reminder_id,
        "owner_user_id": owner_id,
        "message": message,
        "reminder_time": reminder_time_str,
        "days_of_week": "*",
        "repeat": False,
        "remind_at": remind_at,
    })
    date_label = (
        "tomorrow" if target_date == today + timedelta(days=1)
        else ("today" if target_date == today else str(target_date))
    )
    await update.message.reply_text(
        f"One-time reminder #{reminder_id} set: '{message}' at {reminder_time_str} ({date_label})."
    )


def _register(context: ContextTypes.DEFAULT_TYPE, reminder: dict) -> None:
    scheduler = context.bot_data.get("scheduler")
    if scheduler is None:
        return
    try:
        from bot.services.scheduler import register_reminder_job
        register_reminder_job(scheduler, context.bot, reminder)
    except Exception:
        logger.warning("Could not register reminder job", exc_info=True)


# ---------------------------------------------------------------------------
# Subcommand: list
# ---------------------------------------------------------------------------


async def _remind_list(update: Update, owner_id: int) -> None:
    reminders = await db.list_reminders(owner_id)
    if not reminders:
        await update.message.reply_text("No reminders set. Use /remind add to create one.")
        return

    lines = ["Your reminders:"]
    for r in reminders:
        if not r.get("repeat", True) and r.get("remind_at"):
            ra: datetime = r["remind_at"]
            # asyncpg returns UTC-offset datetimes from TIMESTAMPTZ
            ra_w = ra.astimezone(WARSAW)
            when = f"once on {ra_w.strftime('%Y-%m-%d %H:%M')}"
        else:
            when = f"{r['reminder_time']} ({_days_label(r['days_of_week'])})"
        lines.append(f"  #{r['id']} \u2014 {when}: {r['message']}")
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Subcommand: remove
# ---------------------------------------------------------------------------


async def _remind_remove(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
    args: list[str],
) -> None:
    if len(args) < 2:
        await update.message.reply_text("Usage: /remind remove <id>")
        return

    try:
        reminder_id = int(args[1])
    except ValueError:
        await update.message.reply_text("Reminder ID must be a number.")
        return

    removed = await db.remove_reminder(owner_id, reminder_id)
    if not removed:
        await update.message.reply_text(f"Reminder #{reminder_id} not found.")
        return

    scheduler = context.bot_data.get("scheduler")
    if scheduler is not None:
        try:
            from bot.services.scheduler import remove_reminder_job
            remove_reminder_job(scheduler, reminder_id)
        except Exception:
            logger.warning("Could not remove reminder job", exc_info=True)

    await update.message.reply_text(f"Reminder #{reminder_id} removed.")


# ---------------------------------------------------------------------------
# Inline-button callback  (✅ Zrobione / 🔔 +1h / ❌ Pomiń)
# ---------------------------------------------------------------------------


async def reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    try:
        action, rid_str = query.data.split(":", 1)
        reminder_id = int(rid_str)
    except (ValueError, IndexError):
        logger.error("Invalid reminder callback data: %s", query.data)
        return

    owner_id = update.effective_user.id

    if action == "rd":
        # Done — just acknowledge; for one-time reminders the scheduler already
        # deactivated the DB row after firing, so nothing extra needed here.
        await query.edit_message_text("\u2705 Zrobione!")

    elif action == "rs":
        # Snooze +1h
        reminder = await db.get_reminder_by_id(owner_id, reminder_id)
        if reminder is None:
            # Might already be deactivated (one-time that fired)
            await query.edit_message_text("\U0001f514 Przypomnę za 1h.")
            # Schedule snooze using only message text from current message
            message_text = query.message.text or ""
            # Strip leading bell emoji prefix added by scheduler
            if message_text.startswith("\U0001f514 "):
                message_text = message_text[2:]
            scheduler = context.bot_data.get("scheduler")
            if scheduler is not None:
                from bot.services.scheduler import schedule_snooze_reminder
                schedule_snooze_reminder(
                    scheduler,
                    context.bot,
                    {"id": reminder_id, "message": message_text},
                    owner_id,
                )
            return

        scheduler = context.bot_data.get("scheduler")
        if scheduler is not None:
            from bot.services.scheduler import schedule_snooze_reminder
            schedule_snooze_reminder(scheduler, context.bot, reminder, owner_id)
            await query.edit_message_text("\U0001f514 Przypomnę za 1h.")
        else:
            await query.edit_message_text("Scheduler niedostępny — spróbuj ponownie.")

    elif action == "rx":
        await query.edit_message_text("\u274c Pominięto.")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


COMMANDS: list[tuple[str, str]] = [
    ("remind", "Manage reminders (add/list/remove)"),
]


def register(app) -> None:
    app.add_handler(CommandHandler("remind", remind_cmd))
    app.add_handler(CallbackQueryHandler(reminder_callback, pattern=r"^r[dsx]:"))
