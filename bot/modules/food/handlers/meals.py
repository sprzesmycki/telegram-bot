from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
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

from bot.config import get_config
from bot.handlers._common import fmt_hhmm, handle_llm_error, short_text, strip_command
from bot.handlers.profiles import get_target_profiles
from bot.services import db
from bot.services.llm import (
    analyze_liquid,
    analyze_meal,
    analyze_recipe,
    compress_image,
    get_compare_models,
    get_llm_client,
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
_PENDING_TTL = 1800  # 30 minutes

# ---------------------------------------------------------------------------
# Bilingual step prompts (Polish / English)
# ---------------------------------------------------------------------------

_MSG_WHAT = "Co jadłeś/piłeś? / What did you eat or drink?"
_MSG_TYPE = "Posiłek czy napój? / Meal or drink?"
_MSG_AMOUNT = "Ile ml? / Amount (ml)?\n(Pomiń → /yes / Skip → /yes)"
_MSG_KCAL = "Kcal?\n(Pomiń → /yes / Skip → /yes)"
_MSG_PROTEIN = "Białko (g) / Protein (g)?\n(Pomiń → /yes / Skip → /yes)"
_MSG_CARBS = "Węglowodany (g) / Carbs (g)?\n(Pomiń → /yes / Skip → /yes)"
_MSG_FAT = "Tłuszcze (g) / Fat (g)?\n(Pomiń → /yes / Skip → /yes)"
_MSG_BAD_NUM = "Podaj liczbę. / Enter a number."

# ---------------------------------------------------------------------------
# Preview rendering (AI flow)
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
    raw_llm: str | None = None,
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
    raw_llm: str | None = None,
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
    try:
        return await analyser(*args, **kwargs)
    except Exception as exc:
        msg = handle_llm_error(exc)
        if msg is None:
            raise
        await update.message.reply_text(msg)
        return None


async def _analyse_or_reply_query(query, analyser, *args, **kwargs) -> dict | None:
    try:
        return await analyser(*args, **kwargs)
    except Exception as exc:
        msg = handle_llm_error(exc)
        if msg is None:
            raise
        await query.message.reply_text(msg)
        return None


# ---------------------------------------------------------------------------
# Multi-model comparison helpers
# ---------------------------------------------------------------------------


def _fmt_compare_meal(
    model: str,
    result: dict,
    profiles: list[dict],
    eaten_at: datetime,
) -> str:
    desc = result.get("description", "")
    target = ", ".join(p["name"] for p in profiles)
    return (
        f"[{model}]\n"
        f"{target} at {eaten_at.strftime('%H:%M')}\n"
        f"{desc}\n"
        f"{result['calories']} kcal | P: {result['protein_g']:g}g | C: {result['carbs_g']:g}g | F: {result['fat_g']:g}g"
    )


async def _send_compare_meal_previews(
    update: Update,
    pending: dict,
    compare_models: list,
) -> None:
    _, primary_model = get_llm_client()
    profiles = pending["profiles"]
    eaten_at = pending["eaten_at"]
    description = pending["description"]
    image_b64 = pending.get("image_base64")

    primary_block = _fmt_compare_meal(primary_model, pending["result"], profiles, eaten_at)
    await update.message.reply_text(
        primary_block + "\n\nReply /yes to log, or send a remark to refine."
    )

    tasks = [
        analyze_meal(
            description,
            image_base64=image_b64,
            model_override=model_id,
            client_override=client,
        )
        for (_, client, model_id) in compare_models
    ]
    cmp_results = await asyncio.gather(*tasks, return_exceptions=True)

    for (label, _, _), cmp_result in zip(compare_models, cmp_results):
        if isinstance(cmp_result, Exception):
            msg = handle_llm_error(cmp_result) or str(cmp_result)
            await update.message.reply_text(f"[{label}]\n{msg}")
        else:
            await update.message.reply_text(
                _fmt_compare_meal(label, cmp_result, profiles, eaten_at)
            )


# ---------------------------------------------------------------------------
# Type selector keyboard
# ---------------------------------------------------------------------------

_TYPE_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("🍽 Posiłek / Meal", callback_data="log_type:meal"),
    InlineKeyboardButton("🥤 Napój / Drink", callback_data="log_type:drink"),
]])


async def _ask_type(update: Update) -> None:
    await update.message.reply_text(_MSG_TYPE, reply_markup=_TYPE_KEYBOARD)


# ---------------------------------------------------------------------------
# Manual flow helpers
# ---------------------------------------------------------------------------


