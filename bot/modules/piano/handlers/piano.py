from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from bot.services import db
from bot.services.llm import LLMParseError
from bot.modules.piano.services import audio_agent, coach, repertoire, streaks
from bot.utils.storage import save_piano_recording

logger = logging.getLogger(__name__)

USAGE = (
    "\U0001f3b9 Piano practice coach\n"
    "Usage:\n"
    "/piano                              — show summary & streak\n"
    "/piano session start                — start a practice timer\n"
    "/piano session stop [pieces]        — stop timer and log\n"
    "/piano log [N] [piece1, piece2]     — log today's practice (N in mins)\n"
    "/piano checkin [note]               — coaching check-in\n"
    "/piano pieces                       — list your repertoire\n"
    "/piano piece add <title> [by <composer>]\n"
    "/piano piece status <title> <learning|polishing|mastered|needs_review>\n"
    "/piano piece note <title> <note text>\n"
    "/piano piece remove <title>\n"
    "/piano analyze [piece title]        — analyse last voice note\n"
    "/piano history [N]                  — recent sessions (default 7)\n"
    "/piano stats                        — totals"
)

_DURATION_RE = re.compile(
    r"(\d+)\s*(?:min(?:ute)?s?|m)\b",
    re.IGNORECASE,
)
_BARE_DURATION_RE = re.compile(r"^(\d+)(?:\s+|$)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_log_body(body: str) -> tuple[int | None, list[str], str | None]:
    """Extract (duration_minutes, pieces_practiced, notes) from a log body.

    Formats accepted:
      ``30 min Chopin Nocturne, scales``
      ``45 minutes``
      ``30 Chopin Nocturne`` (leading bare number as minutes)
      ``Chopin Nocturne`` (no duration)
    Anything after a ``--`` separator becomes notes.
    """
    text = (body or "").strip()
    if not text:
        return (None, [], None)

    notes: str | None = None
    if "--" in text:
        main, _, trailing = text.partition("--")
        text = main.strip()
        notes = trailing.strip() or None

    duration: int | None = None
    # 1. Try explicit duration (e.g. "30 min")
    match = _DURATION_RE.search(text)
    if match:
        try:
            duration = int(match.group(1))
        except ValueError:
            duration = None
        text = (text[: match.start()] + text[match.end():]).strip(" ,.-\t")
    else:
        # 2. Try bare number at the very start
        match = _BARE_DURATION_RE.search(text)
        if match:
            try:
                duration = int(match.group(1))
            except ValueError:
                duration = None
            text = text[match.end():].strip(" ,.-\t")

    pieces: list[str] = []
    if text:
        pieces = [p.strip() for p in text.split(",") if p.strip()]

    return (duration, pieces, notes)


def _strip_subcommand(text: str, subcommand_tokens: int) -> str:
    """Strip ``/piano <sub1> [<sub2>...]`` from *text* and return remainder."""
    remainder = text
    for _ in range(subcommand_tokens):
        remainder = remainder.strip()
        if not remainder:
            break
        parts = remainder.split(maxsplit=1)
        remainder = parts[1] if len(parts) == 2 else ""
    return remainder.strip()


# ---------------------------------------------------------------------------
# Top-level command
# ---------------------------------------------------------------------------


async def piano_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    args = context.args or []
    text = update.message.text or ""

    if not args:
        await _piano_summary(update, owner_id)
        return

    sub = args[0].lower()

    if sub == "log":
        await _piano_log(update, context, owner_id, args, text)
    elif sub == "session":
        await _piano_session_router(update, context, owner_id, args, text)
    elif sub == "checkin":
        await _piano_checkin(update, owner_id, args, text)
    elif sub == "pieces":
        await _piano_pieces_list(update, owner_id)
    elif sub == "piece":
        await _piano_piece_router(update, owner_id, args, text)
    elif sub == "analyze":
        await _piano_analyze(update, context, owner_id, args, text)
    elif sub == "history":
        await _piano_history(update, owner_id, args)
    elif sub == "stats":
        await _piano_stats(update, owner_id)
    else:
        await update.message.reply_text(USAGE)


# ---------------------------------------------------------------------------
# /piano session …
# ---------------------------------------------------------------------------


async def _piano_session_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
    args: list[str],
    text: str,
) -> None:
    if len(args) < 2:
        await update.message.reply_text(
            "Usage:\n"
            "/piano session start\n"
            "/piano session stop [pieces practiced]"
        )
        return

    action = args[1].lower()
    if action == "start":
        await _piano_session_start(update, owner_id)
    elif action == "stop":
        await _piano_session_stop(update, context, owner_id, text)
    else:
        await update.message.reply_text(f"Unknown session action: {action}")


