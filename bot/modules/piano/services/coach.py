from __future__ import annotations

import logging

from bot.modules.piano.services import repertoire
from bot.services import db

logger = logging.getLogger(__name__)


def format_streak(
    current_streak: int,
    streak_minutes: int | None = None,
    freeze_until=None,
) -> str:
    if current_streak <= 0:
        return "No active streak — today is a good day to start."
    if current_streak == 1:
        base = "\U0001f525 Day 1 — nice start!"
    else:
        base = f"\U0001f525 Day {current_streak} in a row!"
    extras: list[str] = []
    if streak_minutes:
        extras.append(f"{streak_minutes} min this streak")
    if freeze_until:
        extras.append(f"\U0001f6e1️ freeze until {freeze_until}")
    return base + (f" — {', '.join(extras)}" if extras else "")


async def build_coach_context(owner_id: int) -> dict:
    """Collect the fresh context the lightweight model needs for a check-in."""
    streak = await db.get_piano_streak(owner_id)
    pieces = await db.list_piano_pieces(owner_id)
    sessions = await db.list_piano_sessions(owner_id, limit=3)

    session_summaries: list[str] = []
    for s in sessions:
        pieces_list = s.get("pieces_practiced") or []
        pieces_str = ", ".join(pieces_list) if pieces_list else "unspecified"
        duration = f"{s['duration_minutes']} min" if s.get("duration_minutes") else "duration n/a"
        session_summaries.append(
            f"{s['practiced_at']}: {duration} — {pieces_str}"
        )

    return {
        "current_streak": int(streak.get("current_streak") or 0),
        "longest_streak": int(streak.get("longest_streak") or 0),
        "pieces_in_progress": repertoire.summarize_in_progress(pieces),
        "all_pieces": pieces,
        "last_3_sessions": session_summaries,
    }


async def run_checkin(owner_id: int, user_note: str | None = None) -> str:
    """Run a single-call coaching check-in. Stateless; always reads fresh context."""
    from bot.services.agent_runner import load_agent, run_agent

    agent = load_agent("bot/modules/piano/agents/practice_coach.md")
    ctx = await build_coach_context(owner_id)

    user_prompt_parts = [
        f"Current streak: {ctx['current_streak']} days (longest: {ctx['longest_streak']}).",
        f"Pieces in progress: {ctx['pieces_in_progress']}.",
    ]
    if ctx["last_3_sessions"]:
        user_prompt_parts.append("Recent sessions:")
        user_prompt_parts.extend(f"- {line}" for line in ctx["last_3_sessions"])
    else:
        user_prompt_parts.append("No sessions logged yet.")
    if user_note:
        user_prompt_parts.append(f"User says: {user_note}")
    user_prompt_parts.append(
        "Respond with: (1) a one-line greeting that references the streak, "
        "(2) one short question acknowledging their practice today, "
        "(3) up to 3 bullet points suggesting what to focus on next."
    )

    logger.debug("piano run_checkin: streak=%d", ctx["current_streak"])

    return await run_agent(
        agent,
        [{"role": "user", "content": "\n".join(user_prompt_parts)}],
        temperature=0.7,
    )


async def summarize_log(
    owner_id: int,
    duration_minutes: int | None,
    pieces_practiced: list[str],
    notes: str | None,
) -> str:
    """Short post-log encouragement. One LLM call with the cheap model."""
    from bot.services.agent_runner import load_agent, run_agent

    agent = load_agent("bot/modules/piano/agents/practice_coach.md")
    ctx = await build_coach_context(owner_id)

    pieces_str = ", ".join(pieces_practiced) if pieces_practiced else "unspecified"
    duration_str = f"{duration_minutes} min" if duration_minutes else "unspecified duration"

    user_prompt = (
        f"The user just logged a piano practice session: {duration_str}, "
        f"pieces: {pieces_str}."
        + (f" Their note: {notes}" if notes else "")
        + f"\nCurrent streak: {ctx['current_streak']} days. "
        "Reply with 1-2 short sentences of encouragement and one concrete "
        "suggestion for their next session. Plain text only."
    )

    return await run_agent(
        agent,
        [{"role": "user", "content": user_prompt}],
        temperature=0.7,
    )
