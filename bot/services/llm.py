from __future__ import annotations

import io
import json
import logging
import os
import re
from typing import Callable

import openai
from openai import AsyncOpenAI
from PIL import Image

from bot.config import get_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class LLMParseError(Exception):
    pass


class VisionNotSupportedError(Exception):
    pass


# ---------------------------------------------------------------------------
# Provider singleton
# ---------------------------------------------------------------------------

_current_provider: str | None = None
_current_client: AsyncOpenAI | None = None
_current_model: str | None = None


def _build_client(
    provider: str, model_override: str | None = None,
) -> tuple[AsyncOpenAI, str, str]:
    """Build an AsyncOpenAI client for the given provider.

    Returns (client, model, provider). API keys are always read from the
    environment (secrets); base URLs and default model names come from
    config.yaml (via get_config()), with env-var overrides honoured there.
    """
    cfg = get_config().llm

    if provider == "local":
        client = AsyncOpenAI(
            api_key=os.getenv("LOCAL_API_KEY", "ollama"),
            base_url=cfg.local.base_url,
        )
        model = model_override or cfg.local.model
    elif provider == "custom":
        client = AsyncOpenAI(
            api_key=os.getenv("CUSTOM_API_KEY"),
            base_url=cfg.custom.base_url or None,
        )
        model = model_override or cfg.custom.model
    else:  # openrouter
        provider = "openrouter"
        client = AsyncOpenAI(
            api_key=os.getenv("OPENROUTER_API_KEY"),
            base_url=cfg.openrouter.base_url,
        )
        model = model_override or cfg.openrouter.model

    return client, model, provider


def init_llm() -> None:
    """Initialise the LLM provider from config."""
    global _current_provider, _current_client, _current_model

    provider = get_config().llm.provider
    _current_client, _current_model, _current_provider = _build_client(provider)
    logger.debug(
        "LLM initialised: provider=%s model=%s",
        _current_provider, _current_model,
    )


def get_llm_client(model_override: str | None = None) -> tuple[AsyncOpenAI, str]:
    """Return the current (client, model) pair, initialising if needed.

    When *model_override* is provided, return the current client paired with
    that model name instead of the globally-active one. The client itself
    (base_url, api_key) is not swapped — only the model string passed on each
    request changes. This lets piano handlers pin their own model tier without
    mutating the global state touched by /model.
    """
    if _current_client is None:
        init_llm()
    model = model_override or _current_model
    return _current_client, model  # type: ignore[return-value]


def switch_provider(provider: str, model_override: str | None = None) -> None:
    """Rebuild the LLM client in-place for a different provider."""
    global _current_provider, _current_client, _current_model

    _current_client, _current_model, _current_provider = _build_client(
        provider, model_override,
    )
    logger.debug(
        "LLM switched: provider=%s model=%s",
        _current_provider, _current_model,
    )


def get_provider_info() -> dict:
    """Return metadata about the active provider."""
    client, model = get_llm_client()
    return {
        "provider": _current_provider,
        "model": model,
        "base_url": str(client.base_url),
    }


def get_compare_models() -> list[tuple[str, "AsyncOpenAI", str]]:
    """Parse compare_models config and return (label, client, model_id) triples.

    Each entry is either:
    - ``model_id``            — uses the current provider's client
    - ``model_id@provider``   — builds a dedicated client for *provider*

    Returns an empty list when the setting is unset or blank.
    """
    entries = get_config().llm.compare_models
    if not entries:
        return []

    result = []
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        if "@" in entry:
            model_id, _, provider = entry.rpartition("@")
            client, model, _ = _build_client(provider.strip(), model_id.strip())
        else:
            client, _ = get_llm_client()
            model = entry
        result.append((entry, client, model))
    return result


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)

_RETRY_NUDGE = (
    "Your previous response was not valid JSON. You MUST return valid "
    "JSON only{schema_hint}. No markdown fences, no explanation."
)


