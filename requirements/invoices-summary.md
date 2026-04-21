# Invoice Monthly Summary & Average Commands

## Context
The invoices module can scan, process, and list invoices but has no aggregation/analytics. Adding `/invoices month` and `/invoices avg` subcommands with category breakdown, recurring vs one-time split, top vendors, month-over-month trend, and amortized annual costs (e.g. annual insurance contributes `total / billing_period_months` to each month's effective cost).

## New Field: `billing_period_months`
Add INT column (DEFAULT 1) to `invoices`. LLM extracts it:
- `1` = monthly recurring or one-time
- `3` = quarterly
- `12` = annual

Enables: `effective_monthly_cost = total / billing_period_months`.

---

## Files Changed

| File | Change |
|------|--------|
| `alembic/versions/0007_invoices_add_billing_period.py` | New migration |
| `bot/modules/invoices/agents/invoice_reader.md` | Add `billing_period_months` to JSON schema |
| `bot/services/db.py` | Update `log_invoice`, `list_invoices`; add `get_invoices_for_month`, `get_invoices_for_range` |
| `bot/modules/invoices/services/summary.py` | New — aggregation logic |
| `bot/modules/invoices/handlers/invoices.py` | Add `month`/`avg` subcommand dispatch |
| `README.md` | Docs update |

---

## Commands

**`/invoices month [YYYY-MM]`** — summary for a specific month (default: current month)

**`/invoices avg [N]`** — average over last N months (default: 6, includes months with zero invoices in denominator)

---

## Amortization
For invoices with `billing_period_months > 1`, the effective monthly contribution = `total / billing_period_months`. Used in:
- The "Effective monthly cost" line in `/invoices month`
- The per-month effective values in `/invoices avg`
- Avg calculation: `sum(effective per month) / N`