def _new_pending_log(
    owner_id: int,
    profiles: list[dict],
    eaten_at: datetime,
    description: str | None = None,
    photo_path: str | None = None,
) -> dict:
    return {
        "step": "type" if description else "description",
        "kind": None,
        "owner_id": owner_id,
        "profiles": profiles,
        "eaten_at": eaten_at,
        "description": description,
        "photo_path": photo_path,
        "calories": None,
        "protein_g": None,
        "carbs_g": None,
        "fat_g": None,
        "amount_ml": None,
        "_ts": time.time(),
    }


async def _handle_manual_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict,
) -> None:
    text = (update.message.text or "").strip()
    step = pending["step"]

    if step == "description":
        pending["description"] = text
        pending["step"] = "type"
        await _ask_type(update)
        return

    def _num(s: str) -> float | None:
        try:
            return float(s)
        except ValueError:
            return None

    if step == "amount_ml":
        val = _num(text)
        if val is None:
            await update.message.reply_text(_MSG_BAD_NUM)
            return
        pending["amount_ml"] = int(val)
        pending["step"] = "calories"
        await update.message.reply_text(_MSG_KCAL)

    elif step == "calories":
        val = _num(text)
        if val is None:
            await update.message.reply_text(_MSG_BAD_NUM)
            return
        pending["calories"] = val
        pending["step"] = "protein"
        await update.message.reply_text(_MSG_PROTEIN)

    elif step == "protein":
        val = _num(text)
        if val is None:
            await update.message.reply_text(_MSG_BAD_NUM)
            return
        pending["protein_g"] = val
        pending["step"] = "carbs"
        await update.message.reply_text(_MSG_CARBS)

    elif step == "carbs":
        val = _num(text)
        if val is None:
            await update.message.reply_text(_MSG_BAD_NUM)
            return
        pending["carbs_g"] = val
        pending["step"] = "fat"
        await update.message.reply_text(_MSG_FAT)

    elif step == "fat":
        val = _num(text)
        if val is None:
            await update.message.reply_text(_MSG_BAD_NUM)
            return
        pending["fat_g"] = val
        await _commit_manual(update, context)


async def _commit_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = context.user_data.pop("pending_log", None)
    if not pending:
        return

    nutrition = {
        "calories": pending.get("calories") or 0,
        "protein_g": pending.get("protein_g") or 0,
        "carbs_g": pending.get("carbs_g") or 0,
        "fat_g": pending.get("fat_g") or 0,
    }

    if pending["kind"] == "drink":
        await _log_liquid_for_profiles(
            update=update,
            owner_id=pending["owner_id"],
            profiles=pending["profiles"],
            drunk_at=pending["eaten_at"],
            description=pending["description"],
            amount_ml=pending.get("amount_ml") or 0,
            nutrition=nutrition,
        )
    else:
        await _log_meal_for_profiles(
            update=update,
            owner_id=pending["owner_id"],
            profiles=pending["profiles"],
            eaten_at=pending["eaten_at"],
            description=pending["description"],
            nutrition=nutrition,
            photo_path=pending.get("photo_path"),
        )


# ---------------------------------------------------------------------------
# log_type callback  (meal / drink button)
# ---------------------------------------------------------------------------


