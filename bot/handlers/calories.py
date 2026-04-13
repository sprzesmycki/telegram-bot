from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime

import httpx
import openai
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.handlers.profiles import get_target_profiles
from bot.services import db_postgres, db_sqlite
from bot.services.llm import (
    LLMParseError,
    VisionNotSupportedError,
    analyze_meal,
    analyze_recipe,
    analyze_liquid,
    compress_image,
)
from bot.utils.formatting import (
    format_meal_logged,
    format_meal_preview,
    format_recipe_preview,
    format_liquid_preview,
    format_liquid_logged,
    parse_servings,
    parse_time,
    strip_command_args,
)
from bot.utils.storage import save_meal_photo

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://\S+")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _log_and_reply(
    update: Update,
    owner_id: int,
    profiles: list[dict],
    eaten_at: datetime,
    description: str,
    calories: int,
    protein_g: float,
    carbs_g: float,
    fat_g: float,
    raw_llm: str,
    photo_path: str | None = None,
) -> None:
    """Log a meal for each profile, mirror to PG, and send a reply."""
    reply_parts: list[str] = []

    for profile in profiles:
        profile_id = profile["id"]
        profile_name = profile["name"]

        meal_id = await db_sqlite.log_meal(
            profile_id=profile_id,
            owner_id=owner_id,
            eaten_at=eaten_at,
            description=description,
            calories=calories,
            protein_g=protein_g,
            carbs_g=carbs_g,
            fat_g=fat_g,
            raw_llm=raw_llm,
            photo_path=photo_path,
        )
        await db_postgres.mirror_log_meal(
            meal_id=meal_id,
            profile_id=profile_id,
            owner_id=owner_id,
            eaten_at=eaten_at,
            description=description,
            calories=calories,
            protein_g=protein_g,
            carbs_g=carbs_g,
            fat_g=fat_g,
            raw_llm=raw_llm,
            photo_path=photo_path,
        )

        totals = await db_sqlite.get_daily_totals(profile_id, owner_id)
        goal = await db_sqlite.get_goal(profile_id)

        reply_parts.append(
            format_meal_logged(
                profile_name=profile_name,
                description=description,
                cals=calories,
                protein=protein_g,
                carbs=carbs_g,
                fat=fat_g,
                daily_total=totals,
                goal=goal,
            )
        )

    await update.message.reply_text("\n\n".join(reply_parts))


