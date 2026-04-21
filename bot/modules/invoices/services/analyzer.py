"""Invoice analysis service — local LLM via invoice_reader agent."""
from __future__ import annotations

import base64
import json
import logging
import re

from bot.services.agent_runner import load_agent, run_agent
from bot.services.llm import compress_image

logger = logging.getLogger(__name__)

_INVOICE_AGENT = "bot/modules/invoices/agents/invoice_reader.md"

# Matches a JSON string token including any content (with DOTALL so . covers \n)
_JSON_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)


_CTRL_CHAR_MAP = {
    0x08: "\\b", 0x09: "\\t", 0x0A: "\\n", 0x0C: "\\f", 0x0D: "\\r",
}


def _escape_control_chars(text: str) -> str:
    """Escape all bare control characters (U+0000–U+001F) inside JSON string values.

    The JSON spec forbids literal control characters in strings. PDF-extracted
    text often contains tabs, form feeds, or other non-printable bytes that the
    model echoes verbatim, making the response unparseable.
    """
    def _escape(m: re.Match) -> str:
        chars = []
        for ch in m.group(0):
            cp = ord(ch)
            if cp < 0x20:
                chars.append(_CTRL_CHAR_MAP.get(cp, f"\\u{cp:04x}"))
            else:
                chars.append(ch)
        return "".join(chars)
    return _JSON_STRING_RE.sub(_escape, text)


def _parse_llm_json(raw: str) -> tuple[dict, str]:
    """Return (parsed_dict, last_error). On success error is empty string."""
    text = _escape_control_chars(raw.strip())
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text), ""
    except json.JSONDecodeError as exc:
        logger.warning("First JSON parse attempt failed: %s", exc)
        last_error = str(exc)
        # raw_decode stops at the matching closing brace, ignoring any trailing
        # model commentary after the JSON object.
        start = text.find("{")
        if start != -1:
            try:
                obj, _ = json.JSONDecoder().raw_decode(text, start)
                if isinstance(obj, dict):
                    return obj, ""
            except json.JSONDecodeError as exc2:
                logger.warning("Second JSON parse attempt (raw_decode) failed: %s", exc2)
                last_error = str(exc2)
        return {}, last_error
    return {}, "no JSON object found"


async def analyze_invoice(raw_bytes: bytes, ext: str, mime_type: str) -> dict:
    """Run the invoice_reader agent on raw file bytes.

    Returns the parsed JSON dict from the LLM, or raises ValueError with a
    user-facing message if the input cannot be processed.
    """
    if mime_type.startswith("image/") or ext.lower() in (".jpg", ".jpeg", ".png", ".webp"):
        compressed = compress_image(raw_bytes)
        image_b64 = base64.b64encode(compressed).decode()
        messages: list[dict] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                    {"type": "text", "text": "Analyse this invoice."},
                ],
            }
        ]
    elif mime_type == "application/pdf" or ext.lower() == ".pdf":
        import io
        import pypdf
        try:
            reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
            pdf_text = "\n".join(
                page.extract_text() or "" for page in reader.pages
            ).strip()
        except Exception as exc:
            logger.error("PDF text extraction failed: %s", exc)
            raise ValueError(
                "Could not read this PDF. Try sending a photo of the invoice instead."
            ) from exc
        if not pdf_text:
            raise ValueError("PDF has no text layer (scanned). Please send a photo instead.")
        messages = [{"role": "user", "content": f"Analyse this invoice:\n\n{pdf_text}"}]
    else:
        raise ValueError("Please send a photo with caption /invoice, or a PDF document.")

    agent = load_agent(_INVOICE_AGENT)
    raw_result = await run_agent(agent, messages)
    logger.debug("Invoice LLM raw response (%d chars):\n%s", len(raw_result), raw_result)

    parsed, parse_error = _parse_llm_json(raw_result)
    if not parsed:
        snippet = raw_result[:1000].replace("\n", " ").strip()
        logger.error(
            "Invoice JSON parse failed (%s). Full LLM response (%d chars):\n%s",
            parse_error, len(raw_result), raw_result,
        )
        raise ValueError(
            f"Model response was not valid JSON: {parse_error}\n\nModel said: {snippet}"
        )
    return parsed
