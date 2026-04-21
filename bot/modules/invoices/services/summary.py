"""Aggregation helpers for invoice monthly summaries and averages."""
from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import date


def _effective(invoice: dict) -> float:
    total = float(invoice.get("total") or 0)
    period = int(invoice.get("billing_period_months") or 1)
    if period not in (1, 3, 12):
        period = 1
    return total / period


def build_month_summary(invoices: list[dict]) -> dict:
    total_actual = 0.0
    total_effective = 0.0
    by_category: dict[str, dict] = defaultdict(lambda: {"actual": 0.0, "effective": 0.0, "count": 0})
    recurring_actual = 0.0
    recurring_count = 0
    one_time_actual = 0.0
    one_time_count = 0
    vendor_totals: dict[str, float] = defaultdict(float)
    vendor_counts: dict[str, int] = defaultdict(int)

    for inv in invoices:
        actual = float(inv.get("total") or 0)
        eff = _effective(inv)
        cat = inv.get("category") or "other"

        total_actual += actual
        total_effective += eff
        by_category[cat]["actual"] += actual
        by_category[cat]["effective"] += eff
        by_category[cat]["count"] += 1

        if inv.get("recurring"):
            recurring_actual += actual
            recurring_count += 1
        else:
            one_time_actual += actual
            one_time_count += 1

        vendor = inv.get("vendor") or "Unknown"
        vendor_totals[vendor] += actual
        vendor_counts[vendor] += 1

    top_vendors = sorted(
        [{"vendor": v, "total": vendor_totals[v], "count": vendor_counts[v]} for v in vendor_totals],
        key=lambda x: x["total"],
        reverse=True,
    )[:5]

    return {
        "total_actual": total_actual,
        "total_effective": total_effective,
        "by_category": dict(by_category),
        "recurring": {"actual": recurring_actual, "count": recurring_count},
        "one_time": {"actual": one_time_actual, "count": one_time_count},
        "top_vendors": top_vendors,
        "invoice_count": len(invoices),
    }


def build_avg_summary(invoices: list[dict], n_months: int, end_year: int, end_month: int) -> dict:
    """Aggregate invoices over n_months ending at (end_year, end_month) inclusive."""
    months: list[tuple[int, int]] = []
    y, m = end_year, end_month
    for _ in range(n_months):
        months.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    months.reverse()

    per_month: dict[tuple[int, int], dict] = {
        ym: {"actual": 0.0, "effective": 0.0, "count": 0} for ym in months
    }
    by_category: dict[str, float] = defaultdict(float)

    for inv in invoices:
        issue = inv.get("issue_date")
        if not issue:
            continue
        if isinstance(issue, str):
            try:
                issue = date.fromisoformat(issue[:10])
            except ValueError:
                continue
        ym = (issue.year, issue.month)
        if ym not in per_month:
            continue
        actual = float(inv.get("total") or 0)
        eff = _effective(inv)
        per_month[ym]["actual"] += actual
        per_month[ym]["effective"] += eff
        per_month[ym]["count"] += 1
        cat = inv.get("category") or "other"
        by_category[cat] += eff

    total_effective = sum(v["effective"] for v in per_month.values())
    total_actual = sum(v["actual"] for v in per_month.values())

    return {
        "avg_actual": total_actual / n_months,
        "avg_effective": total_effective / n_months,
        "months": [
            {"year": y, "month": m, **per_month[(y, m)]}
            for y, m in months
        ],
        "by_category": {
            cat: total / n_months
            for cat, total in sorted(by_category.items(), key=lambda x: x[1], reverse=True)
        },
        "n_months": n_months,
    }