async def _log_liquid_and_reply(
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


async def _send_meal_preview(update: Update, pending: dict) -> None:
    """Format and send the current pending_meal preview."""
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


async def _send_recipe_preview(update: Update, pending: dict) -> None:
    """Format and send the current pending recipe preview."""
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


def _handle_llm_error(exc: Exception) -> str | None:
    """Map LLM exceptions to user-facing error text. Returns None if not handled."""
    if isinstance(exc, VisionNotSupportedError):
        return (
            "The current model does not support image analysis. "
            "Please switch to a vision-capable model with /model, "
            "or describe your meal with /cal <description>."
        )
    if isinstance(exc, LLMParseError):
        return "Could not parse nutrition data. Please try rephrasing."
    if isinstance(exc, openai.NotFoundError):
        return (
            f"Model not found at provider: {exc}\n"
            "Check /model and switch to a valid model."
        )
    if isinstance(exc, openai.APIError):
        logger.error("LLM API error", exc_info=True)
        return f"LLM API error: {exc}"
    return None


# ---------------------------------------------------------------------------
# /cal command  (text mode)
# ---------------------------------------------------------------------------


async def cal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    text = (update.message.text or "")
    # Strip the /cal prefix -- take everything after "/cal "
    if text.lower().startswith("/cal"):
        text = text[4:].strip()

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

    try:
        result = await analyze_meal(description)
    except Exception as exc:
        msg = _handle_llm_error(exc)
        if msg is None:
            raise
        await update.message.reply_text(msg)
        return

    context.user_data["pending_meal"] = {
        "kind": "meal",
        "owner_id": owner_id,
        "description": description,  # composite LLM input; grows with refinements
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
    caption = update.message.caption or ""

    # Strip /cal prefix from caption if present
    if caption.lower().startswith("/cal"):
        caption = caption[4:].strip()

    description = strip_command_args(caption) if caption else ""
    eaten_at = parse_time(caption) or datetime.now()
    profiles = await get_target_profiles(owner_id, caption)

    if not profiles:
        await update.message.reply_text("Profile not found.")
        return

    # Download and compress the photo
    photo = update.message.photo[-1]  # largest resolution
    file = await photo.get_file()
    photo_bytes = await file.download_as_bytearray()
    compressed = compress_image(bytes(photo_bytes))
    image_b64 = base64.b64encode(compressed).decode()

    # Persist the compressed photo to disk for audit/review
    try:
        photo_path = save_meal_photo(compressed, owner_id)
    except Exception:
        logger.warning("Failed to save photo", exc_info=True)
        photo_path = None

    try:
        result = await analyze_meal(description, image_base64=image_b64)
    except Exception as exc:
        msg = _handle_llm_error(exc)
        if msg is None:
            raise
        await update.message.reply_text(msg)
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


async def recipe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    text = (update.message.text or "")
    if text.lower().startswith("/recipe"):
        text = text[7:].strip()

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
        # URL mode -- fetch the page and extract text
        url = url_match.group(0)
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
                resp = await client.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"
                        )
                    },
                )
                resp.raise_for_status()
                html = resp.text
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            logger.warning("Failed to fetch recipe URL %s: %s", url, exc)
            await update.message.reply_text(
                "Could not fetch the recipe URL. Please paste the recipe text instead."
            )
            return

        soup = BeautifulSoup(html, "html.parser")
        recipe_text = soup.get_text(separator="\n", strip=True)

        if not recipe_text.strip():
            await update.message.reply_text(
                "Could not extract text from the URL. Please paste the recipe text instead."
            )
            return

        # Truncate to 8000 chars to stay within LLM context limits
        recipe_text = recipe_text[:8000]
    else:
        # Paste mode -- use the text after /recipe as recipe content
        recipe_text = strip_command_args(text)

    try:
        result = await analyze_recipe(recipe_text, servings)
    except Exception as exc:
        msg = _handle_llm_error(exc)
        if msg is None:
            raise
        await update.message.reply_text(msg)
        return

    context.user_data["pending_meal"] = {
        "kind": "recipe",
        "owner_id": owner_id,
        "description": recipe_text,  # composite recipe text; grows with refinements
        "servings": servings,
        "profiles": profiles,
        "result": result,
    }

    await _send_recipe_preview(update, context.user_data["pending_meal"])


# ---------------------------------------------------------------------------
# /yes command  (confirm pending meal or recipe)
# ---------------------------------------------------------------------------


async def yes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = context.user_data.get("pending_meal")
    if not pending:
        await update.message.reply_text("Nothing to confirm.")
        return

    kind = pending["kind"]
    result = pending["result"]

    if kind == "meal":
        await _log_and_reply(
            update=update,
            owner_id=pending["owner_id"],
            profiles=pending["profiles"],
            eaten_at=pending["eaten_at"],
            description=result.get("description", pending["description"]),
            calories=result["calories"],
            protein_g=result["protein_g"],
            carbs_g=result["carbs_g"],
            fat_g=result["fat_g"],
            raw_llm=json.dumps(result),
            photo_path=pending.get("photo_path"),
        )
    elif kind == "recipe":
        await _log_and_reply(
            update=update,
            owner_id=pending["owner_id"],
            profiles=pending["profiles"],
            eaten_at=datetime.now(),
            description=result["dish_name"],
            calories=result["per_serving"]["calories"],
            protein_g=result["per_serving"]["protein_g"],
            carbs_g=result["per_serving"]["carbs_g"],
            fat_g=result["per_serving"]["fat_g"],
            raw_llm=json.dumps(result),
        )
    elif kind == "liquid":
        await _log_liquid_and_reply(
            update=update,
            owner_id=pending["owner_id"],
            profiles=pending["profiles"],
            drunk_at=pending["drunk_at"],
            description=result["description"],
            amount_ml=result["amount_ml"],
            calories=result["calories"],
            protein_g=result["protein_g"],
            carbs_g=result["carbs_g"],
            fat_g=result["fat_g"],
            raw_llm=json.dumps(result),
        )

    del context.user_data["pending_meal"]


# ---------------------------------------------------------------------------
# /today  (list today's meals & liquids with inline delete buttons)
# ---------------------------------------------------------------------------


def _fmt_hhmm(iso_ts: str | None) -> str:
    """Extract HH:MM from a stored timestamp; fall back to '?' on oddities."""
    if not iso_ts:
        return "?"
    try:
        return datetime.fromisoformat(iso_ts).strftime("%H:%M")
    except ValueError:
        return iso_ts[11:16] if len(iso_ts) >= 16 else iso_ts


