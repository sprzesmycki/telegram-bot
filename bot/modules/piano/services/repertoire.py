from __future__ import annotations

import re

from bot.services import db

VALID_STATUSES = ("learning", "polishing", "mastered", "needs_review")

_STATUS_EMOJI = {
    "learning": "\U0001f4d6",       # 📖
    "polishing": "\U0001f527",      # 🔧
    "mastered": "\u2705",           # ✅
    "needs_review": "\U0001f504",   # 🔄
}


def status_emoji(status: str) -> str:
    return _STATUS_EMOJI.get(status, "\U0001f3b5")  # 🎵 fallback


_BY_RE = re.compile(r"\s+by\s+", re.IGNORECASE)


def parse_piece_title(text: str) -> tuple[str, str | None]:
    """Split ``"<title> by <composer>"`` into ``(title, composer)``.

    If no "by" separator is found, returns the whole string as the title and
    ``None`` as the composer.
    """
    cleaned = text.strip()
    if not cleaned:
        return ("", None)
    parts = _BY_RE.split(cleaned, maxsplit=1)
    if len(parts) == 2:
        title = parts[0].strip()
        composer = parts[1].strip() or None
        return (title, composer)
    return (cleaned, None)


def format_pieces_list(pieces: list[dict]) -> str:
    if not pieces:
        return "No pieces in your repertoire yet. Add one with /piano piece add <title>."

    by_status: dict[str, list[dict]] = {}
    for p in pieces:
        by_status.setdefault(p["status"], []).append(p)

    lines: list[str] = ["Your repertoire:"]
    for status in ("learning", "polishing", "needs_review", "mastered"):
        group = by_status.get(status, [])
        if not group:
            continue
        for piece in group:
            composer = f" — {piece['composer']}" if piece.get("composer") else ""
            lines.append(f"{status_emoji(status)} {piece['title']}{composer}")
    return "\n".join(lines)


def summarize_in_progress(pieces: list[dict]) -> str:
    """Short one-line summary of pieces currently being worked on."""
    in_progress = [p for p in pieces if p["status"] in ("learning", "polishing", "needs_review")]
    if not in_progress:
        return "no pieces in progress"
    titles = [p["title"] for p in in_progress[:5]]
    more = len(in_progress) - len(titles)
    joined = ", ".join(titles)
    return joined if more <= 0 else f"{joined} (+{more} more)"


async def find_piece_by_title(owner_id: int, title: str) -> dict | None:
    """Thin wrapper around the DB helper for handler readability."""
    return await db.find_piano_piece_by_title(owner_id, title)
