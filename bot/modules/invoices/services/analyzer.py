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


def _parse_llm_json(raw: str) -> dict:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        obj = re.search(r"\{[\s\S]*\}", text)
        if obj:
            try:
                return json.loads(obj.group())
            except json.JSONDecodeError:
                pass
    return {}


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
    return _parse_llm_json(raw_result)