async def _piano_session_start(update: Update, owner_id: int) -> None:
    started_at = await db.start_piano_session(owner_id)
    time_str = started_at.strftime("%H:%M")
    await update.message.reply_text(
        f"\u23f1\ufe0f Piano session started at {time_str}. "
        "Go practice! Use `/piano session stop` when you're done."
    )


async def _piano_session_stop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
    text: str,
) -> None:
    started_at = await db.get_active_piano_session(owner_id)
    if not started_at:
        await update.message.reply_text(
            "No active session found. Use `/piano session start` first."
        )
        return

    now = datetime.now(db.WARSAW)
    duration_minutes = round((now - started_at).total_seconds() / 60)
    await db.clear_active_piano_session(owner_id)

    body = _strip_subcommand(text, 3)  # strip "/piano session stop"
    if not body:
        # No pieces specified, ask for them.
        context.user_data["pending_piano_log_duration"] = duration_minutes
        context.user_data["pending_piano_log"] = True
        await update.message.reply_text(
            f"\u23f1\ufe0f Session stopped. Duration: {duration_minutes} min.\n"
            "What pieces did you practice? (Reply with titles, or `none` if just exercises)"
        )
        return

    # Pieces specified in the stop command
    await _ingest_log(update, owner_id, f"{duration_minutes} min {body}")


# ---------------------------------------------------------------------------
# Summary (no args)
# ---------------------------------------------------------------------------


async def _piano_summary(update: Update, owner_id: int) -> None:
    streak = await db.get_piano_streak(owner_id)
    pieces = await db.list_piano_pieces(owner_id)
    sessions = await db.list_piano_sessions(owner_id, limit=1)
    active_start = await db.get_active_piano_session(owner_id)

    lines: list[str] = []
    current = int(streak.get("current_streak") or 0)
    lines.append(coach.format_streak(current))

    in_progress = repertoire.summarize_in_progress(pieces)
    lines.append(f"In progress: {in_progress}")

    if active_start:
        now = datetime.now(db.WARSAW)
        elapsed = round((now - active_start).total_seconds() / 60)
        lines.append(f"\u23f1\ufe0f Active session: {elapsed} min (started {active_start.strftime('%H:%M')})")

    if sessions:
        last = sessions[0]
        pieces_str = ", ".join(last["pieces_practiced"]) or "unspecified"
        duration = (
            f"{last['duration_minutes']} min"
            if last.get("duration_minutes") else "duration n/a"
        )
        lines.append(f"Last session: {last['practiced_at']} — {duration} ({pieces_str})")
    else:
        lines.append("No sessions logged yet. Use /piano log to start.")

    lines.append("")
    lines.append("Next steps:")
    lines.append("  \u2022 /piano log <N min> <pieces>")
    lines.append("  \u2022 /piano checkin")
    lines.append("  \u2022 Send a voice note then /piano analyze")

    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# /piano log
# ---------------------------------------------------------------------------


async def _piano_log(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
    args: list[str],
    text: str,
) -> None:
    body = _strip_subcommand(text, 2)  # strip "/piano log"
    if not body:
        context.user_data["pending_piano_log"] = True
        await update.message.reply_text(
            "How long did you practice and what pieces? "
            "Reply like `30 min Chopin Nocturne, scales`."
        )
        return

    await _ingest_log(update, owner_id, body)


async def _ingest_log(
    update: Update,
    owner_id: int,
    body: str,
    duration_override: int | None = None,
) -> None:
    duration, pieces_practiced, notes = _parse_log_body(body)
    if duration is None:
        duration = duration_override

    if duration is None and not pieces_practiced:
        await update.message.reply_text(
            "Couldn't parse that. Try: `30 min Chopin, scales`."
        )
        return

    practiced_at = date.today()

    await db.log_piano_session(
        owner_id=owner_id,
        practiced_at=practiced_at,
        duration_minutes=duration,
        notes=notes,
        pieces_practiced=pieces_practiced,
    )

    for title in pieces_practiced:
        piece = await repertoire.find_piece_by_title(owner_id, title)
        if piece:
            await db.touch_piano_piece_last_practiced(
                owner_id, piece["id"], practiced_at,
            )

    streak = await streaks.compute_and_update_streak(owner_id, practiced_at)

    try:
        reply = await coach.summarize_log(owner_id, duration, pieces_practiced, notes)
    except Exception as exc:
        logger.warning("piano summarize_log failed: %s", exc)
        reply = "Nice session — keep it going."

    header = coach.format_streak(int(streak.get("current_streak") or 0))
    await update.message.reply_text(f"{header}\n\n{reply}")


