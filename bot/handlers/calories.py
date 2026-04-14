from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.handlers._common import fmt_hhmm, handle_llm_error, short_text, strip_command
from bot.handlers.profiles import get_target_profiles
from bot.services import db
from bot.services.llm import (
    analyze_liquid,
    analyze_meal,
    analyze_recipe,
    compress_image,
)
from bot.utils.formatting import (
    format_liquid_logged,
    format_liquid_preview,
    format_meal_logged,
    format_meal_preview,
    format_recipe_preview,
    parse_servings,
    parse_time,
    strip_command_args,
)
from bot.utils.storage import save_meal_photo

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://\S+")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_RECIPE_FETCH_TIMEOUT = 15
_RECIPE_MAX_CHARS = 8000


# ---------------------------------------------------------------------------
# Preview rendering
# ---------------------------------------------------------------------------


async def _send_meal_preview(update: Update, pending: dict) -> None:
    result = pending["result"]
    await update.message.reply_text(
        format_meal_preview(
            description=result.get("description", pending["description"]),
            cals=result["calories"],
            protein=result["protein_g"],
            carbs=result["carbs_g"],
            fat=result["fat_g"],
            profile_names=[p["name"] for p in pending["profiles"]],
            eaten_at=pending["eaten_at"],
        )
    )


async def _send_liquid_preview(update: Update, pending: dict) -> None:
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


async def _send_recipe_preview(update: Update, pending: dict) -> None:
    result = pending["result"]
    await update.message.reply_text(
        format_recipe_preview(
            dish_name=result["dish_name"],
            per_serving=result["per_serving"],
            total=result["total"],
            servings=result["servings"],
            profile_names=[p["name"] for p in pending["profiles"]],
        )
    )


# ---------------------------------------------------------------------------
# Confirmed-entry logging (fans out over selected profiles)
# ---------------------------------------------------------------------------


async def _log_meal_for_profiles(
    update: Update,
    owner_id: int,
    profiles: list[dict],
    eaten_at: datetime,
    description: str,
    nutrition: dict,
    raw_llm: str,
    photo_path: str | None = None,
) -> None:
    reply_parts: list[str] = []
    for profile in profiles:
        await db.log_meal(
            profile_id=profile["id"],
            owner_id=owner_id,
            eaten_at=eaten_at,
            description=description,
            calories=nutrition["calories"],
            protein_g=nutrition["protein_g"],
            carbs_g=nutrition["carbs_g"],
            fat_g=nutrition["fat_g"],
            raw_llm=raw_llm,
            photo_path=photo_path,
        )
        totals = await db.get_daily_totals(profile["id"], owner_id)
        goal = await db.get_goal(profile["id"])
        reply_parts.append(
            format_meal_logged(
                profile_name=profile["name"],
                description=description,
                cals=nutrition["calories"],
                protein=nutrition["protein_g"],
                carbs=nutrition["carbs_g"],
                fat=nutrition["fat_g"],
                daily_total=totals,
                goal=goal,
            )
        )
    await update.message.reply_text("\n\n".join(reply_parts))


async def _log_liquid_for_profiles(
    update: Update,
    owner_id: int,
    profiles: list[dict],
    drunk_at: datetime,
    description: str,
    amount_ml: int,
    nutrition: dict,
    raw_llm: str,
) -> None:
    reply_parts: list[str] = []
    for profile in profiles:
        await db.log_liquid(
            profile_id=profile["id"],
            owner_id=owner_id,
            drunk_at=drunk_at,
            description=description,
            amount_ml=amount_ml,
            calories=nutrition["calories"],
            protein_g=nutrition["protein_g"],
            carbs_g=nutrition["carbs_g"],
            fat_g=nutrition["fat_g"],
            raw_llm=raw_llm,
        )
        totals = await db.get_daily_totals(profile["id"], owner_id)
        goal = await db.get_goal(profile["id"])
        hydration = await db.get_daily_hydration(profile["id"], owner_id)
        reply_parts.append(
            format_liquid_logged(
                profile_name=profile["name"],
                description=description,
                amount_ml=amount_ml,
                cals=nutrition["calories"],
                protein=nutrition["protein_g"],
                carbs=nutrition["carbs_g"],
                fat=nutrition["fat_g"],
                daily_total=totals,
                goal=goal,
                hydration_ml=hydration,
            )
        )
    await update.message.reply_text("\n\n".join(reply_parts))


