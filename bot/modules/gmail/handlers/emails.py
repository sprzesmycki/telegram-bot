"""Gmail email fetching and display handlers."""
from __future__ import annotations

import asyncio
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from bot.config import get_config

logger = logging.getLogger(__name__)

COMMANDS: list[tuple[str, str]] = [
    ("emails", "Fetch unread Gmail messages"),
]

_BODY_PREVIEW_LEN = 500

# Global registry so invoice buttons work for both /emails and scheduled notifications.
_inv_registry: dict[int, dict] = {}
_inv_next_key: int = 0


def add_invoice_keyboard(
    email_data,
    kb: InlineKeyboardMarkup | None,
    *,
    apple_auto_queue: list | None = None,
) -> InlineKeyboardMarkup | None:
    """Append invoice action buttons to an email keyboard.

    PDF attachments get a per-key entry in _inv_registry (looked up by the callback handler).
    Apple invoices get a button unless apple_auto_queue is provided, in which case the email
    id is appended there instead (used by /emails auto-process mode).
    """
    global _inv_next_key
    cfg = get_config()
    if not cfg.modules.invoices.enabled:
        return kb

    inv_rows = []
    for att in email_data.attachments:
        if att.local_path and att.filename.lower().endswith(".pdf"):
            key = _inv_next_key
            _inv_registry[key] = {"path": att.local_path, "gmail_id": email_data.id}
            _inv_next_key += 1
            label = att.filename[:40]
            inv_rows.append(
                [InlineKeyboardButton(f"🧾 Invoice: {label}", callback_data=f"inv_email:{key}")]
            )

    is_apple = (
        not inv_rows
        and email_data.body_text
        and "your invoice from apple" in email_data.subject.lower()
    )
    if is_apple:
        if apple_auto_queue is not None:
            apple_auto_queue.append(email_data.id)
        else:
            inv_rows.append(
                [InlineKeyboardButton("🧾 Parse Apple invoice", callback_data=f"inv_body:{email_data.id}")]
            )

    if inv_rows:
        existing = list(kb.inline_keyboard) if kb else []
        return InlineKeyboardMarkup(existing + inv_rows)
    return kb


def format_email(email_data, preview_only: bool = True) -> tuple[str, InlineKeyboardMarkup | None]:
    """Return (message_text, optional_keyboard) for an EmailData object."""
    e = email_data
    body = e.body_text or "(no body)"
    truncated = preview_only and len(body) > _BODY_PREVIEW_LEN
    display_body = body[:_BODY_PREVIEW_LEN] + "…" if truncated else body

    att_line = ""
    if e.attachments:
        parts = [f"{att.filename} ({att.size_bytes // 1024}KB)" for att in e.attachments]
        att_line = f"\n📎 {', '.join(parts)}"

    text = (
        f"📧 {e.subject}\n"
        f"From: {e.sender}\n"
        f"Date: {e.date}\n\n"
        f"{display_body}"
        f"{att_line}"
    )

    kb = None
    if truncated:
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Read more", callback_data=f"gmail_read:{e.id}")]]
        )
    return text, kb


async def emails_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = get_config()
    credentials_path = os.getenv("GMAIL_CREDENTIALS_PATH", "./credentials.json")
    gmail_cfg = cfg.modules.gmail

    limit = gmail_cfg.max_results
    if context.args:
        try:
            limit = max(1, min(int(context.args[0]), 50))
        except ValueError:
            pass

    from bot.services.gmail import fetch_unread, load_gmail_service

    loop = asyncio.get_event_loop()
    try:
        service = await loop.run_in_executor(
            None, lambda: load_gmail_service(credentials_path)
        )
    except FileNotFoundError:
        await update.message.reply_text(
            "❌ Gmail credentials not found.\n"
            "Set GMAIL_CREDENTIALS_PATH in .env and run scripts/gmail_auth.py first."
        )
        return
    except Exception as e:
        logger.error("Gmail auth error: %s", e, exc_info=True)
        await update.message.reply_text(f"❌ Gmail auth error: {e}")
        return

    try:
        emails = await loop.run_in_executor(
            None,
            lambda: fetch_unread(
                service,
                gmail_cfg.label,
                limit,
                None,
                cfg.storage.gmail_attachments_dir,
            ),
        )
    except Exception as e:
        logger.error("Gmail fetch error: %s", e, exc_info=True)
        await update.message.reply_text(f"❌ Failed to fetch emails: {e}")
        return

    if not emails:
        await update.message.reply_text("📭 No unread emails.")
        return

    auto_process = cfg.modules.invoices.enabled and cfg.modules.gmail.auto_process_invoices
    apple_auto_queue: list[str] = [] if auto_process else None
    context.user_data.setdefault("gmail_bodies", {})

    for email_data in emails:
        context.user_data["gmail_bodies"][email_data.id] = email_data.body_text
        text, kb = format_email(email_data)
        kb = add_invoice_keyboard(email_data, kb, apple_auto_queue=apple_auto_queue)
        await update.message.reply_text(text, reply_markup=kb)

    if apple_auto_queue:
        from bot.modules.invoices.handlers.invoices import start_gmail_auto_processing
        await start_gmail_auto_processing(
            context,
            update.effective_chat.id,
            update.effective_user.id,
            context.bot,
            apple_auto_queue,
        )


async def read_more_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    msg_id = query.data.split(":", 1)[1]
    bodies = context.user_data.get("gmail_bodies", {})
    full_body = bodies.get(msg_id, "(body not available — re-run /emails)")
    await query.message.reply_text(full_body[:4000])


def register(app: Application) -> None:
    app.add_handler(CommandHandler("emails", emails_cmd))
    app.add_handler(CallbackQueryHandler(read_more_callback, pattern=r"^gmail_read:"))