# ---------------------------------------------------------------------------
# /piano checkin
# ---------------------------------------------------------------------------


async def _piano_checkin(
    update: Update, owner_id: int, args: list[str], text: str,
) -> None:
    note = _strip_subcommand(text, 2) or None  # strip "/piano checkin"
    try:
        reply = await coach.run_checkin(owner_id, user_note=note)
    except Exception as exc:
        logger.error("piano run_checkin failed", exc_info=True)
        await update.message.reply_text(f"Check-in failed: {exc}")
        return
    await update.message.reply_text(reply)


# ---------------------------------------------------------------------------
# /piano pieces + /piano piece …
# ---------------------------------------------------------------------------


async def _piano_pieces_list(update: Update, owner_id: int) -> None:
    pieces = await db.list_piano_pieces(owner_id)
    await update.message.reply_text(repertoire.format_pieces_list(pieces))


async def _piano_piece_router(
    update: Update, owner_id: int, args: list[str], text: str,
) -> None:
    if len(args) < 2:
        await update.message.reply_text(
            "Usage:\n"
            "/piano piece add <title> [by <composer>]\n"
            "/piano piece status <title> <status>\n"
            "/piano piece note <title> <note>\n"
            "/piano piece remove <title>"
        )
        return

    action = args[1].lower()
    body = _strip_subcommand(text, 3)  # strip "/piano piece <action>"

    if action == "add":
        await _piano_piece_add(update, owner_id, body)
    elif action == "status":
        await _piano_piece_status(update, owner_id, body)
    elif action == "note":
        await _piano_piece_note(update, owner_id, body)
    elif action == "remove":
        await _piano_piece_remove(update, owner_id, body)
    else:
        await update.message.reply_text(f"Unknown piece action: {action}")


async def _piano_piece_add(update: Update, owner_id: int, body: str) -> None:
    if not body:
        await update.message.reply_text(
            "Usage: /piano piece add <title> [by <composer>]"
        )
        return
    title, composer = repertoire.parse_piece_title(body)
    if not title:
        await update.message.reply_text("Title required.")
        return
    existing = await repertoire.find_piece_by_title(owner_id, title)
    if existing:
        await update.message.reply_text(
            f"Piece '{existing['title']}' already in your repertoire."
        )
        return
    await db.add_piano_piece(owner_id, title, composer)
    composer_str = f" by {composer}" if composer else ""
    await update.message.reply_text(
        f"\U0001f4d6 Added '{title}'{composer_str} to your repertoire."
    )


async def _piano_piece_status(update: Update, owner_id: int, body: str) -> None:
    if not body:
        await update.message.reply_text(
            "Usage: /piano piece status <title> <learning|polishing|mastered|needs_review>"
        )
        return
    tokens = body.rsplit(maxsplit=1)
    if len(tokens) != 2:
        await update.message.reply_text(
            "Usage: /piano piece status <title> <status>"
        )
        return
    title_part, new_status = tokens[0].strip(), tokens[1].strip().lower()
    if new_status not in repertoire.VALID_STATUSES:
        await update.message.reply_text(
            f"Status must be one of: {', '.join(repertoire.VALID_STATUSES)}"
        )
        return
    piece = await repertoire.find_piece_by_title(owner_id, title_part)
    if piece is None:
        await update.message.reply_text(f"Piece '{title_part}' not found.")
        return
    await db.update_piano_piece_status(owner_id, piece["id"], new_status)
    emoji = repertoire.status_emoji(new_status)
    await update.message.reply_text(
        f"{emoji} '{piece['title']}' → {new_status}"
    )


async def _piano_piece_note(update: Update, owner_id: int, body: str) -> None:
    if not body:
        await update.message.reply_text("Usage: /piano piece note <title> <note text>")
        return

    piece, note = await _match_piece_prefix(owner_id, body)
    if piece is None:
        await update.message.reply_text(
            "Could not match a piece from your repertoire at the start of that input. "
            "Add the piece first with /piano piece add."
        )
        return
    if not note:
        await update.message.reply_text("Note text required after the piece title.")
        return

    await db.update_piano_piece_note(owner_id, piece["id"], note)
    await update.message.reply_text(f"Note saved for '{piece['title']}'.")


