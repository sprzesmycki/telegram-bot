"""Invoice reading, classification, and storage handler.

Supported inputs:
- Photo with caption ``/invoice`` → vision LLM analysis (local gemma4:26b)
- PDF document → text extraction via pypdf → LLM analysis

Flow: receive file → LLM → pending_invoices (DB) → preview card → ✅/❌
On confirm: duplicate check → if found → Replace / Skip choice → save
"""
from __future__ import annotations

import calendar
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
from bot.modules.invoices.services.summary import build_avg_summary, build_month_summary
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

    tmp_file_path = (await db.get_pending_invoice(pending_id, owner_id))["tmp_file_path"]
    tmp_path = Path(tmp_file_path) if tmp_file_path else None

    if tmp_path:
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
    else:
        final_path = None

    await db.log_invoice(
        owner_id, result, str(final_path) if final_path else "", source, gmail_message_id, original_filename
    )
    await db.delete_pending_invoice(pending_id)

    vendor = result.get("vendor") or "Unknown"
    total_str = _fmt_amount(result.get("total"), result.get("currency") or "")
    file_line = f"\n📁 {final_path.name}" if final_path else ""
    await message.edit_text(f"✅ Invoice saved!\n\n{vendor} — {total_str}{file_line}")


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
        if source not in ("gmail", "catalog") and pending["tmp_file_path"]:
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
        if source not in ("gmail", "catalog") and pending["tmp_file_path"]:
            Path(pending["tmp_file_path"]).unlink(missing_ok=True)
        await db.delete_pending_invoice(pending_id)

    await query.message.edit_text("🗑️ Invoice discarded.")
    if "invoice_scan_total" in context.user_data:
        await _advance_scan_queue(context, context.bot, query.message.chat_id, owner_id)