# ---------------------------------------------------------------------------
# LLM analysis with user-facing error reporting
# ---------------------------------------------------------------------------


async def _analyse_or_reply(
    update: Update, analyser, *args, **kwargs,
) -> dict | None:
    """Run an analyser coroutine and translate failures into reply text.

    Returns the parsed result, or ``None`` when the user has already been told
    what went wrong (so the caller should stop).
    """
    try:
        return await analyser(*args, **kwargs)
    except Exception as exc:
        msg = handle_llm_error(exc)
        if msg is None:
            raise
        await update.message.reply_text(msg)
        return None


# ---------------------------------------------------------------------------
# /cal command  (text mode)
# ---------------------------------------------------------------------------


async def cal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    text = strip_command(update.message.text or "", "cal")

    if not text:
        await update.message.reply_text(
            "Usage: /cal <description> [@name] [at HH:MM]"
        )
        return

    eaten_at = parse_time(text) or datetime.now()
    description = strip_command_args(text)
    profiles = await get_target_profiles(owner_id, text)
    if not profiles:
        await update.message.reply_text("Profile not found.")
        return

    result = await _analyse_or_reply(update, analyze_meal, description)
    if result is None:
        return

    context.user_data["pending_meal"] = {
        "kind": "meal",
        "owner_id": owner_id,
        "description": description,
        "image_base64": None,
        "photo_path": None,
        "eaten_at": eaten_at,
        "profiles": profiles,
        "result": result,
    }
    await _send_meal_preview(update, context.user_data["pending_meal"])


# ---------------------------------------------------------------------------
# Photo handler  (photo with or without /cal caption)
# ---------------------------------------------------------------------------


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    caption = strip_command(update.message.caption or "", "cal")

    description = strip_command_args(caption) if caption else ""
    eaten_at = parse_time(caption) or datetime.now()
    profiles = await get_target_profiles(owner_id, caption)
    if not profiles:
        await update.message.reply_text("Profile not found.")
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    raw_bytes = bytes(await file.download_as_bytearray())
    compressed = compress_image(raw_bytes)
    image_b64 = base64.b64encode(compressed).decode()

    try:
        photo_path = save_meal_photo(compressed, owner_id)
    except Exception:
        logger.warning("Failed to save photo", exc_info=True)
        photo_path = None

    result = await _analyse_or_reply(
        update, analyze_meal, description, image_base64=image_b64,
    )
    if result is None:
        return

    context.user_data["pending_meal"] = {
        "kind": "meal",
        "owner_id": owner_id,
        "description": description,
        "image_base64": image_b64,
        "photo_path": photo_path,
        "eaten_at": eaten_at,
        "profiles": profiles,
        "result": result,
    }
    await _send_meal_preview(update, context.user_data["pending_meal"])


# ---------------------------------------------------------------------------
# /recipe command
# ---------------------------------------------------------------------------