def _parse_json_response(content: str) -> dict:
    """Strip optional markdown fences and parse JSON."""
    match = _FENCE_RE.search(content)
    if match:
        content = match.group(1)
    return json.loads(content)


async def _call_and_parse_json(
    *,
    label: str,
    messages: list[dict],
    schema_hint: str = "",
    post_process: Callable[[dict], dict] | None = None,
    model_override: str | None = None,
    client_override: AsyncOpenAI | None = None,
    temperature: float = 0.3,
) -> dict:
    """Call the LLM, parse JSON, retry once if parsing fails.

    *label* is used purely for logs (e.g. "analyze_meal").
    *schema_hint* is appended to the retry prompt to remind the model which
    fields are required.
    *post_process* is an optional transformer applied to the parsed dict.
    When *client_override* is set it is used directly (paired with
    *model_override* as the model string); this lets compare-mode calls use
    a different provider without touching global state.
    Raises ``LLMParseError`` when parsing fails after one retry.
    """
    if client_override is not None:
        client, model = client_override, model_override
    else:
        client, model = get_llm_client(model_override=model_override)
    logger.debug("%s: model=%s", label, model)

    response = await client.chat.completions.create(
        model=model, messages=messages, temperature=temperature,
    )
    content = response.choices[0].message.content or ""
    logger.debug("%s raw response: %s", label, content)

    try:
        result = _parse_json_response(content)
        return post_process(result) if post_process else result
    except json.JSONDecodeError:
        logger.debug("%s: first JSON parse failed, retrying", label)

    hint = f" {schema_hint}" if schema_hint else ""
    retry_messages = [
        *messages,
        {"role": "assistant", "content": content},
        {"role": "user", "content": _RETRY_NUDGE.format(schema_hint=hint)},
    ]

    retry_response = await client.chat.completions.create(
        model=model, messages=retry_messages, temperature=temperature,
    )
    retry_content = retry_response.choices[0].message.content or ""
    logger.debug("%s retry response: %s", label, retry_content)

    try:
        result = _parse_json_response(retry_content)
        return post_process(result) if post_process else result
    except json.JSONDecodeError as exc:
        logger.error("%s: JSON parse failed after retry: %s", label, retry_content)
        raise LLMParseError(
            f"Failed to parse LLM response as JSON: {retry_content}"
        ) from exc


# ---------------------------------------------------------------------------
# Bilingual field combiners
# ---------------------------------------------------------------------------


def _combine_bilingual(result: dict, *, key: str, en: str, pl: str) -> dict:
    """Combine ``<en_field>`` + ``<pl_field>`` into a single ``<key>`` field."""
    en_val = (result.get(en) or "").strip()
    pl_val = (result.get(pl) or "").strip()
    if en_val and pl_val:
        result[key] = f"{en_val}\n{pl_val}"
    elif en_val or pl_val:
        result[key] = en_val or pl_val
    return result


def _combine_meal(result: dict) -> dict:
    return _combine_bilingual(result, key="description", en="description_en", pl="description_pl")


def _combine_recipe(result: dict) -> dict:
    return _combine_bilingual(result, key="dish_name", en="dish_name_en", pl="dish_name_pl")


# ---------------------------------------------------------------------------
# Meal analysis
# ---------------------------------------------------------------------------