async def email_body_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parse an email body (e.g. Apple invoice) as an invoice."""
    query = update.callback_query
    await query.answer()
    owner_id = update.effective_user.id

    email_id = query.data.split(":", 1)[1]
    body_text = context.user_data.get("gmail_bodies", {}).get(email_id)
    if not body_text:
        await query.message.reply_text("❌ Email body not available. Re-run /emails and try again.")
        return

    status_msg = await query.message.reply_text("🔍 Analysing invoice…")

    try:
        result = await analyze_invoice(body_text.encode(), ".txt", "text/plain")
    except ValueError as exc:
        await status_msg.edit_text(f"❌ {exc}")
        return
    except Exception as exc:
        logger.error("Invoice email body error: %s", exc, exc_info=True)
        await status_msg.edit_text(f"❌ Analysis failed: {exc}")
        return

    if result.get("error") == "not_an_invoice":
        await status_msg.edit_text("⚠️ This doesn't look like an invoice.")
        return

    result["_meta"] = {"source": "gmail", "gmail_message_id": email_id, "original_filename": None}
    pending_id = await db.create_pending_invoice(owner_id, "", result)

    await status_msg.edit_text(
        _format_invoice_preview(result),
        reply_markup=_save_discard_kb(pending_id),
    )


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
# Gmail auto-processing (no confirm dialog — saves all, shows summary at end)
# ---------------------------------------------------------------------------


def _build_auto_summary(results: list[dict]) -> str:
    saved = [r for r in results if r["status"] == "saved"]
    skipped_dup = [r for r in results if r["status"] == "duplicate"]
    not_invoice = [r for r in results if r["status"] == "not_invoice"]
    errors = [r for r in results if r["status"] == "error"]

    lines = [f"📊 Auto-processed {len(results)} email(s)\n"]

    if saved:
        lines.append(f"✅ Saved ({len(saved)}):")
        for r in saved:
            lines.append(f"  • {r['vendor']} — {_fmt_amount(r['total'], r['currency'])}")

    if skipped_dup:
        lines.append(f"\n⏭️ Skipped — duplicate ({len(skipped_dup)}):")
        for r in skipped_dup:
            lines.append(f"  • {r['vendor']} — {_fmt_amount(r['total'], r['currency'])}")

    if not_invoice:
        lines.append(f"\n⚠️ Not an invoice ({len(not_invoice)}):")
        for r in not_invoice:
            lines.append(f"  • {r.get('label', '(unknown)')}")

    if errors:
        lines.append(f"\n❌ Errors ({len(errors)}):")
        for r in errors:
            lines.append(f"  • {r.get('label', '(unknown)')}: {r.get('error', '')}")

    return "\n".join(lines)


async def _process_gmail_auto_item(
    bot,
    chat_id: int,
    owner_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    email_id: str,
    idx: int,
    total: int,
) -> None:
    results: list[dict] = context.user_data.setdefault("gmail_auto_results", [])
    body_text = context.user_data.get("gmail_bodies", {}).get(email_id)

    if not body_text:
        results.append({"status": "error", "label": email_id, "error": "body not available"})
        await _advance_gmail_auto_queue(context, bot, chat_id, owner_id)
        return

    status_msg = await bot.send_message(chat_id, f"🔍 Processing Apple invoice {idx}/{total}…")

    try:
        result = await analyze_invoice(body_text.encode(), ".txt", "text/plain")
    except Exception as exc:
        logger.error("Gmail auto invoice error for %s: %s", email_id, exc, exc_info=True)
        results.append({"status": "error", "label": email_id, "error": str(exc)})
        await status_msg.delete()
        await _advance_gmail_auto_queue(context, bot, chat_id, owner_id)
        return

    if result.get("error") == "not_an_invoice" or not result:
        results.append({"status": "not_invoice", "label": email_id})
        await status_msg.delete()
        await _advance_gmail_auto_queue(context, bot, chat_id, owner_id)
        return

    vendor = result.get("vendor") or "Unknown"
    total_val = result.get("total")
    currency = result.get("currency") or ""

    duplicate = await db.find_duplicate_invoice(owner_id, result.get("invoice_number"), None)
    if duplicate:
        results.append({
            "status": "duplicate",
            "vendor": vendor,
            "total": total_val,
            "currency": currency,
        })
        await status_msg.delete()
        await _advance_gmail_auto_queue(context, bot, chat_id, owner_id)
        return

    await db.log_invoice(owner_id, result, "", "gmail", email_id, None)
    results.append({
        "status": "saved",
        "vendor": vendor,
        "total": total_val,
        "currency": currency,
    })
    await status_msg.delete()
    await _advance_gmail_auto_queue(context, bot, chat_id, owner_id)


async def _advance_gmail_auto_queue(
    context: ContextTypes.DEFAULT_TYPE,
    bot,
    chat_id: int,
    owner_id: int,
) -> None:
    queue: list[str] = context.user_data.get("gmail_auto_queue", [])
    if not queue:
        results = context.user_data.pop("gmail_auto_results", [])
        total = context.user_data.pop("gmail_auto_total", len(results))
        context.user_data.pop("gmail_auto_queue", None)
        await bot.send_message(chat_id, _build_auto_summary(results))
        return

    email_id = queue.pop(0)
    total = context.user_data.get("gmail_auto_total", 1)
    idx = total - len(queue)
    await _process_gmail_auto_item(bot, chat_id, owner_id, context, email_id, idx, total)


async def start_gmail_auto_processing(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    owner_id: int,
    bot,
    email_ids: list[str],
) -> None:
    total = len(email_ids)
    context.user_data["gmail_auto_queue"] = list(email_ids[1:])
    context.user_data["gmail_auto_total"] = total
    context.user_data["gmail_auto_results"] = []
    await bot.send_message(chat_id, f"🔄 Auto-processing {total} Apple invoice(s)…")
    await _process_gmail_auto_item(bot, chat_id, owner_id, context, email_ids[0], 1, total)


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
        "• /invoices [N] — list last N saved invoices (default 10)\n"
        "• /invoices month [YYYY-MM] — monthly summary (default: current month)\n"
        "• /invoices avg [N] — average cost over last N months (default 6)\n\n"
        "All processing is done locally (gemma4:26b)."
    )


def _fmt_currency(val: float, currency: str = "") -> str:
    return f"{val:,.2f} {currency}".strip() if currency else f"{val:,.2f}"


def _month_name(year: int, month: int) -> str:
    return f"{calendar.month_abbr[month]} {year}"


async def _invoices_month(update: Update, owner_id: int, args: list[str]) -> None:
    today = _date.today()
    year, month = today.year, today.month
    if args:
        try:
            year, month = map(int, args[0].split("-"))
        except (ValueError, AttributeError):
            await update.message.reply_text("❌ Use YYYY-MM format, e.g. /invoices month 2026-03")
            return

    invoices = await db.get_invoices_for_month(owner_id, year, month)

    # Month-over-month: fetch previous month
    prev_month = month - 1 or 12
    prev_year = year if month > 1 else year - 1
    prev_invoices = await db.get_invoices_for_month(owner_id, prev_year, prev_month)

    month_label = _month_name(year, month)

    if not invoices:
        prev_label = _month_name(prev_year, prev_month)
        await update.message.reply_text(
            f"📊 Summary: {month_label}\n\nNo invoices found with issue_date in this month.\n"
            f"(Previous month {prev_label}: {len(prev_invoices)} invoice(s))"
        )
        return

    s = build_month_summary(invoices)
    currencies = {inv.get("currency") for inv in invoices if inv.get("currency")}
    cur = next(iter(currencies), "")

    lines = [f"📊 Summary: {month_label}\n"]
    lines.append(f"💰 Total paid:             {_fmt_currency(s['total_actual'], cur)}")
    if s["total_effective"] != s["total_actual"]:
        lines.append(f"📈 Effective monthly cost: {_fmt_currency(s['total_effective'], cur)}")
    lines.append(f"📄 Invoices: {s['invoice_count']}")

    if s["by_category"]:
        lines.append("\n📂 By category:")
        for cat, data in sorted(s["by_category"].items(), key=lambda x: x[1]["actual"], reverse=True):
            line = f"  {cat}: {_fmt_currency(data['actual'], cur)} ({data['count']})"
            if data["effective"] != data["actual"]:
                line += f" → {_fmt_currency(data['effective'], cur)}/mo"
            lines.append(line)

    if s["recurring"]["count"] or s["one_time"]["count"]:
        lines.append("")
        if s["recurring"]["count"]:
            lines.append(f"🔄 Recurring: {_fmt_currency(s['recurring']['actual'], cur)} ({s['recurring']['count']} invoice(s))")
        if s["one_time"]["count"]:
            lines.append(f"⚡ One-time:  {_fmt_currency(s['one_time']['actual'], cur)} ({s['one_time']['count']} invoice(s))")

    if s["top_vendors"]:
        lines.append("\n🏢 Top vendors:")
        for i, v in enumerate(s["top_vendors"], 1):
            lines.append(f"  {i}. {v['vendor']} — {_fmt_currency(v['total'], cur)}")

    # Month-over-month
    if prev_invoices:
        prev_s = build_month_summary(prev_invoices)
        prev_label = _month_name(prev_year, prev_month)
        if prev_s["total_actual"]:
            change_pct = (s["total_actual"] - prev_s["total_actual"]) / prev_s["total_actual"] * 100
            arrow = "+" if change_pct >= 0 else ""
            lines.append(f"\n📉 vs {prev_label}: {arrow}{change_pct:.0f}% actual")

    # Individual invoice list
    lines.append("\n📋 Invoices:")
    for i, inv in enumerate(invoices, 1):
        vendor = inv.get("vendor") or "Unknown"
        total = inv.get("total")
        currency = inv.get("currency") or ""
        cat = inv.get("category") or "—"
        sub = inv.get("subcategory")
        cat_str = f"{cat} › {sub}" if sub else cat
        recurring_mark = " 🔁" if inv.get("recurring") else ""
        notes = inv.get("notes") or ""
        date_str = str(inv.get("issue_date") or "—")[:10]
        lines.append(
            f"\n{i}. {vendor}{recurring_mark}\n"
            f"   Date:     {date_str}\n"
            f"   Amount:   {_fmt_currency(float(total or 0), currency)}\n"
            f"   Category: {cat_str}"
            + (f"\n   Notes:    {notes}" if notes else "")
        )

    await update.message.reply_text("\n".join(lines))


async def _invoices_avg(update: Update, owner_id: int, args: list[str]) -> None:
    n_months = 6
    if args:
        try:
            n_months = max(1, min(int(args[0]), 24))
        except ValueError:
            await update.message.reply_text("❌ Provide a number of months, e.g. /invoices avg 3")
            return

    today = _date.today()
    end_year, end_month = today.year, today.month

    # start date = first day of the earliest month in range
    start_month = end_month - n_months + 1
    start_year = end_year
    while start_month <= 0:
        start_month += 12
        start_year -= 1

    start_date = _date(start_year, start_month, 1)
    # end_date = first day of month after end_month
    if end_month == 12:
        end_date = _date(end_year + 1, 1, 1)
    else:
        end_date = _date(end_year, end_month + 1, 1)

    invoices = await db.get_invoices_for_range(owner_id, start_date, end_date)

    currencies = {inv.get("currency") for inv in invoices if inv.get("currency")}
    cur = next(iter(currencies), "")

    a = build_avg_summary(invoices, n_months, end_year, end_month)

    start_label = _month_name(start_year, start_month)
    end_label = _month_name(end_year, end_month)
    lines = [f"📊 {n_months}-month average ({start_label} – {end_label})\n"]
    lines.append(f"💰 Avg actual spend:   {_fmt_currency(a['avg_actual'], cur)}/mo")
    lines.append(f"📈 Avg effective cost: {_fmt_currency(a['avg_effective'], cur)}/mo")

    if a["by_category"]:
        lines.append("\n📂 Avg by category:")
        for cat, avg_eff in a["by_category"].items():
            lines.append(f"  {cat}: {_fmt_currency(avg_eff, cur)}")

    lines.append("\n📅 Month breakdown:")
    for m in a["months"]:
        label = _month_name(m["year"], m["month"])
        if m["count"] == 0:
            lines.append(f"  {label}: —  (0 invoices)")
        else:
            line = f"  {label}: {_fmt_currency(m['actual'], cur)}"
            if m["effective"] != m["actual"]:
                line += f"  ({_fmt_currency(m['effective'], cur)} eff.)"
            lines.append(line)

    await update.message.reply_text("\n".join(lines))


async def invoices_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    args = context.args or []

    if args and args[0] == "month":
        await _invoices_month(update, owner_id, args[1:])
        return

    if args and args[0] == "avg":
        await _invoices_avg(update, owner_id, args[1:])
        return

    limit = 10
    if args:
        try:
            limit = max(1, min(int(args[0]), 50))
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
    app.add_handler(CallbackQueryHandler(email_body_callback, pattern=r"^inv_body:"))
    app.add_handler(CommandHandler("invoice", invoice_help_cmd))
    app.add_handler(CommandHandler("invoices", invoices_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("scan_stop", scan_stop_cmd))
