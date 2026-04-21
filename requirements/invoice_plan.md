# Invoice Processing Feature Plan

## Phase 1 — User-provided invoices

### Flow
1. User sends a **photo with caption `/invoice`** or a **PDF document**
2. Bot downloads the file to `./data/invoices/tmp/<uuid>.<ext>`
3. Local LLM (`gemma4:26b` via Ollama) reads it — photo as vision, PDF as extracted text
4. Bot replies with a **preview card** and two inline buttons:
   - ✅ **Save** — renames file to `<Vendor>_<YYYY-MM-DD>.<ext>`, writes row to `invoices` table
   - ❌ **Discard** — deletes tmp file, removes pending row
5. Pending state persists in `pending_invoices` DB table (survives bot restart)

### File naming
`./data/invoices/<Vendor_Name>_<YYYY-MM-DD>.<ext>`
- Vendor sanitized: non-word chars → `_`, truncated to 40 chars
- If date missing: today's date
- If vendor missing: `Unknown`
- Collision: append `_2`, `_3`, …

### Preview card format
```
🧾 Invoice Preview

Vendor:      Allegro
Invoice #:   FV/2026/04/12345
Issue date:  2026-04-15
Due date:    2026-04-30
Category:    software

Subtotal:    45.93 PLN
Tax:          4.07 PLN
Total:       50.00 PLN

Line items:
  • Subscription × 1 — 45.93 PLN

Notes:  —

Is this correct?
[✅ Save] [❌ Discard]
```

### PDF handling
`pypdf` for text extraction (no system deps). If the PDF has no text layer (scanned), the bot asks the user to send a photo instead.

---

## DB — one Alembic migration (0004)

### `pending_invoices`
```sql
id              SERIAL PRIMARY KEY,
owner_user_id   BIGINT NOT NULL,
tmp_file_path   TEXT NOT NULL,
parsed          JSONB NOT NULL,
created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
```
Rows older than 24 h are deleted at bot startup.

### `invoices`
```sql
id                  SERIAL PRIMARY KEY,
owner_user_id       BIGINT NOT NULL,
vendor              TEXT,
invoice_number      TEXT,
issue_date          DATE,
due_date            DATE,
currency            TEXT,
subtotal            NUMERIC(14, 2),
tax                 NUMERIC(14, 2),
total               NUMERIC(14, 2),
category            TEXT,
line_items          JSONB NOT NULL DEFAULT '[]',
notes               TEXT,
source              TEXT NOT NULL DEFAULT 'manual',  -- 'manual' | 'gmail'
gmail_message_id    TEXT,
file_path           TEXT,
created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
```

---

## Files created / modified

| File | Action |
|------|--------|
| `alembic/versions/0004_invoices_schema.py` | New migration |
| `bot/services/db.py` | Add invoice DB functions |
| `bot/modules/invoices/handlers/invoices.py` | Full implementation (replaces stub) |
| `bot/modules/invoices/__init__.py` | Add stale-pending cleanup on startup |
| `pyproject.toml` | Add `pypdf>=4.0` |
| `config.yaml` | Set `modules.invoices.enabled: true` |
| `README.md` | Update commands table and module section |

---

## DB functions added to `bot/services/db.py`

- `create_pending_invoice(owner_id, tmp_file_path, parsed) -> int`
- `get_pending_invoice(pending_id, owner_id) -> dict | None`
- `delete_pending_invoice(pending_id) -> None`
- `cleanup_stale_pending_invoices(max_age_hours=24) -> list[str]` — returns tmp paths
- `log_invoice(owner_id, parsed, file_path, source, gmail_message_id) -> int`
- `list_invoices(owner_id, limit) -> list[dict]`
- `delete_invoice(owner_id, invoice_id) -> bool`

---

## Handler registration detail

- Invoice photo handler uses PTB group `-1` so it fires **before** the calories photo handler (group `0`). It raises `ApplicationHandlerStop` to prevent calories from also processing the image.
- Filter: `filters.PHOTO & filters.CaptionRegex(r"(?i)^/invoice")`
- PDF documents use `filters.Document.PDF` — no conflict with calories.

---

## Phase 2 — Gmail integration (planned, not implemented yet)

When an email fetched by the Gmail module has a PDF attachment already saved locally, an inline **"Process as invoice"** button will appear in the email preview card. The callback runs the same analysis pipeline with `source='gmail'` and `gmail_message_id` set.
