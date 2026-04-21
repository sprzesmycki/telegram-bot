"""Invoice reading, classification, and storage handler.

Supported inputs:
- Photo with caption ``/invoice`` → vision LLM analysis (local gemma4:26b)
- PDF document → text extraction via pypdf → LLM analysis

Flow: receive file → LLM → pending_invoices (DB) → preview card → ✅/❌
On confirm: duplicate check → if found → Replace / Skip choice → save
"""
from __future__ import annotations

import logging
import re
import shutil
import uuid
from datetime import date as _date
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.config import get_config
from bot.modules.invoices.services.analyzer import analyze_invoice
from bot.services import db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tmp_path(ext: str) -> Path:
    cfg = get_config()
    d = Path(cfg.storage.invoices_dir) / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"tmp_{uuid.uuid4().hex}{ext}"


def _make_final_path(result: dict, ext: str) -> Path:
    cfg = get_config()
    category = result.get("category") or "uncategorized"
    category_safe = re.sub(r"[^\w]", "_", category).strip("_").lower() or "uncategorized"
    invoices_dir = Path(cfg.storage.invoices_dir) / category_safe
    invoices_dir.mkdir(parents=True, exist_ok=True)

    vendor = result.get("vendor") or "Unknown"
    issue_date = result.get("issue_date") or _date.today().isoformat()

    vendor_safe = re.sub(r"[^\w]", "_", vendor)[:40].strip("_") or "Unknown"
    stem = f"{vendor_safe}_{issue_date}"

    candidate = invoices_dir / f"{stem}{ext}"
    counter = 2
    while candidate.exists():
        candidate = invoices_dir / f"{stem}_{counter}{ext}"
        counter += 1
    return candidate


def _fmt_amount(val, currency: str) -> str:
    if val is None:
        return "—"
    try:
        return f"{float(val):,.2f} {currency}".strip()
    except (TypeError, ValueError):
        return str(val)


def _format_invoice_preview(result: dict) -> str:
    vendor = result.get("vendor") or "Unknown"
    inv_num = result.get("invoice_number") or "—"
    issue_date = result.get("issue_date") or "—"
    due_date = result.get("due_date") or "—"
    currency = result.get("currency") or ""
    total = result.get("total")
    subtotal = result.get("subtotal")
    tax = result.get("tax")
    category = result.get("category") or "—"
    subcategory = result.get("subcategory")
    recurring = result.get("recurring", False)
    notes = result.get("notes") or "—"
    line_items = result.get("line_items") or []

    cat_str = category
    if subcategory:
        cat_str += f" › {subcategory}"
    if recurring:
        cat_str += "  🔁"

    lines = [
        "🧾 Invoice Preview",
        "",
        f"Vendor:      {vendor}",
        f"Invoice #:   {inv_num}",
        f"Issue date:  {issue_date}",
        f"Due date:    {due_date}",
        f"Category:    {cat_str}",
        "",
        f"Subtotal:    {_fmt_amount(subtotal, currency)}",
    ]
    if tax:
        lines.append(f"Tax:         {_fmt_amount(tax, currency)}")
    lines.append(f"Total:       {_fmt_amount(total, currency)}")

    if line_items:
        lines.append("")
        lines.append("Line items:")
        for item in line_items[:5]:
            desc = item.get("description", "")
            qty = item.get("quantity", 1)
            amount = item.get("amount")
            lines.append(f"  • {desc} × {qty} — {_fmt_amount(amount, currency)}")
        if len(line_items) > 5:
            lines.append(f"  … and {len(line_items) - 5} more")

    lines += ["", f"Notes:  {notes}", "", "Is this correct?"]
    return "\n".join(lines)


def _save_discard_kb(pending_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Save", callback_data=f"invoice_confirm:{pending_id}"),
        InlineKeyboardButton("❌ Discard", callback_data=f"invoice_discard:{pending_id}"),
    ]])


def _replace_skip_kb(pending_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Replace", callback_data=f"invoice_replace:{pending_id}"),
        InlineKeyboardButton("⏭️ Skip", callback_data=f"invoice_skip:{pending_id}"),
    ]])


