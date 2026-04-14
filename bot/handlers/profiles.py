from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.services import db
from bot.utils.formatting import format_profile_list, parse_target
from bot.utils.nutrition import calculate_bmr, calculate_tdee, calculate_macros

logger = logging.getLogger(__name__)

USAGE = (
    "Usage:\n"
    "/profile add <name>\n"
    "/profile list\n"
    "/profile switch <name>\n"
    "/profile delete <name>\n"
    "/profile set <height|weight|age|gender|activity> <value> [@name]\n"
    "\n"
    "Use /stats [@name] to see requirements."
)

SET_USAGE = (
    "Usage: /profile set <field> <value> [@name]\n"
    "Fields: height, weight, age, gender (male/female), activity (sedentary/light/moderate/active/very_active)"
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
        existing = await db.list_profiles(owner_id)
        existing_names = {p["name"] for p in existing}

        if name in existing_names:
            await update.message.reply_text(f"Profile '{name}' already exists.")
            return

        # Auto-create "Me" as default if this is the first profile
        # and the user isn't explicitly adding "Me" themselves.
        if not existing and name != "Me":
            me_id = await db.create_profile(owner_id, "Me")
            await db.set_active_profile(owner_id, me_id)

        profile_id = await db.create_profile(owner_id, name)

        # If the user's first profile is the one they named themselves,
        # make it active.
        if not existing:
            await db.set_active_profile(owner_id, profile_id)

        await update.message.reply_text(f"Profile '{name}' created.")

    elif sub == "list":
        profiles = await db.list_profiles(owner_id)
        active = await db.get_active_profile(owner_id)
        active_id = active["id"] if active else None
        text = format_profile_list(profiles, active_id) if profiles else "No profiles yet."
        await update.message.reply_text(text)

    elif sub == "switch" and len(args) >= 2:
        name = args[1]
        profile = await db.get_profile_by_name(owner_id, name)
        if profile is None:
            await update.message.reply_text(f"Profile '{name}' not found.")
            return
        await db.set_active_profile(owner_id, profile["id"])
        await update.message.reply_text(f"Switched to profile '{name}'.")

    elif sub == "delete" and len(args) >= 2:
        name = args[1]
        profiles = await db.list_profiles(owner_id)
        if len(profiles) <= 1:
            await update.message.reply_text("Cannot delete your last profile.")
            return
        deleted = await db.delete_profile(owner_id, name)
        if deleted:
            await update.message.reply_text(f"Profile '{name}' deleted.")
        else:
            await update.message.reply_text(f"Profile '{name}' not found.")

    elif sub == "set" and len(args) >= 3:
        field = args[1].lower()
        value = args[2]
        text = " ".join(args[2:]) # For @name parsing

        targets = await get_target_profiles(owner_id, text)
        if not targets:
            await update.message.reply_text("Profile not found.")
            return

        kwargs = {}
        try:
            if field in ("height", "height_cm"):
                kwargs["height_cm"] = float(value)
            elif field in ("weight", "weight_kg"):
                kwargs["weight_kg"] = float(value)
            elif field in ("age",):
                kwargs["age"] = int(value)
            elif field in ("gender",):
                val = value.lower()
                if val not in ("male", "female"):
                    raise ValueError("Gender must be 'male' or 'female'")
                kwargs["gender"] = val
            elif field in ("activity", "activity_level"):
                val = value.lower()
                valid = ("sedentary", "light", "moderate", "active", "very_active")
                if val not in valid:
                    raise ValueError(f"Activity must be one of: {', '.join(valid)}")
                kwargs["activity_level"] = val
            else:
                await update.message.reply_text(SET_USAGE)
                return
        except ValueError as e:
            await update.message.reply_text(f"Invalid value: {e}")
            return

        for p in targets:
            await db.update_profile(p["id"], owner_id, **kwargs)

        names = ", ".join(p["name"] for p in targets)
        await update.message.reply_text(f"Updated {field} for: {names}")

    else:
        await update.message.reply_text(USAGE)


# ---------------------------------------------------------------------------
# /stats command
# ---------------------------------------------------------------------------


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    text = " ".join(context.args) if context.args else ""
    
    targets = await get_target_profiles(owner_id, text)
    if not targets:
        await update.message.reply_text("Profile not found.")
        return

    for p in targets:
        name = p["name"]
        h = p.get("height_cm")
        w = p.get("weight_kg")
        age = p.get("age")
        gender = p.get("gender")
        activity = p.get("activity_level")

        if not all([h, w, age, gender, activity]):
            missing = []
            if not h: missing.append("height")
            if not w: missing.append("weight")
            if not age: missing.append("age")
            if not gender: missing.append("gender")
            if not activity: missing.append("activity")
            
            await update.message.reply_text(
                f"Profile '{name}' is missing: {', '.join(missing)}.\n"
                f"Set them using `/profile set <field> <value> @{name}`"
            )
            continue

        bmr = calculate_bmr(w, h, age, gender)
        tdee = calculate_tdee(bmr, activity)
        macros = calculate_macros(tdee, w)

        # Automatically save these as goals
        await db.set_goal(
            p["id"],
            int(tdee),
            protein_g=round(macros["protein_g"], 1),
            carbs_g=round(macros["carbs_g"], 1),
            fat_g=round(macros["fat_g"], 1),
        )

        resp = (
            f"📊 *Stats for {name}*\n"
            f"━━━━━━━━━━━━━━\n"
            f"BMR: {int(bmr)} kcal\n"
            f"TDEE: {int(tdee)} kcal (Maintenance)\n\n"
            f"*Daily Targets (Saved):*\n"
            f"Calories: {int(tdee)} kcal\n"
            f"Protein: {int(macros['protein_g'])}g\n"
            f"Carbs: {int(macros['carbs_g'])}g\n"
            f"Fat: {int(macros['fat_g'])}g"
        )
        await update.message.reply_text(resp, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Helper for other handlers
# ---------------------------------------------------------------------------


async def get_target_profiles(owner_id: int, text: str) -> list[dict]:
    """Resolve target profiles from ``@name`` / ``@both`` in *text*.

    Returns a list of profile dicts.
    """
    profile_name, is_both = parse_target(text)

    if is_both:
        return await db.get_all_profiles(owner_id)

    if profile_name is not None:
        profile = await db.get_profile_by_name(owner_id, profile_name)
        if profile is None:
            return []
        return [profile]

    active = await db.ensure_default_profile(owner_id)
    return [active]


async def resolve_single_profile(owner_id: int, text: str) -> dict | None:
    """Resolve a single target profile, or ``None`` if the name was unknown.

    Convenience wrapper for handlers that don't support ``@both`` — they just
    want the first (and only) profile the user asked for.
    """
    profiles = await get_target_profiles(owner_id, text)
    return profiles[0] if profiles else None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


COMMANDS: list[tuple[str, str]] = [
    ("profile", "Manage profiles (add/list/switch/delete/set)"),
    ("stats", "Show BMR, TDEE and macro targets"),
]


def register(app) -> None:
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
