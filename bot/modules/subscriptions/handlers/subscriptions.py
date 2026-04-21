"""Subscription management handlers (/sub command)."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from bot.services import db

logger = logging.getLogger(__name__)

_PERIOD_MAP = {
    "monthly": 1,
    "quarterly": 3,
    "yearly": 12,
    "annual": 12,
    "annually": 12,
}

_PERIOD_LABEL = {1: "monthly", 3: "quarterly", 12: "yearly"}

_HELP = (
    "🔔 Subscriptions\n\n"
    "/sub add <name> <amount> [monthly|quarterly|yearly]\n"
    "/sub list [all]\n"
    "/sub update <id> <new_amount>\n"
    "/sub disable <id>\n"
    "/sub enable <id>\n\n"
    "Default currency: PLN. Default period: monthly.\n"
    "Examples:\n"
    "  /sub add Netflix 45.99\n"
    "  /sub add HBO Max 49.99 monthly\n"
    "  /sub add Amazon Prime 299 yearly"
)


def _parse_add_args(args: list[str]) -> tuple[str, float, int] | None:
    """Parse: name amount [period]  →  (name, amount, billing_period_months)."""
    if len(args) < 2:
        return None

    period = 1
    end = len(args)
    if args[-1].lower() in _PERIOD_MAP:
        period = _PERIOD_MAP[args[-1].lower()]
        end -= 1

    if end < 2:
        return None

    try:
        amount = float(args[end - 1].replace(",", "."))
    except ValueError:
        return None

    name = " ".join(args[: end - 1]).strip()
    if not name or amount <= 0:
        return None

    return name, amount, period


def _fmt_amount(amount, currency: str = "PLN") -> str:
    return f"{float(amount):.2f} {currency}"


async def sub_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    args = context.args or []

    if not args:
        await update.message.reply_text(_HELP)
        return

    sub = args[0].lower()

    if sub == "add":
        await _sub_add(update, owner_id, args[1:])
    elif sub == "list":
        await _sub_list(update, owner_id, args[1:])
    elif sub == "update":
        await _sub_update(update, owner_id, args[1:])
    elif sub == "disable":
        await _sub_toggle(update, owner_id, args[1:], active=False)
    elif sub == "enable":
        await _sub_toggle(update, owner_id, args[1:], active=True)
    else:
        await update.message.reply_text(_HELP)


async def _sub_add(update: Update, owner_id: int, args: list[str]) -> None:
    parsed = _parse_add_args(args)
    if not parsed:
        await update.message.reply_text(
            "❌ Usage: /sub add <name> <amount> [monthly|quarterly|yearly]\n"
            "Example: /sub add Netflix 45.99"
        )
        return

    name, amount, period = parsed
    sub_id = await db.create_subscription(
        owner_id=owner_id,
        name=name,
        vendor=None,
        category="subscriptions",
        subcategory=None,
        amount=amount,
        currency="PLN",
        billing_period_months=period,
        notes=None,
    )
    period_label = _PERIOD_LABEL.get(period, f"every {period} months")
    await update.message.reply_text(
        f"✅ Subscription added (#{sub_id})\n"
        f"  {name} — {_fmt_amount(amount)} {period_label}"
    )


async def _sub_list(update: Update, owner_id: int, args: list[str]) -> None:
    active_only = not (args and args[0].lower() == "all")
    rows = await db.list_subscriptions(owner_id, active_only=active_only)

    if not rows:
        msg = "No active subscriptions." if active_only else "No subscriptions found."
        await update.message.reply_text(msg)
        return

    label = "Active subscriptions" if active_only else "All subscriptions"
    lines = [f"🔔 {label}:\n"]
    for row in rows:
        period_label = _PERIOD_LABEL.get(row["billing_period_months"], f"/{row['billing_period_months']}mo")
        status = "" if row["active"] else "  [inactive]"
        start = str(row.get("start_date") or "")[:10]
        lines.append(
            f"#{row['id']}  {row['name']}  {_fmt_amount(row['amount'], row['currency'])} {period_label}"
            f"  (from {start}){status}"
        )

    await update.message.reply_text("\n".join(lines))


async def _sub_update(update: Update, owner_id: int, args: list[str]) -> None:
    if len(args) < 2:
        await update.message.reply_text("❌ Usage: /sub update <id> <new_amount>")
        return

    try:
        sub_id = int(args[0])
        new_amount = float(args[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("❌ Usage: /sub update <id> <new_amount>")
        return

    old = await db.get_subscription(sub_id, owner_id)
    if not old:
        await update.message.reply_text(f"❌ Subscription #{sub_id} not found.")
        return

    new_id = await db.update_subscription_price(owner_id, sub_id, new_amount)
    old_amount = float(old["amount"])
    await update.message.reply_text(
        f"✅ Price updated for {old['name']}\n"
        f"  {_fmt_amount(old_amount, old['currency'])} → {_fmt_amount(new_amount, old['currency'])}\n"
        f"  Old entry #{sub_id} deactivated. New entry #{new_id} active from today."
    )


async def _sub_toggle(update: Update, owner_id: int, args: list[str], *, active: bool) -> None:
    if not args:
        action = "enable" if active else "disable"
        await update.message.reply_text(f"❌ Usage: /sub {action} <id>")
        return

    try:
        sub_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Provide a numeric subscription ID.")
        return

    found = await db.set_subscription_active(owner_id, sub_id, active)
    if not found:
        await update.message.reply_text(f"❌ Subscription #{sub_id} not found.")
        return

    action = "enabled" if active else "disabled"
    await update.message.reply_text(f"✅ Subscription #{sub_id} {action}.")


COMMANDS: list[tuple[str, str]] = [
    ("sub", "Manage subscriptions (add/list/update/disable/enable)"),
]


def register(app: Application) -> None:
    app.add_handler(CommandHandler("sub", sub_cmd))