async def log_type_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    pending = context.user_data.get("pending_log")
    if not pending:
        await query.edit_message_text("Brak aktywnego wpisu. / No active entry.")
        return
    if time.time() - pending.get("_ts", 0) > _PENDING_TTL:
        del context.user_data["pending_log"]
        await query.edit_message_text(
            "Czas minął. / Entry expired — start again with /log."
        )
        return

    kind = query.data.split(":", 1)[1]  # "meal" or "drink"
    pending["kind"] = kind

    if get_config().modules.food.ai_analysis:
        description = pending.get("description") or ""
        image_b64 = pending.get("image_base64")
        eaten_at = pending["eaten_at"]
        profiles = pending["profiles"]
        owner_id = pending["owner_id"]
        photo_path = pending.get("photo_path")

        await query.edit_message_text("Analizuję… / Analysing…")

        if kind == "drink":
            result = await _analyse_or_reply_query(query, analyze_liquid, description)
            if result is None:
                del context.user_data["pending_log"]
                return
            context.user_data["pending_meal"] = {
                "kind": "liquid",
                "owner_id": owner_id,
                "description": description,
                "drunk_at": eaten_at,
                "profiles": profiles,
                "result": result,
                "_ts": time.time(),
            }
            await query.message.reply_text(
                format_liquid_preview(
                    description=result["description"],
                    amount_ml=result["amount_ml"],
                    cals=result["calories"],
                    protein=result["protein_g"],
                    carbs=result["carbs_g"],
                    fat=result["fat_g"],
                    profile_names=[p["name"] for p in profiles],
                    drunk_at=eaten_at,
                )
            )
        else:
            result = await _analyse_or_reply_query(
                query, analyze_meal, description, image_base64=image_b64,
            )
            if result is None:
                del context.user_data["pending_log"]
                return
            ai_pending = {
                "kind": "meal",
                "owner_id": owner_id,
                "description": description,
                "image_base64": image_b64,
                "photo_path": photo_path,
                "eaten_at": eaten_at,
                "profiles": profiles,
                "result": result,
                "_ts": time.time(),
            }
            context.user_data["pending_meal"] = ai_pending
            compare_models = get_compare_models()
            if compare_models:
                await _send_compare_meal_previews(query.message, ai_pending, compare_models)
            else:
                await query.message.reply_text(
                    format_meal_preview(
                        description=result.get("description", description),
                        cals=result["calories"],
                        protein=result["protein_g"],
                        carbs=result["carbs_g"],
                        fat=result["fat_g"],
                        profile_names=[p["name"] for p in profiles],
                        eaten_at=eaten_at,
                    )
                )
        del context.user_data["pending_log"]
    else:
        if kind == "drink":
            pending["step"] = "amount_ml"
            await query.edit_message_text(_MSG_AMOUNT)
        else:
            pending["step"] = "calories"
            await query.edit_message_text(_MSG_KCAL)


# ---------------------------------------------------------------------------
# /log command  (unified meal + drink entry point)
# ---------------------------------------------------------------------------


async def log_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    text = strip_command(update.message.text or "", "log")

    eaten_at = parse_time(text) or datetime.now()
    description = strip_command_args(text) or None
    profiles = await get_target_profiles(owner_id, text)
    if not profiles:
        await update.message.reply_text("Profile not found.")
        return

    context.user_data["pending_log"] = _new_pending_log(
        owner_id=owner_id,
        profiles=profiles,
        eaten_at=eaten_at,
        description=description,
    )

    if description:
        await _ask_type(update)
    else:
        await update.message.reply_text(_MSG_WHAT)


# ---------------------------------------------------------------------------
# Photo handler
# ---------------------------------------------------------------------------


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    caption = strip_command(update.message.caption or "", "log")

    eaten_at = parse_time(caption) or datetime.now()
    profiles = await get_target_profiles(owner_id, caption)
    if not profiles:
        await update.message.reply_text("Profile not found.")
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    raw_bytes = bytes(await file.download_as_bytearray())
    compressed = compress_image(raw_bytes)

    try:
        photo_path = save_meal_photo(compressed, owner_id)
    except Exception:
        logger.warning("Failed to save photo", exc_info=True)
        photo_path = None

    if get_config().modules.food.ai_analysis:
        image_b64 = base64.b64encode(compressed).decode()
        description = strip_command_args(caption) if caption else ""
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
            "_ts": time.time(),
        }
        compare_models = get_compare_models()
        if compare_models:
            await _send_compare_meal_previews(update, context.user_data["pending_meal"], compare_models)
        else:
            await _send_meal_preview(update, context.user_data["pending_meal"])
    else:
        context.user_data["pending_log"] = _new_pending_log(
            owner_id=owner_id,
            profiles=profiles,
            eaten_at=eaten_at,
            photo_path=photo_path,
        )
        await update.message.reply_text(
            "Zdjęcie zapisane. Co to było? / Photo saved. What was this?\n"
            "(Podaj opis / Enter a description)"
        )


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
    if not get_config().modules.food.ai_analysis:
        await update.message.reply_text(
            "Analiza przepisów wymaga AI. Włącz ai_analysis w config.yaml. / "
            "Recipe analysis requires AI. Enable ai_analysis in config.yaml."
        )
        return

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
        "_ts": time.time(),
    }
    await _send_recipe_preview(update, context.user_data["pending_meal"])


# ---------------------------------------------------------------------------
# /yes command  (confirm pending AI preview or manual entry)
# ---------------------------------------------------------------------------


