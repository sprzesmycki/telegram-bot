"""Shared handler helpers.

Pure utilities used by every command handler. Anything that touches the DB or
needs Telegram state lives next to its feature; this module stays side-effect-free
so it can be imported from anywhere without risk of cycles.
"""
from __future__ import annotations

import logging
from datetime import datetime

import openai

from bot.services.llm import LLMParseError, VisionNotSupportedError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Command-prefix handling
# ---------------------------------------------------------------------------


def strip_command(text: str, command: str) -> str:
    """Remove a leading ``/command`` (case-insensitive) and return the rest.

    Safe to call on captions or plain strings; returns the original text if
    the command prefix isn't present. Leaves the `/` out of *command* —
    ``strip_command("/cal 100g rice", "cal")`` → ``"100g rice"``.
    """
    if not text:
        return ""
    prefix = f"/{command}"
    if text.lower().startswith(prefix):
        return text[len(prefix):].strip()
    return text.strip()


# ---------------------------------------------------------------------------
# LLM error mapping
# ---------------------------------------------------------------------------


def handle_llm_error(exc: Exception) -> str | None:
    """Map LLM exceptions to user-facing error text.

    Returns ``None`` when the exception isn't one we know how to translate —
    callers should re-raise in that case so the global error handler catches it.
    """
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
# Text helpers for /today and similar listings
# ---------------------------------------------------------------------------


def fmt_hhmm(iso_ts: str | None) -> str:
    """Extract HH:MM from a stored ISO timestamp; fall back to '?' on junk."""
    if not iso_ts:
        return "?"
    try:
        return datetime.fromisoformat(iso_ts).strftime("%H:%M")
    except ValueError:
        return iso_ts[11:16] if len(iso_ts) >= 16 else iso_ts


def short_text(text: str, limit: int = 60) -> str:
    """Collapse newlines and truncate with an ellipsis if longer than *limit*."""
    cleaned = (text or "").replace("\n", " ").strip()
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "\u2026"