async def _fetch_recipe_text(update: Update, url: str) -> str | None:
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=_RECIPE_FETCH_TIMEOUT,
        ) as client:
            resp = await client.get(url, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            html = resp.text
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.warning("Failed to fetch recipe URL %s: %s", url, exc)
        await update.message.reply_text(
            "Could not fetch the recipe URL. Please paste the recipe text instead."
        )
        return None

    recipe_text = BeautifulSoup(html, "html.parser").get_text(
        separator="\n", strip=True,
    )
    if not recipe_text.strip():
        await update.message.reply_text(
            "Could not extract text from the URL. Please paste the recipe text instead."
        )
        return None

    return recipe_text[:_RECIPE_MAX_CHARS]


async def recipe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    text = strip_command(update.message.text or "", "recipe")

    if not text:
        await update.message.reply_text(
            "Usage: /recipe <URL or paste recipe text> [for N]"
        )
        return

    profiles = await get_target_profiles(owner_id, text)
    if not profiles:
        await update.message.reply_text("Profile not found.")
        return

    servings = parse_servings(text)
    url_match = _URL_RE.search(text)

    if url_match:
        recipe_text = await _fetch_recipe_text(update, url_match.group(0))
        if recipe_text is None:
            return
    else:
        recipe_text = strip_command_args(text)

    result = await _analyse_or_reply(update, analyze_recipe, recipe_text, servings)
    if result is None:
        return

    context.user_data["pending_meal"] = {
        "kind": "recipe",
        "owner_id": owner_id,
        "description": recipe_text,
        "servings": servings,
        "profiles": profiles,
        "result": result,
    }
    await _send_recipe_preview(update, context.user_data["pending_meal"])


# ---------------------------------------------------------------------------
# /yes command  (confirm pending meal / recipe / liquid)
# ---------------------------------------------------------------------------


async def yes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = context.user_data.get("pending_meal")
    if not pending:
        await update.message.reply_text("Nothing to confirm.")
        return

    kind = pending["kind"]
    result = pending["result"]
    raw_llm = json.dumps(result)

    if kind == "meal":
        await _log_meal_for_profiles(
            update=update,
            owner_id=pending["owner_id"],
            profiles=pending["profiles"],
            eaten_at=pending["eaten_at"],
            description=result.get("description", pending["description"]),
            nutrition={
                "calories": result["calories"],
                "protein_g": result["protein_g"],
                "carbs_g": result["carbs_g"],
                "fat_g": result["fat_g"],
            },
            raw_llm=raw_llm,
            photo_path=pending.get("photo_path"),
        )
    elif kind == "recipe":
        per_serving = result["per_serving"]
        await _log_meal_for_profiles(
            update=update,
            owner_id=pending["owner_id"],
            profiles=pending["profiles"],
            eaten_at=datetime.now(),
            description=result["dish_name"],
            nutrition={
                "calories": per_serving["calories"],
                "protein_g": per_serving["protein_g"],
                "carbs_g": per_serving["carbs_g"],
                "fat_g": per_serving["fat_g"],
            },
            raw_llm=raw_llm,
        )
    elif kind == "liquid":
        await _log_liquid_for_profiles(
            update=update,
            owner_id=pending["owner_id"],
            profiles=pending["profiles"],
            drunk_at=pending["drunk_at"],
            description=result["description"],
            amount_ml=result["amount_ml"],
            nutrition={
                "calories": result["calories"],
                "protein_g": result["protein_g"],
                "carbs_g": result["carbs_g"],
                "fat_g": result["fat_g"],
            },
            raw_llm=raw_llm,
        )

    del context.user_data["pending_meal"]


# ---------------------------------------------------------------------------
# /today  (list today's meals & liquids with inline delete buttons)
# ---------------------------------------------------------------------------


_DELETE_BUTTONS_PER_ROW = 4


async def _build_today_view(
    profile: dict, owner_id: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the text + inline keyboard for one profile's /today list."""
    meals = await db.get_meals_today(profile["id"], owner_id)
    liquids = await db.get_liquids_today(profile["id"], owner_id)

    if not meals and not liquids:
        return (f"[{profile['name']}] No entries logged today.", None)

    entries_rows: list[tuple[str, str, int, str]] = []
    for m in meals:
        entries_rows.append((
            fmt_hhmm(m.get("eaten_at")),
            short_text(m.get("description") or ""),
            int(m.get("calories") or 0),
            f"delm:{m['id']}",
        ))
    for liquid in liquids:
        amount = int(liquid.get("amount_ml") or 0)
        desc = short_text(liquid.get("description") or "")
        label = f"{desc} ({amount} ml)" if amount else desc
        entries_rows.append((
            fmt_hhmm(liquid.get("drunk_at")),
            label,
            int(liquid.get("calories") or 0),
            f"dell:{liquid['id']}",
        ))
    entries_rows.sort(key=lambda e: e[0])

    totals = await db.get_daily_totals(profile["id"], owner_id)

    lines = [f"[{profile['name']}] Today"]
    buttons: list[InlineKeyboardButton] = []
    for idx, (ts, label, cals, cb_data) in enumerate(entries_rows, start=1):
        lines.append(f"{idx}. {ts}  {label}  \u2014  {cals} kcal")
        buttons.append(InlineKeyboardButton(f"\u274c {idx}", callback_data=cb_data))

    lines.append("")
    lines.append(
        f"Total: {int(totals.get('calories') or 0)} kcal | "
        f"P: {float(totals.get('protein_g') or 0):g}g | "
        f"C: {float(totals.get('carbs_g') or 0):g}g | "
        f"F: {float(totals.get('fat_g') or 0):g}g"
    )

    rows = [
        buttons[i : i + _DELETE_BUTTONS_PER_ROW]
        for i in range(0, len(buttons), _DELETE_BUTTONS_PER_ROW)
    ]
    return ("\n".join(lines), InlineKeyboardMarkup(rows) if rows else None)


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    text = update.message.text or ""

    profiles = await get_target_profiles(owner_id, text)
    if not profiles:
        await update.message.reply_text("Profile not found.")
        return

    for profile in profiles:
        body, keyboard = await _build_today_view(profile, owner_id)
        await update.message.reply_text(body, reply_markup=keyboard)


async def today_delete_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    owner_id = update.effective_user.id
    try:
        action, raw_id = query.data.split(":", 1)
        entry_id = int(raw_id)
    except (ValueError, AttributeError):
        logger.error("Invalid today-delete callback data: %s", query.data)
        return

    if action == "delm":
        row = await db.get_meal_by_id(entry_id, owner_id)
        delete = db.delete_meal
    elif action == "dell":
        row = await db.get_liquid_by_id(entry_id, owner_id)
        delete = db.delete_liquid
    else:
        return

    if row is None:
        await query.edit_message_text("Entry not found or already removed.")
        return

    await delete(entry_id, owner_id)
    removed_label = short_text(row.get("description") or action)

    # Re-render the list for the profile that owned the row.
    profile = next(
        (p for p in await db.list_profiles(owner_id) if p["id"] == row["profile_id"]),
        {"id": row["profile_id"], "name": ""},
    )
    body, keyboard = await _build_today_view(profile, owner_id)
    header = f"\u2705 Removed: {removed_label}\n\n"
    try:
        await query.edit_message_text(header + body, reply_markup=keyboard)
    except Exception:
        logger.debug("edit_message_text failed", exc_info=True)
        await query.message.reply_text(header + body, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Refinement handler  (plain text = remark on the pending meal)
# ---------------------------------------------------------------------------


async def refine_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Treat any plain text as a refinement when a pending meal exists.

    Silently no-ops when there's nothing pending -- users can chat freely
    until they start a /cal or /recipe flow.
    """
    from bot.handlers.piano import piano_text_dispatch

    if await piano_text_dispatch(update, context):
        return

    pending = context.user_data.get("pending_meal")
    if not pending:
        return

    remark = (update.message.text or "").strip()
    if not remark:
        return

    pending["description"] = (
        f"{pending['description']}\n\nRefinement from user: {remark}"
    )

    kind = pending["kind"]
    if kind == "meal":
        result = await _analyse_or_reply(
            update, analyze_meal, pending["description"],
            image_base64=pending.get("image_base64"),
        )
        if result is None:
            return
        pending["result"] = result
        await _send_meal_preview(update, pending)
    elif kind == "recipe":
        result = await _analyse_or_reply(
            update, analyze_recipe, pending["description"], pending.get("servings"),
        )
        if result is None:
            return
        pending["result"] = result
        await _send_recipe_preview(update, pending)
    elif kind == "liquid":
        result = await _analyse_or_reply(update, analyze_liquid, pending["description"])
        if result is None:
            return
        pending["result"] = result
        await _send_liquid_preview(update, pending)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


COMMANDS: list[tuple[str, str]] = [
    ("cal", "Log a meal (photo or text)"),
    ("recipe", "Log a recipe from URL or text"),
    ("yes", "Confirm and log the pending preview"),
    ("today", "List today's meals & drinks (with delete buttons)"),
]


def register(app) -> None:
    app.add_handler(CommandHandler("cal", cal_cmd))
    app.add_handler(CommandHandler("recipe", recipe_cmd))
    app.add_handler(CommandHandler("yes", yes_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CallbackQueryHandler(today_delete_callback, pattern=r"^del[ml]:"))
    # Photo handler must be registered after command handlers.
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    # Plain text is interpreted as a remark on the pending meal when one
    # exists. No-op otherwise.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, refine_handler))