async def yes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending_log = context.user_data.get("pending_log")
    if pending_log:
        step = pending_log.get("step")
        if step == "description":
            await update.message.reply_text(
                "Najpierw podaj opis. / Please provide a description first."
            )
            return
        if step == "type":
            await update.message.reply_text(
                "Wybierz posiłek lub napój. / Please select meal or drink."
            )
            return
        await _commit_manual(update, context)
        return

    pending = context.user_data.get("pending_meal")
    if not pending:
        await update.message.reply_text("Nothing to confirm.")
        return
    if time.time() - pending.get("_ts", 0) > _PENDING_TTL:
        del context.user_data["pending_meal"]
        await update.message.reply_text(
            "That preview expired (>30 min). Please re-send /log or /recipe."
        )
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
        lines.append(f"{idx}. {ts}  {label}  —  {cals} kcal")
        buttons.append(InlineKeyboardButton(f"❌ {idx}", callback_data=cb_data))

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


async def _send_today_full(
    update: Update, profile: dict, owner_id: int,
) -> None:
    meals = await db.get_meals_today(profile["id"], owner_id)
    for meal in meals:
        photo_path = meal.get("photo_path")
        if photo_path and os.path.exists(photo_path):
            caption = (
                f"{short_text(meal.get('description') or '', limit=200)} "
                f"— {int(meal.get('calories') or 0)} kcal"
            )
            try:
                with open(photo_path, "rb") as f:
                    await update.message.reply_photo(f, caption=caption[:1024])
            except Exception:
                logger.warning("Failed to send photo %s", photo_path, exc_info=True)

    body, keyboard = await _build_today_view(profile, owner_id)
    await update.message.reply_text(body, reply_markup=keyboard)


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    text = update.message.text or ""
    full_mode = "full" in (context.args or [])

    profiles = await get_target_profiles(owner_id, text)
    if not profiles:
        await update.message.reply_text("Profile not found.")
        return

    for profile in profiles:
        if full_mode:
            await _send_today_full(update, profile, owner_id)
        else:
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

    profile = next(
        (p for p in await db.list_profiles(owner_id) if p["id"] == row["profile_id"]),
        {"id": row["profile_id"], "name": ""},
    )
    body, keyboard = await _build_today_view(profile, owner_id)
    header = f"✅ Removed: {removed_label}\n\n"
    try:
        await query.edit_message_text(header + body, reply_markup=keyboard)
    except Exception:
        logger.debug("edit_message_text failed", exc_info=True)
        await query.message.reply_text(header + body, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# /cancel command
# ---------------------------------------------------------------------------


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cleared = []
    if context.user_data.pop("pending_meal", None) is not None:
        cleared.append("meal/recipe/liquid preview")
    if context.user_data.pop("pending_log", None) is not None:
        cleared.append("food log")
    if context.user_data.pop("pending_piano_log", None) is not None:
        context.user_data.pop("pending_piano_log_duration", None)
        context.user_data.pop("pending_piano_log_ts", None)
        cleared.append("piano log")
    if cleared:
        await update.message.reply_text(f"Cancelled: {', '.join(cleared)}.")
    else:
        await update.message.reply_text("Nothing active to cancel.")


# ---------------------------------------------------------------------------
# Refinement / plain-text dispatch
# ---------------------------------------------------------------------------


async def refine_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = get_config()
    if cfg.modules.piano.enabled:
        from bot.modules.piano.handlers.piano import piano_text_dispatch
        if await piano_text_dispatch(update, context):
            return

    # Manual flow: route text to current step handler
    pending_log = context.user_data.get("pending_log")
    if pending_log and pending_log.get("step") in (
        "description", "amount_ml", "calories", "protein", "carbs", "fat"
    ):
        if time.time() - pending_log.get("_ts", 0) > _PENDING_TTL:
            del context.user_data["pending_log"]
            return
        await _handle_manual_text(update, context, pending_log)
        return

    # AI refinement flow
    pending = context.user_data.get("pending_meal")
    if not pending:
        return
    if time.time() - pending.get("_ts", 0) > _PENDING_TTL:
        del context.user_data["pending_meal"]
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
    ("log", "Log a meal or drink (photo or text)"),
    ("recipe", "Log a recipe from URL or text"),
    ("yes", "Confirm and log the pending preview"),
    ("cancel", "Cancel the active pending flow"),
    ("today", "List today's meals & drinks (with delete buttons)"),
]


def register(app) -> None:
    app.add_handler(CommandHandler("log", log_cmd))
    app.add_handler(CommandHandler("recipe", recipe_cmd))
    app.add_handler(CommandHandler("yes", yes_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CallbackQueryHandler(today_delete_callback, pattern=r"^del[ml]:"))
    app.add_handler(CallbackQueryHandler(log_type_callback, pattern=r"^log_type:"))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, refine_handler))