async def analyze_meal(
    description: str,
    image_base64: str | None = None,
    *,
    model_override: str | None = None,
    client_override: AsyncOpenAI | None = None,
) -> dict:
    """Analyse a meal from text and/or a photo.

    Returns dict with keys: calories, protein_g, carbs_g, fat_g, description.
    Raises ``VisionNotSupportedError`` when the model cannot handle images.
    Raises ``LLMParseError`` when the model fails to return valid JSON.
    """
    from bot.services.agent_runner import load_agent
    agent = load_agent("bot/modules/calories/agents/meal_analyzer.md")

    if image_base64 is not None:
        hint = (description or "").strip()
        if hint and hint.lower() != "analyze this meal":
            text_prompt = (
                "Analyse the meal in the photo. The user has added a description "
                "of the ingredients or contents — treat it as authoritative for "
                "what's in the dish, especially for items that may be hidden "
                f"(layers, fillings, sauces): {hint}"
            )
        else:
            text_prompt = "Analyse the meal in the photo."
        user_content: str | list = [
            {"type": "text", "text": text_prompt},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
            },
        ]
    else:
        user_content = description

    messages = [
        {"role": "system", "content": agent.system_prompt},
        {"role": "user", "content": user_content},
    ]

    try:
        return await _call_and_parse_json(
            label="analyze_meal",
            messages=messages,
            schema_hint="with both description_en and description_pl fields",
            post_process=_combine_meal,
            model_override=model_override,
            client_override=client_override,
        )
    except openai.BadRequestError:
        if image_base64 is not None:
            model = model_override if client_override is not None else get_llm_client(model_override=model_override)[1]
            raise VisionNotSupportedError(
                f"The current model ({model}) does not support vision/image inputs."
            )
        raise


async def analyze_liquid(
    description: str,
    *,
    model_override: str | None = None,
    client_override: AsyncOpenAI | None = None,
) -> dict:
    """Analyse a drink from text.

    Returns dict with keys: amount_ml, calories, protein_g, carbs_g, fat_g, description.
    Raises ``LLMParseError`` when the model fails to return valid JSON.
    """
    from bot.services.agent_runner import load_agent
    agent = load_agent("bot/modules/calories/agents/liquid_analyzer.md")

    messages = [
        {"role": "system", "content": agent.system_prompt},
        {"role": "user", "content": description},
    ]
    return await _call_and_parse_json(
        label="analyze_liquid",
        messages=messages,
        schema_hint="with both description_en and description_pl fields",
        post_process=_combine_meal,
        model_override=model_override,
        client_override=client_override,
    )


async def analyze_recipe(
    recipe_text: str,
    servings: int | None = None,
    *,
    model_override: str | None = None,
    client_override: AsyncOpenAI | None = None,
) -> dict:
    """Analyse a recipe and return total / per-serving macros.

    Raises ``LLMParseError`` when the model fails to return valid JSON.
    """
    from bot.services.agent_runner import load_agent
    agent = load_agent("bot/modules/calories/agents/recipe_analyzer.md")

    user_text = recipe_text
    if servings is not None:
        user_text += f"\n\nServings: {servings}"

    messages = [
        {"role": "system", "content": agent.system_prompt},
        {"role": "user", "content": user_text},
    ]
    return await _call_and_parse_json(
        label="analyze_recipe",
        messages=messages,
        schema_hint="with both dish_name_en and dish_name_pl fields",
        post_process=_combine_recipe,
        model_override=model_override,
        client_override=client_override,
    )


# ---------------------------------------------------------------------------
# Daily nutrition review
# ---------------------------------------------------------------------------