# ---------------------------------------------------------------------------
# Core analysis pipeline
# ---------------------------------------------------------------------------


async def _analyse_and_preview(
    update: Update,
    owner_id: int,
    raw_bytes: bytes,
    ext: str,
    mime_type: str,
    original_filename: str | None = None,
) -> None:
    tmp_path = _make_tmp_path(ext)
    tmp_path.write_bytes(raw_bytes)

    status_msg = await update.message.reply_text("🔍 Analysing invoice…")

    try:
        result = await analyze_invoice(raw_bytes, ext, mime_type)
    except ValueError as exc:
        tmp_path.unlink(missing_ok=True)
        await status_msg.edit_text(f"❌ {exc}")
        return
    except Exception as exc:
        logger.error("Invoice LLM error: %s", exc, exc_info=True)
        tmp_path.unlink(missing_ok=True)
        await status_msg.edit_text(f"❌ Analysis failed: {exc}")
        return

    if result.get("error") == "not_an_invoice":
        tmp_path.unlink(missing_ok=True)
        await status_msg.edit_text("⚠️ This doesn't look like an invoice.")
        return

    if not result:
        tmp_path.unlink(missing_ok=True)
        await status_msg.edit_text("❌ Could not parse LLM response. Please try again.")
        return

    result["_meta"] = {"source": "manual", "original_filename": original_filename}
    pending_id = await db.create_pending_invoice(owner_id, str(tmp_path), result)

    await status_msg.edit_text(
        _format_invoice_preview(result),
        reply_markup=_save_discard_kb(pending_id),
    )


# ---------------------------------------------------------------------------
# Save helper (shared by confirm and replace callbacks)
# ---------------------------------------------------------------------------


