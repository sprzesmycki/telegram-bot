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

    Returns (client, model, provider).
    """
    if provider == "local":
        client = AsyncOpenAI(
            api_key=os.getenv("LOCAL_API_KEY", "ollama"),
            base_url=os.getenv("LOCAL_BASE_URL", "http://localhost:11434/v1"),
        )
        model = model_override or os.getenv("LOCAL_MODEL", "gemma3:27b")
    elif provider == "custom":
        client = AsyncOpenAI(
            api_key=os.getenv("CUSTOM_API_KEY"),
            base_url=os.getenv("CUSTOM_BASE_URL"),
        )
        model = model_override or os.getenv("CUSTOM_MODEL")
    else:  # openrouter
        provider = "openrouter"
        client = AsyncOpenAI(
            api_key=os.getenv("OPENROUTER_API_KEY"),
            base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        )
        model = model_override or os.getenv(
            "OPENROUTER_MODEL", "anthropic/claude-3-5-sonnet"
        )

    return client, model, provider


def init_llm() -> None:
    """Initialise the LLM provider from environment variables."""
    global _current_provider, _current_client, _current_model

    provider = os.getenv("LLM_PROVIDER", "openrouter")
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
    temperature: float = 0.3,
) -> dict:
    """Call the LLM, parse JSON, retry once if parsing fails.

    *label* is used purely for logs (e.g. "analyze_meal").
    *schema_hint* is appended to the retry prompt to remind the model which
    fields are required (e.g. "with both description_en and description_pl fields").
    *post_process* is an optional transformer applied to the parsed dict
    before returning (e.g. combining bilingual fields).
    Raises ``LLMParseError`` when parsing fails after one retry.
    """
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
    messages = [
        *messages,
        {"role": "assistant", "content": content},
        {"role": "user", "content": _RETRY_NUDGE.format(schema_hint=hint)},
    ]

    retry_response = await client.chat.completions.create(
        model=model, messages=messages, temperature=temperature,
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
# Meal analysis
# ---------------------------------------------------------------------------

_MEAL_SYSTEM = (
    "You are a nutrition assistant. Always return valid JSON only, no markdown, "
    "no prose, no code fences. "
    "Schema (ALL fields REQUIRED, no exceptions): "
    '{"calories": int, "protein_g": float, "carbs_g": float, "fat_g": float, '
    '"description_en": str, "description_pl": str}. '
    '"description_en" is a short English label for the dish. '
    '"description_pl" is the SAME dish translated to Polish. '
    "Both fields are mandatory — never omit either. "
    'Examples: {"description_en": "Scrambled eggs with toast", '
    '"description_pl": "Jajecznica z tostem"}. '
    "Always estimate numeric values, never refuse. "
    "IMPORTANT: If a hand or finger is visible in the photo, use it as a "
    "scale reference to estimate portion sizes more accurately (e.g., a "
    "fist is roughly 250ml/1 cup, a palm is ~100g of meat)."
)

_LIQUID_SYSTEM = (
    "You are a nutrition assistant specialized in liquids and hydration. "
    "Always return valid JSON only, no markdown, no prose, no code fences. "
    "Schema (ALL fields REQUIRED): "
    '{"amount_ml": int, "calories": int, "protein_g": float, "carbs_g": float, '
    '"fat_g": float, "description_en": str, "description_pl": str}. '
    '"description_en" is a short English label for the drink (e.g. "Black coffee"). '
    '"description_pl" is the SAME drink translated to Polish (e.g. "Czarna kawa"). '
    "Always estimate numeric values, never refuse. "
    "If the user doesn't specify an amount, assume a standard glass (250ml)."
)

_RECIPE_SYSTEM = (
    "Given a recipe, calculate total calories and macros for the whole dish, "
    "then divide by servings. Return valid JSON only, no markdown, no prose. "
    "Schema (ALL fields REQUIRED): "
    '{"total": {"calories": int, "protein_g": float, "carbs_g": float, "fat_g": float}, '
    '"per_serving": {"calories": int, "protein_g": float, "carbs_g": float, "fat_g": float}, '
    '"servings": int, "dish_name_en": str, "dish_name_pl": str}. '
    '"dish_name_en" is a short English name for the dish. '
    '"dish_name_pl" is the SAME dish translated to Polish. '
    "Both name fields are mandatory — never omit either. "
    'Example: {"dish_name_en": "Creamy tomato pasta", '
    '"dish_name_pl": "Makaron w sosie pomidorowym", ...}. '
    "Always estimate, never refuse."
)


def _combine_bilingual(result: dict, *, key: str, en: str, pl: str) -> dict:
    """Combine ``<en_field>`` + ``<pl_field>`` into a single ``<key>`` field.

    Used to collapse description_en/description_pl (meals, liquids) and
    dish_name_en/dish_name_pl (recipes) into ``"<en>\\n<pl>"`` for DB storage.
    Preserves the original split fields for callers that want them.
    """
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


async def analyze_meal(description: str, image_base64: str | None = None) -> dict:
    """Analyse a meal from text and/or a photo.

    Returns dict with keys: calories, protein_g, carbs_g, fat_g, description.
    Raises ``VisionNotSupportedError`` when the model cannot handle images.
    Raises ``LLMParseError`` when the model fails to return valid JSON.
    """
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
        {"role": "system", "content": _MEAL_SYSTEM},
        {"role": "user", "content": user_content},
    ]

    try:
        return await _call_and_parse_json(
            label="analyze_meal",
            messages=messages,
            schema_hint="with both description_en and description_pl fields",
            post_process=_combine_meal,
        )
    except openai.BadRequestError:
        if image_base64 is not None:
            _, model = get_llm_client()
            raise VisionNotSupportedError(
                f"The current model ({model}) does not support vision/image inputs."
            )
        raise


async def analyze_liquid(description: str) -> dict:
    """Analyse a drink from text.

    Returns dict with keys: amount_ml, calories, protein_g, carbs_g, fat_g, description.
    Raises ``LLMParseError`` when the model fails to return valid JSON.
    """
    messages = [
        {"role": "system", "content": _LIQUID_SYSTEM},
        {"role": "user", "content": description},
    ]
    return await _call_and_parse_json(
        label="analyze_liquid",
        messages=messages,
        schema_hint="with both description_en and description_pl fields",
        post_process=_combine_meal,
    )


async def analyze_recipe(recipe_text: str, servings: int | None = None) -> dict:
    """Analyse a recipe and return total / per-serving macros.

    Raises ``LLMParseError`` when the model fails to return valid JSON.
    """
    user_text = recipe_text
    if servings is not None:
        user_text += f"\n\nServings: {servings}"

    messages = [
        {"role": "system", "content": _RECIPE_SYSTEM},
        {"role": "user", "content": user_text},
    ]
    return await _call_and_parse_json(
        label="analyze_recipe",
        messages=messages,
        schema_hint="with both dish_name_en and dish_name_pl fields",
        post_process=_combine_recipe,
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