def _format_review_payload(
    profile_name: str,
    review_date: str,
    meals: list[dict],
    liquids: list[dict],
    totals: dict,
    goal: dict,
    hydration_ml: int,
    supplements_scheduled: list[dict],
    supplements_taken_names: list[str],
) -> str:
    lines: list[str] = [
        f"Profile: {profile_name}",
        f"Date: {review_date}",
        "",
        "Meals:",
    ]
    if meals:
        for m in meals:
            eaten = m.get("eaten_at", "")
            t = str(eaten)[11:16] if len(str(eaten)) >= 16 else str(eaten)
            lines.append(
                f"- {t} {m.get('description', '')} \u2014 "
                f"{m.get('calories', 0)} kcal | "
                f"P {m.get('protein_g', 0):g}g | "
                f"C {m.get('carbs_g', 0):g}g | "
                f"F {m.get('fat_g', 0):g}g"
            )
    else:
        lines.append("- (none logged)")

    lines.append("")
    lines.append("Drinks:")
    if liquids:
        for liq in liquids:
            drunk = liq.get("drunk_at", "")
            t = str(drunk)[11:16] if len(str(drunk)) >= 16 else str(drunk)
            lines.append(
                f"- {t} {liq.get('description', '')} ({liq.get('amount_ml', 0)}ml) \u2014 "
                f"{liq.get('calories', 0)} kcal"
            )
    else:
        lines.append("- (none logged)")

    goal_cals = goal.get("daily_calories") or 0
    lines.extend([
        "",
        "Totals:",
        f"- Calories: {totals.get('calories', 0)} / {goal_cals} kcal",
        f"- Protein: {totals.get('protein_g', 0):g}g"
        + (f" / {goal['daily_protein_g']:g}g" if goal.get("daily_protein_g") else ""),
        f"- Carbs: {totals.get('carbs_g', 0):g}g"
        + (f" / {goal['daily_carbs_g']:g}g" if goal.get("daily_carbs_g") else ""),
        f"- Fat: {totals.get('fat_g', 0):g}g"
        + (f" / {goal['daily_fat_g']:g}g" if goal.get("daily_fat_g") else ""),
        f"- Hydration: {hydration_ml} ml",
    ])

    if supplements_scheduled:
        lines.append("")
        lines.append("Supplements:")
        taken = set(supplements_taken_names)
        for s in supplements_scheduled:
            status = "taken" if s["name"] in taken else "missed"
            lines.append(f"- {s['name']} @ {s['reminder_time']}: {status}")

    return "\n".join(lines)


async def review_day(
    profile_name: str,
    review_date: str,
    meals: list[dict],
    liquids: list[dict],
    totals: dict,
    goal: dict,
    hydration_ml: int,
    supplements_scheduled: list[dict] | None = None,
    supplements_taken_names: list[str] | None = None,
    *,
    model_override: str | None = None,
    client_override: AsyncOpenAI | None = None,
) -> str:
    """Generate a bilingual daily nutrition review via the active LLM.

    Returns the review text (plain, no JSON). Raised exceptions propagate so
    the caller can decide how to surface errors to the user.
    """
    from bot.services.agent_runner import load_agent, run_agent

    agent = load_agent("bot/modules/calories/agents/day_reviewer.md")

    payload = _format_review_payload(
        profile_name, review_date, meals, liquids, totals, goal, hydration_ml,
        supplements_scheduled or [], supplements_taken_names or [],
    )

    logger.debug("review_day: profile=%s date=%s", profile_name, review_date)

    if client_override is not None:
        # Compare-mode: caller supplies a specific client+model
        full_messages = [
            {"role": "system", "content": agent.system_prompt},
            {"role": "user", "content": payload},
        ]
        response = await client_override.chat.completions.create(
            model=model_override,
            messages=full_messages,
            temperature=0.5,
        )
        return (response.choices[0].message.content or "").strip()

    # Normal path: use agent runner (respects agent's model spec + global /model)
    return await run_agent(
        agent,
        [{"role": "user", "content": payload}],
        temperature=0.5,
    )


# ---------------------------------------------------------------------------
# Image compression
# ---------------------------------------------------------------------------


def compress_image(photo_bytes: bytes, max_size_kb: int = 512) -> bytes:
    """Compress and resize a photo for LLM vision input.

    - Resizes so the longest side is at most 1920 px.
    - JPEG-compresses with decreasing quality until the result is under
      *max_size_kb* (or quality reaches 20).

    Returns the compressed JPEG bytes.
    """
    img = Image.open(io.BytesIO(photo_bytes))
    img = img.convert("RGB")

    max_dim = 1920
    longest = max(img.size)
    if longest > max_dim:
        scale = max_dim / longest
        img = img.resize(
            (int(img.width * scale), int(img.height * scale)),
            Image.LANCZOS,
        )

    quality = 85
    while quality >= 20:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        if buf.tell() <= max_size_kb * 1024:
            return buf.getvalue()
        quality -= 10

    return buf.getvalue()