async def _do_save(
    message: Message,
    owner_id: int,
    pending_id: int,
    result: dict,
    meta: dict,
) -> None:
    source = meta.get("source", "manual")
    gmail_message_id = meta.get("gmail_message_id")
    original_filename = meta.get("original_filename")

    tmp_path = Path((await db.get_pending_invoice(pending_id, owner_id))["tmp_file_path"])
    ext = tmp_path.suffix

    final_path = _make_final_path(result, ext)
    try:
        if source in ("gmail", "catalog"):
            shutil.copy2(str(tmp_path), final_path)
        else:
            tmp_path.rename(final_path)
    except Exception as exc:
        logger.error("Failed to save invoice file: %s", exc)
        final_path = tmp_path

    await db.log_invoice(
        owner_id, result, str(final_path), source, gmail_message_id, original_filename
    )
    await db.delete_pending_invoice(pending_id)

    vendor = result.get("vendor") or "Unknown"
    total_str = _fmt_amount(result.get("total"), result.get("currency") or "")
    await message.edit_text(
        f"✅ Invoice saved!\n\n{vendor} — {total_str}\n📁 {final_path.name}"
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photos sent with caption ``/invoice``."""
    owner_id = update.effective_user.id
    photo = update.message.photo[-1]
    file = await photo.get_file()
    raw_bytes = bytes(await file.download_as_bytearray())
    await _analyse_and_preview(update, owner_id, raw_bytes, ".jpg", "image/jpeg")
    raise ApplicationHandlerStop


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle PDF document uploads."""
    owner_id = update.effective_user.id
    doc = update.message.document
    mime_type = doc.mime_type or "application/octet-stream"
    ext = Path(doc.file_name or "").suffix.lower() or ".pdf"
    file = await doc.get_file()
    raw_bytes = bytes(await file.download_as_bytearray())
    await _analyse_and_preview(
        update, owner_id, raw_bytes, ext, mime_type,
        original_filename=doc.file_name,
    )


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    owner_id = update.effective_user.id

    pending_id = int(query.data.split(":", 1)[1])
    pending = await db.get_pending_invoice(pending_id, owner_id)
    if not pending:
        await query.message.edit_text("⚠️ Invoice not found (may have expired). Please resend.")
        return

    result = dict(pending["parsed"])
    meta = result.pop("_meta", {})

    duplicate = await db.find_duplicate_invoice(
        owner_id,
        result.get("invoice_number"),
        meta.get("original_filename"),
    )
    if duplicate:
        dup_vendor = duplicate.get("vendor") or "Unknown"
        dup_date = str(duplicate.get("issue_date") or "—")[:10]
        dup_num = duplicate.get("invoice_number") or "—"
        dup_file = Path(duplicate.get("original_filename") or "").name or "—"
        dup_total = _fmt_amount(duplicate.get("total"), duplicate.get("currency") or "")
        await query.message.edit_text(
            f"⚠️ Duplicate invoice found!\n\n"
            f"Vendor:     {dup_vendor}\n"
            f"Invoice #:  {dup_num}\n"
            f"Date:       {dup_date}\n"
            f"Total:      {dup_total}\n"
            f"File:       {dup_file}\n\n"
            f"Replace existing or skip?",
            reply_markup=_replace_skip_kb(pending_id),
        )
        return

    await _do_save(query.message, owner_id, pending_id, result, meta)
    if "invoice_scan_total" in context.user_data:
        await _advance_scan_queue(context, context.bot, query.message.chat_id, owner_id)


async def replace_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    owner_id = update.effective_user.id

    pending_id = int(query.data.split(":", 1)[1])
    pending = await db.get_pending_invoice(pending_id, owner_id)
    if not pending:
        await query.message.edit_text("⚠️ Invoice not found (may have expired). Please resend.")
        return

    result = dict(pending["parsed"])
    meta = result.pop("_meta", {})

    duplicate = await db.find_duplicate_invoice(
        owner_id,
        result.get("invoice_number"),
        meta.get("original_filename"),
    )
    if duplicate:
        if duplicate.get("file_path"):
            Path(duplicate["file_path"]).unlink(missing_ok=True)
        await db.delete_invoice(owner_id, duplicate["id"])

    await _do_save(query.message, owner_id, pending_id, result, meta)
    if "invoice_scan_total" in context.user_data:
        await _advance_scan_queue(context, context.bot, query.message.chat_id, owner_id)


async def skip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    owner_id = update.effective_user.id

    pending_id = int(query.data.split(":", 1)[1])
    pending = await db.get_pending_invoice(pending_id, owner_id)
    if pending:
        source = pending["parsed"].get("_meta", {}).get("source", "manual")
        if source not in ("gmail", "catalog"):
            Path(pending["tmp_file_path"]).unlink(missing_ok=True)
        await db.delete_pending_invoice(pending_id)

    await query.message.edit_text("⏭️ Skipped — existing invoice kept.")
    if "invoice_scan_total" in context.user_data:
        await _advance_scan_queue(context, context.bot, query.message.chat_id, owner_id)


async def discard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    owner_id = update.effective_user.id

    pending_id = int(query.data.split(":", 1)[1])
    pending = await db.get_pending_invoice(pending_id, owner_id)
    if pending:
        source = pending["parsed"].get("_meta", {}).get("source", "manual")
        if source not in ("gmail", "catalog"):
            Path(pending["tmp_file_path"]).unlink(missing_ok=True)
        await db.delete_pending_invoice(pending_id)

    await query.message.edit_text("🗑️ Invoice discarded.")
    if "invoice_scan_total" in context.user_data:
        await _advance_scan_queue(context, context.bot, query.message.chat_id, owner_id)


async def email_attachment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process a PDF attachment from a Gmail email as an invoice."""
    query = update.callback_query
    await query.answer()
    owner_id = update.effective_user.id

    key = int(query.data.split(":", 1)[1])
    entry = context.user_data.get("gmail_inv_paths", {}).get(key)
    if not entry:
        await query.message.reply_text("❌ Attachment not found. Re-run /emails and try again.")
        return

    local_path = Path(entry["path"])
    gmail_id = entry["gmail_id"]

    if not local_path.exists():
        await query.message.reply_text("❌ Attachment file missing from disk. Re-run /emails.")
        return

    raw_bytes = local_path.read_bytes()
    ext = local_path.suffix.lower()
    mime_type = "application/pdf" if ext == ".pdf" else "image/jpeg"

    status_msg = await query.message.reply_text("🔍 Analysing invoice…")

    try:
        result = await analyze_invoice(raw_bytes, ext, mime_type)
    except ValueError as exc:
        await status_msg.edit_text(f"❌ {exc}")
        return
    except Exception as exc:
        logger.error("Invoice email attachment error: %s", exc, exc_info=True)
        await status_msg.edit_text(f"❌ Analysis failed: {exc}")
        return

    if result.get("error") == "not_an_invoice":
        await status_msg.edit_text("⚠️ This doesn't look like an invoice.")
        return

    if not result:
        await status_msg.edit_text("❌ Could not parse LLM response. Please try again.")
        return

    result["_meta"] = {
        "source": "gmail",
        "gmail_message_id": gmail_id,
        "original_filename": local_path.name,
    }
    pending_id = await db.create_pending_invoice(owner_id, str(local_path), result)

    await status_msg.edit_text(
        _format_invoice_preview(result),
        reply_markup=_save_discard_kb(pending_id),
    )


# ---------------------------------------------------------------------------
# Catalog scan
# ---------------------------------------------------------------------------

_CATALOG_EXTS = {".pdf", ".jpg", ".jpeg", ".png"}


async def _process_catalog_file(
    bot,
    chat_id: int,
    owner_id: int,
    file_path: Path,
    idx: int,
    total: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    ext = file_path.suffix.lower()
    mime_type = "application/pdf" if ext == ".pdf" else "image/jpeg"
    raw_bytes = file_path.read_bytes()

    status_msg = await bot.send_message(chat_id, f"🔍 Analysing {file_path.name} ({idx}/{total})…")

    try:
        result = await analyze_invoice(raw_bytes, ext, mime_type)
    except ValueError as exc:
        await status_msg.edit_text(f"❌ {file_path.name}: {exc}")
        await _advance_scan_queue(context, bot, chat_id, owner_id)
        return
    except Exception as exc:
        logger.error("Catalog invoice error for %s: %s", file_path.name, exc, exc_info=True)
        await status_msg.edit_text(f"❌ {file_path.name}: analysis failed, skipping.")
        await _advance_scan_queue(context, bot, chat_id, owner_id)
        return

    if result.get("error") == "not_an_invoice" or not result:
        await status_msg.edit_text(f"⚠️ {file_path.name} — not an invoice, skipping.")
        await _advance_scan_queue(context, bot, chat_id, owner_id)
        return

    result["_meta"] = {"source": "catalog", "original_filename": file_path.name}
    pending_id = await db.create_pending_invoice(owner_id, str(file_path), result)

    await status_msg.edit_text(
        f"📁 {file_path.name}  ({idx}/{total})\n\n" + _format_invoice_preview(result),
        reply_markup=_save_discard_kb(pending_id),
    )


async def _advance_scan_queue(
    context: ContextTypes.DEFAULT_TYPE,
    bot,
    chat_id: int,
    owner_id: int,
) -> None:
    queue = context.user_data.get("invoice_scan_queue", [])
    if not queue:
        total = context.user_data.pop("invoice_scan_total", 0)
        context.user_data.pop("invoice_scan_queue", None)
        await bot.send_message(chat_id, f"✅ Catalog scan complete — {total} file(s) reviewed.")
        return

    file_path_str = queue.pop(0)
    total = context.user_data.get("invoice_scan_total", len(queue) + 1)
    idx = total - len(queue)
    await _process_catalog_file(bot, chat_id, owner_id, Path(file_path_str), idx, total, context)


async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scan a catalog directory and process unprocessed invoice files one by one."""
    cfg = get_config()
    owner_id = update.effective_user.id

    if context.args:
        catalog_dir = Path(" ".join(context.args))
    else:
        catalog_dir = Path(cfg.storage.invoice_catalog_dir)

    if not catalog_dir.exists():
        await update.message.reply_text(
            f"❌ Directory not found: {catalog_dir}\n"
            f"Set invoice_catalog_dir in config.yaml or pass a path: /scan /path/to/dir"
        )
        return

    all_files = sorted(
        f for f in catalog_dir.iterdir()
        if f.is_file() and f.suffix.lower() in _CATALOG_EXTS
    )
    if not all_files:
        await update.message.reply_text(f"📂 No PDF/image files found in {catalog_dir}.")
        return

    processed = await db.get_processed_filenames(owner_id)
    unprocessed = [f for f in all_files if f.name not in processed]

    if not unprocessed:
        await update.message.reply_text(
            f"✅ All {len(all_files)} file(s) in catalog already processed."
        )
        return

    total = len(unprocessed)
    context.user_data["invoice_scan_queue"] = [str(f) for f in unprocessed[1:]]
    context.user_data["invoice_scan_total"] = total

    await update.message.reply_text(
        f"📂 Found {total} unprocessed file(s) in {catalog_dir.name}/. Starting…"
    )
    await _process_catalog_file(
        context.bot,
        update.effective_chat.id,
        owner_id,
        unprocessed[0],
        1,
        total,
        context,
    )


async def scan_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if "invoice_scan_total" not in context.user_data:
        await update.message.reply_text("No catalog scan in progress.")
        return
    remaining = len(context.user_data.get("invoice_scan_queue", []))
    context.user_data.pop("invoice_scan_queue", None)
    context.user_data.pop("invoice_scan_total", None)
    await update.message.reply_text(f"🛑 Scan stopped. {remaining} file(s) skipped.")


async def invoice_help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🧾 Invoice Module\n\n"
        "• Send a photo with caption /invoice — analyse a photo invoice\n"
        "• Send a PDF document — analyse a PDF invoice\n"
        "• /scan [dir] — process all unprocessed files in catalog dir\n"
        "• /invoices [N] — list last N saved invoices (default 10)\n\n"
        "All processing is done locally (gemma4:26b)."
    )