def _short(text: str, limit: int = 60) -> str:
    cleaned = (text or "").replace("\n", " ").strip()
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "\u2026"


async def _build_today_view(
    profile: dict, owner_id: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the text + inline keyboard for one profile's /today list."""
    meals = await db_sqlite.get_meals_today(profile["id"], owner_id)
    liquids = await db_sqlite.get_liquids_today(profile["id"], owner_id)

    if not meals and not liquids:
        return (f"[{profile['name']}] No entries logged today.", None)

    entries: list[tuple[str, str, int, str]] = []
    for m in meals:
        entries.append((
            _fmt_hhmm(m.get("eaten_at")),
            _short(m.get("description") or ""),
            int(m.get("calories") or 0),
            f"delm:{m['id']}",
        ))
    for l in liquids:
        amount = int(l.get("amount_ml") or 0)
        desc = _short(l.get("description") or "")
        label = f"{desc} ({amount} ml)" if amount else desc
        entries.append((
            _fmt_hhmm(l.get("drunk_at")),
            label,
            int(l.get("calories") or 0),
            f"dell:{l['id']}",
        ))

    entries.sort(key=lambda e: e[0])

    totals = await db_sqlite.get_daily_totals(profile["id"], owner_id)

    lines = [f"[{profile['name']}] Today"]
    buttons: list[InlineKeyboardButton] = []
    for idx, (ts, label, cals, cb_data) in enumerate(entries, start=1):
        lines.append(f"{idx}. {ts}  {label}  —  {cals} kcal")
        buttons.append(InlineKeyboardButton(f"\u274c {idx}", callback_data=cb_data))

    lines.append("")
    lines.append(
        f"Total: {int(totals.get('calories') or 0)} kcal | "
        f"P: {float(totals.get('protein_g') or 0):g}g | "
        f"C: {float(totals.get('carbs_g') or 0):g}g | "
        f"F: {float(totals.get('fat_g') or 0):g}g"
    )

    rows = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
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

    profile: dict | None = None
    removed_label = ""

    if action == "delm":
        meal = await db_sqlite.get_meal_by_id(entry_id, owner_id)
        if meal is None:
            await query.edit_message_text("Entry not found or already removed.")
            return
        await db_sqlite.delete_meal(entry_id, owner_id)
        await db_postgres.mirror_delete_meal(entry_id, owner_id)
        profile = {"id": meal["profile_id"], "name": ""}
        removed_label = _short(meal.get("description") or "meal")
    elif action == "dell":
        liquid = await db_sqlite.get_liquid_by_id(entry_id, owner_id)
        if liquid is None:
            await query.edit_message_text("Entry not found or already removed.")
            return
        await db_sqlite.delete_liquid(entry_id, owner_id)
        await db_postgres.mirror_delete_liquid(entry_id, owner_id)
        profile = {"id": liquid["profile_id"], "name": ""}
        removed_label = _short(liquid.get("description") or "liquid")
    else:
        return

    # Resolve the proper profile name for the re-render header.
    all_profiles = await db_sqlite.list_profiles(owner_id)
    for p in all_profiles:
        if p["id"] == profile["id"]:
            profile = p
            break

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

    # Append the remark; the accumulated description is what we re-feed to
    # the LLM so successive refinements build on previous context.
    pending["description"] = (
        f"{pending['description']}\n\nRefinement from user: {remark}"
    )

    try:
        if pending["kind"] == "meal":
            result = await analyze_meal(
                pending["description"],
                image_base64=pending.get("image_base64"),
            )
            pending["result"] = result
            await _send_meal_preview(update, pending)
        elif pending["kind"] == "recipe":
            result = await analyze_recipe(
                pending["description"],
                pending.get("servings"),
            )
            pending["result"] = result
            await _send_recipe_preview(update, pending)
        elif pending["kind"] == "liquid":
            result = await analyze_liquid(pending["description"])
            pending["result"] = result
            await _send_liquid_preview(update, pending)
    except Exception as exc:
        msg = _handle_llm_error(exc)
        if msg is None:
            raise
        await update.message.reply_text(msg)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(app) -> None:
    app.add_handler(CommandHandler("cal", cal_cmd))
    app.add_handler(CommandHandler("recipe", recipe_cmd))
    app.add_handler(CommandHandler("yes", yes_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CallbackQueryHandler(today_delete_callback, pattern=r"^del[ml]:"))
    # Photo handler must be registered after command handlers
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    # Refinement handler -- plain text is interpreted as a remark on the
    # pending meal when one exists. No-ops otherwise.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, refine_handler))
