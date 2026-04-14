from __future__ import annotations

import base64
import logging
import os

from bot.services import db
from bot.services.llm import _call_and_parse_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Analysis (direct audio input)
# ---------------------------------------------------------------------------


_ANALYSIS_SYSTEM = (
    "You are an expert piano teacher assistant. You will receive an audio "
    "recording of a student's piano practice together with written context. "
    "Listen to the audio directly — do not ask for a transcription. Assess "
    "tempo consistency, rhythmic accuracy, dynamics, phrasing, and technical "
    "issues. "
    "Return ONLY valid JSON, no markdown:\n"
    "{\n"
    '  "overall_impression": str,          // 1-2 sentences\n'
    '  "tempo": {"assessment": str, "notes": str},       // assessment in {steady, rushing, dragging, uneven}\n'
    '  "rhythm": {"assessment": str, "notes": str},      // assessment in {accurate, minor_errors, significant_errors}\n'
    '  "dynamics": {"assessment": str, "notes": str},\n'
    '  "problem_areas": [str],             // specific bars or passages to work on\n'
    '  "strengths": [str],\n'
    '  "next_session_focus": [str],        // max 3 actionable suggestions\n'
    '  "progress_vs_last": str             // one of {improved, similar, regressed, first_recording}\n'
    "}\n"
    "Always be encouraging but honest. Never refuse to analyse."
)


def _audio_format_for_api(extension: str | None) -> str:
    """Map a file extension onto the OpenAI Chat Completions ``input_audio.format``.

    The API currently recognises: ``wav``, ``mp3``, ``flac``, ``ogg``, ``opus``,
    ``m4a``, ``webm``. Providers differ on exact support — Gemini audio accepts
    ogg/opus directly; OpenAI's gpt-4o-audio-preview only accepts wav/mp3. We
    pass what we have and let the provider reject unsupported formats.
    """
    if not extension:
        return "ogg"
    ext = extension.lower().lstrip(".")
    # Telegram voice messages arrive as opus-in-ogg; many providers expect "ogg".
    if ext in ("oga", "ogx"):
        return "ogg"
    return ext


async def analyze_recording(
    owner_id: int,
    piece: dict | None,
    audio_bytes: bytes | None,
    audio_format: str | None,
    user_note: str | None,
) -> dict:
    """Call the analysis-tier multimodal model with raw audio and return feedback.

    The audio is sent inline as a base64-encoded ``input_audio`` content block.
    PIANO_ANALYSIS_MODEL must point to a model that accepts audio input
    (e.g. ``google/gemini-2.0-flash-001`` or ``openai/gpt-4o-audio-preview``).
    Raises ``LLMParseError`` when the model fails to return valid JSON after
    one retry.
    """
    analysis_model = os.getenv("PIANO_ANALYSIS_MODEL")

    context_parts: list[str] = []
    if piece:
        composer = f" by {piece['composer']}" if piece.get("composer") else ""
        status = piece.get("status") or "learning"
        context_parts.append(f"Piece: {piece['title']}{composer} (status: {status}).")
        if piece.get("notes"):
            context_parts.append(f"Piece notes: {piece['notes']}.")
        prior = await db.list_piano_recordings(
            owner_id, piece_id=piece["id"], limit=3,
        )
        if prior:
            context_parts.append("Prior recording feedback (most recent first):")
            for rec in prior:
                summary = (rec.get("feedback_summary") or "").strip()
                if summary:
                    context_parts.append(f"- {rec['recorded_at']}: {summary[:300]}")
    else:
        context_parts.append("Piece: unspecified by the user.")

    if user_note:
        context_parts.append(f"User note: {user_note}")

    text_block = (
        "\n\n".join(context_parts)
        if context_parts
        else "Analyse the attached piano recording."
    )
    user_content: list[dict] = [{"type": "text", "text": text_block}]

    if audio_bytes:
        b64 = base64.b64encode(audio_bytes).decode("ascii")
        fmt = _audio_format_for_api(audio_format)
        user_content.append({
            "type": "input_audio",
            "input_audio": {"data": b64, "format": fmt},
        })
    else:
        user_content[0]["text"] += (
            "\n\nNo audio was attached — rely on the user note and piece history."
        )

    messages = [
        {"role": "system", "content": _ANALYSIS_SYSTEM},
        {"role": "user", "content": user_content},
    ]

    logger.debug(
        "piano analyze_recording: piece=%s audio_bytes=%d fmt=%s",
        piece and piece.get("title"),
        len(audio_bytes or b""),
        audio_format,
    )

    return await _call_and_parse_json(
        label="analyze_recording",
        messages=messages,
        model_override=analysis_model,
    )


# ---------------------------------------------------------------------------
# Feedback formatting
# ---------------------------------------------------------------------------


def _fmt_section(label: str, section: dict | None) -> list[str]:
    if not isinstance(section, dict):
        return []
    assessment = section.get("assessment") or ""
    notes = section.get("notes") or ""
    line = f"{label}: {assessment}".rstrip(": ")
    out = [line]
    if notes:
        out.append(f"  {notes}")
    return out


def format_feedback(analysis: dict) -> str:
    lines: list[str] = ["\U0001f3b9 Recording feedback"]
    overall = analysis.get("overall_impression")
    if overall:
        lines.append("")
        lines.append(str(overall))

    progress = analysis.get("progress_vs_last")
    if progress:
        lines.append("")
        lines.append(f"Progress: {progress}")

    for label, key in (("Tempo", "tempo"), ("Rhythm", "rhythm"), ("Dynamics", "dynamics")):
        section_lines = _fmt_section(label, analysis.get(key))
        if section_lines:
            lines.append("")
            lines.extend(section_lines)

    strengths = analysis.get("strengths") or []
    if strengths:
        lines.append("")
        lines.append("Strengths:")
        lines.extend(f"  \u2022 {item}" for item in strengths)

    problems = analysis.get("problem_areas") or []
    if problems:
        lines.append("")
        lines.append("Problem areas:")
        lines.extend(f"  \u2022 {item}" for item in problems)

    nextf = analysis.get("next_session_focus") or []
    if nextf:
        lines.append("")
        lines.append("Next session focus:")
        lines.extend(f"  \u2022 {item}" for item in nextf)

    return "\n".join(lines)