async def invoices_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    limit = 10
    if context.args:
        try:
            limit = max(1, min(int(context.args[0]), 50))
        except ValueError:
            pass

    rows = await db.list_invoices(owner_id, limit)
    if not rows:
        await update.message.reply_text(
            "📂 No invoices saved yet.\n\n"
            "Send a PDF or a photo with caption /invoice to add one."
        )
        return

    lines = [f"📂 Last {len(rows)} invoice(s):\n"]
    for row in rows:
        date_str = str(row.get("issue_date") or "—")[:10]
        vendor = row.get("vendor") or "Unknown"
        total = row.get("total")
        currency = row.get("currency") or ""
        cat = row.get("category") or "—"
        sub = row.get("subcategory")
        cat_str = f"{cat} › {sub}" if sub else cat
        recurring_mark = " 🔁" if row.get("recurring") else ""
        lines.append(f"#{row['id']}  {date_str}  {vendor}  {_fmt_amount(total, currency)}  [{cat_str}]{recurring_mark}")

    await update.message.reply_text("\n".join(lines))


COMMANDS: list[tuple[str, str]] = [
    ("invoice", "Invoice help and usage"),
    ("invoices", "List saved invoices"),
    ("scan", "Scan invoice catalog directory"),
    ("scan_stop", "Stop current catalog scan"),
]


def register(app: Application) -> None:
    # Group -1 fires before the calories photo handler (group 0).
    # ApplicationHandlerStop prevents calories from also processing these photos.
    app.add_handler(
        MessageHandler(
            filters.PHOTO & filters.CaptionRegex(r"(?i)^/invoice"),
            photo_handler,
        ),
        group=-1,
    )
    app.add_handler(MessageHandler(filters.Document.PDF, document_handler))
    app.add_handler(CallbackQueryHandler(confirm_callback, pattern=r"^invoice_confirm:"))
    app.add_handler(CallbackQueryHandler(replace_callback, pattern=r"^invoice_replace:"))
    app.add_handler(CallbackQueryHandler(skip_callback, pattern=r"^invoice_skip:"))
    app.add_handler(CallbackQueryHandler(discard_callback, pattern=r"^invoice_discard:"))
    app.add_handler(CallbackQueryHandler(email_attachment_callback, pattern=r"^inv_email:"))
    app.add_handler(CommandHandler("invoice", invoice_help_cmd))
    app.add_handler(CommandHandler("invoices", invoices_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("scan_stop", scan_stop_cmd))