async def _piano_piece_remove(update: Update, owner_id: int, body: str) -> None:
    if not body:
        await update.message.reply_text("Usage: /piano piece remove <title>")
        return
    piece = await repertoire.find_piece_by_title(owner_id, body)
    if piece is None:
        await update.message.reply_text(f"Piece '{body}' not found.")
        return
    ok = await db.remove_piano_piece(owner_id, piece["id"])
    if ok:
        await update.message.reply_text(f"Removed '{piece['title']}'.")
    else:
        await update.message.reply_text(f"Could not remove '{body}'.")


async def _match_piece_prefix(
    owner_id: int, body: str,
) -> tuple[dict | None, str]:
    """Best-effort match: find the longest piece title that is a prefix of *body*.

    Returns (piece, remaining_text_after_title). Falls back to a whole-body
    lookup if no prefix matches.
    """
    pieces = await db.list_piano_pieces(owner_id)
    if not pieces:
        return (None, body.strip())

    body_lower = body.lower().strip()
    candidates = sorted(pieces, key=lambda p: len(p["title"]), reverse=True)
    for piece in candidates:
        title_lower = piece["title"].lower()
        if body_lower.startswith(title_lower):
            remainder = body.strip()[len(piece["title"]):].lstrip(" ,:-\t")
            return (piece, remainder)

    piece = await repertoire.find_piece_by_title(owner_id, body.strip())
    return (piece, "") if piece else (None, body.strip())


# ---------------------------------------------------------------------------
# /piano history
# ---------------------------------------------------------------------------


async def _piano_history(update: Update, owner_id: int, args: list[str]) -> None:
    limit = 7
    if len(args) >= 2:
        try:
            limit = max(1, min(int(args[1]), 50))
        except ValueError:
            pass
    sessions = await db.list_piano_sessions(owner_id, limit=limit)
    if not sessions:
        await update.message.reply_text("No sessions logged yet.")
        return
    lines = [f"Last {len(sessions)} session(s):"]
    for s in sessions:
        pieces_str = ", ".join(s["pieces_practiced"]) or "unspecified"
        duration = (
            f"{s['duration_minutes']} min"
            if s.get("duration_minutes") else "duration n/a"
        )
        lines.append(f"  \u2022 {s['practiced_at']}: {duration} — {pieces_str}")
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# /piano stats
# ---------------------------------------------------------------------------


async def _piano_stats(update: Update, owner_id: int) -> None:
    totals = await db.piano_total_stats(owner_id)
    streak = await db.get_piano_streak(owner_id)
    top = await db.most_practiced_piece(owner_id)
    pieces = await db.list_piano_pieces(owner_id)

    total_sessions = int(totals.get("total_sessions") or 0)
    total_minutes = int(totals.get("total_minutes") or 0)
    hours = total_minutes // 60
    minutes = total_minutes % 60

    lines = [
        "\U0001f3b9 Practice stats",
        f"Sessions: {total_sessions}",
        f"Total time: {hours}h {minutes}m",
        f"Current streak: {int(streak.get('current_streak') or 0)} days "
        f"(longest: {int(streak.get('longest_streak') or 0)})",
        f"Pieces in repertoire: {len(pieces)}",
    ]
    if top:
        composer_str = f" ({top['composer']})" if top.get("composer") else ""
        lines.append(
            f"Most practiced: {top['title']}{composer_str} — {top['count']} session(s)"
        )
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Voice / audio handling
# ---------------------------------------------------------------------------


def _resolve_audio_attachment(message) -> tuple[str | None, int | None, str, str] | None:
    """Pick the audio attachment off a message.

    Returns ``(file_id, duration, kind, extension)`` or ``None``.
    """
    if message is None:
        return None
    voice = getattr(message, "voice", None)
    if voice:
        return (voice.file_id, voice.duration, "voice", "ogg")
    audio = getattr(message, "audio", None)
    if audio:
        filename = (audio.file_name or "").lower()
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "mp3"
        return (audio.file_id, audio.duration, "audio", ext)
    return None


async def piano_voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    resolved = _resolve_audio_attachment(message)
    if resolved is None:
        return
    file_id, duration, kind, extension = resolved
    owner_id = update.effective_user.id
    caption = (message.caption or "").strip()

    if caption.lower().startswith("/piano analyze"):
        remainder = caption[len("/piano analyze"):].strip()
        await _run_analysis(
            update, context, owner_id,
            file_id=file_id, duration=duration, extension=extension,
            piece_title_hint=remainder or None, user_note=None,
        )
        return

    context.user_data["pending_piano_audio"] = {
        "file_id": file_id,
        "duration": duration,
        "kind": kind,
        "extension": extension,
    }
    await message.reply_text(
        "\U0001f3b9 Is this a piano recording? "
        "Reply /piano analyze (optionally with a piece title) to get feedback."
    )


# ---------------------------------------------------------------------------
# /piano analyze
# ---------------------------------------------------------------------------


async def _piano_analyze(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
    args: list[str],
    text: str,
) -> None:
    hint = _strip_subcommand(text, 2) or None  # strip "/piano analyze"
    # In-message audio takes priority over pending stash.
    resolved = _resolve_audio_attachment(update.message)
    if resolved is None:
        pending = context.user_data.get("pending_piano_audio")
        if not pending:
            await update.message.reply_text(
                "Send a voice note first, then reply /piano analyze."
            )
            return
        file_id = pending["file_id"]
        duration = pending.get("duration")
        extension = pending.get("extension", "ogg")
    else:
        file_id, duration, _kind, extension = resolved

    await _run_analysis(
        update, context, owner_id,
        file_id=file_id, duration=duration, extension=extension,
        piece_title_hint=hint, user_note=hint,
    )


async def _run_analysis(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
    *,
    file_id: str,
    duration: int | None,
    extension: str,
    piece_title_hint: str | None,
    user_note: str | None,
) -> None:
    await update.message.reply_text(
        "\U0001f3a7 Analysing your recording — this may take a moment…"
    )

    try:
        file = await context.bot.get_file(file_id)
        raw = await file.download_as_bytearray()
        audio_bytes = bytes(raw)
    except Exception as exc:
        logger.error("Failed to download piano recording: %s", exc, exc_info=True)
        await update.message.reply_text("Could not download the audio.")
        return

    try:
        file_path = save_piano_recording(audio_bytes, owner_id, extension=extension)
    except Exception:
        logger.warning("Failed to save piano recording to disk", exc_info=True)
        file_path = None

    piece: dict | None = None
    if piece_title_hint:
        piece = await repertoire.find_piece_by_title(owner_id, piece_title_hint)

    try:
        analysis = await audio_agent.analyze_recording(
            owner_id=owner_id,
            piece=piece,
            audio_bytes=audio_bytes,
            audio_format=extension,
            user_note=user_note,
        )
    except LLMParseError as exc:
        logger.error("analyze_recording parse failed: %s", exc)
        await update.message.reply_text(
            "I got feedback from the model but couldn't parse it cleanly. "
            "Here it is as raw text:\n\n" + str(exc).split(": ", 1)[-1]
        )
        analysis = None
    except Exception as exc:
        logger.error("analyze_recording failed", exc_info=True)
        await update.message.reply_text(f"Analysis failed: {exc}")
        return

    if analysis is None:
        # Couldn't parse — don't persist.
        context.user_data.pop("pending_piano_audio", None)
        return

    feedback_text = audio_agent.format_feedback(analysis)

    summary = (analysis.get("overall_impression") or "").strip()[:500] or None
    try:
        await db.add_piano_recording(
            owner_id=owner_id,
            piece_id=(piece["id"] if piece else None),
            file_path=file_path,
            duration_seconds=duration,
            feedback_summary=summary,
            raw_analysis=json.dumps(analysis),
        )
    except Exception:
        logger.error("Failed to persist piano recording", exc_info=True)

    context.user_data.pop("pending_piano_audio", None)
    await update.message.reply_text(feedback_text)


# ---------------------------------------------------------------------------
# Text dispatch — called from calories.refine_handler
# ---------------------------------------------------------------------------


async def piano_text_dispatch(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Handle plain-text input that belongs to a piano pending state.

    Returns ``True`` when the message was consumed by piano (so the
    calorie-refine handler should skip its own logic).
    """
    if not context.user_data.get("pending_piano_log"):
        return False

    body = (update.message.text or "").strip()
    if not body:
        return False

    del context.user_data["pending_piano_log"]
    duration_override = context.user_data.pop("pending_piano_log_duration", None)
    owner_id = update.effective_user.id
    await _ingest_log(update, owner_id, body, duration_override=duration_override)
    return True


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


COMMANDS: list[tuple[str, str]] = [
    ("piano", "Piano practice coach (log/checkin/analyze)"),
]


def register(app) -> None:
    app.add_handler(CommandHandler("piano", piano_cmd))
    app.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO, piano_voice_handler)
    )
