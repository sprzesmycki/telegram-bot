"""Tests for invoice aggregation helpers in bot/modules/invoices/services/summary.py."""

from bot.modules.invoices.services.summary import (
    _effective,
    build_avg_summary,
    build_month_summary,
)


# ---------------------------------------------------------------------------
# _effective
# ---------------------------------------------------------------------------


def test_effective_monthly():
    assert _effective({"total": 120.0, "billing_period_months": 1}) == 120.0


def test_effective_quarterly():
    assert _effective({"total": 120.0, "billing_period_months": 3}) == 40.0


def test_effective_annual():
    assert _effective({"total": 120.0, "billing_period_months": 12}) == 10.0


def test_effective_invalid_period_defaults_to_1():
    assert _effective({"total": 120.0, "billing_period_months": 7}) == 120.0


def test_effective_missing_period_defaults_to_1():
    assert _effective({"total": 60.0}) == 60.0


def test_effective_zero_total():
    assert _effective({"total": 0, "billing_period_months": 12}) == 0.0


# ---------------------------------------------------------------------------
# build_month_summary
# ---------------------------------------------------------------------------

_INV_MONTHLY = {"total": 100.0, "billing_period_months": 1, "category": "software", "recurring": True, "vendor": "GitHub"}
_INV_ANNUAL = {"total": 120.0, "billing_period_months": 12, "category": "software", "recurring": True, "vendor": "JetBrains"}
_INV_ONETIME = {"total": 50.0, "billing_period_months": 1, "category": "hardware", "recurring": False, "vendor": "Amazon"}


def test_build_month_summary_totals():
    result = build_month_summary([_INV_MONTHLY, _INV_ONETIME])
    assert result["total_actual"] == 150.0
    assert result["invoice_count"] == 2


def test_build_month_summary_effective_uses_billing_period():
    result = build_month_summary([_INV_ANNUAL])
    assert result["total_actual"] == 120.0
    assert result["total_effective"] == 10.0   # 120 / 12


def test_build_month_summary_recurring_split():
    result = build_month_summary([_INV_MONTHLY, _INV_ONETIME])
    assert result["recurring"]["count"] == 1
    assert result["recurring"]["actual"] == 100.0
    assert result["one_time"]["count"] == 1
    assert result["one_time"]["actual"] == 50.0


def test_build_month_summary_by_category():
    result = build_month_summary([_INV_MONTHLY, _INV_ONETIME])
    assert "software" in result["by_category"]
    assert "hardware" in result["by_category"]
    assert result["by_category"]["software"]["count"] == 1


def test_build_month_summary_top_vendors():
    result = build_month_summary([_INV_MONTHLY, _INV_ONETIME])
    vendors = [v["vendor"] for v in result["top_vendors"]]
    assert "GitHub" in vendors
    assert "Amazon" in vendors


def test_build_month_summary_top_vendors_sorted_by_total():
    result = build_month_summary([_INV_MONTHLY, _INV_ONETIME])
    totals = [v["total"] for v in result["top_vendors"]]
    assert totals == sorted(totals, reverse=True)


def test_build_month_summary_empty():
    result = build_month_summary([])
    assert result["total_actual"] == 0.0
    assert result["invoice_count"] == 0
    assert result["top_vendors"] == []


def test_build_month_summary_with_subscriptions():
    sub = {"amount": 10.0, "billing_period_months": 1, "category": "subscriptions", "name": "Netflix"}
    result = build_month_summary([], subscriptions=[sub])
    assert result["subscription_count"] == 1
    assert result["total_actual"] == 10.0
    assert result["recurring"]["count"] == 1


# ---------------------------------------------------------------------------
# build_avg_summary
# ---------------------------------------------------------------------------

_INV_2026_03 = {"total": 90.0, "billing_period_months": 1, "category": "software", "issue_date": "2026-03-15"}
_INV_2026_04 = {"total": 60.0, "billing_period_months": 1, "category": "software", "issue_date": "2026-04-10"}


def test_build_avg_summary_basic():
    result = build_avg_summary([_INV_2026_03, _INV_2026_04], n_months=2, end_year=2026, end_month=4)
    assert result["avg_actual"] == 75.0   # (90 + 60) / 2
    assert result["n_months"] == 2


def test_build_avg_summary_month_range():
    result = build_avg_summary([], n_months=3, end_year=2026, end_month=4)
    months = [(m["year"], m["month"]) for m in result["months"]]
    assert (2026, 2) in months
    assert (2026, 3) in months
    assert (2026, 4) in months


def test_build_avg_summary_year_boundary():
    result = build_avg_summary([], n_months=3, end_year=2026, end_month=1)
    months = [(m["year"], m["month"]) for m in result["months"]]
    assert (2025, 11) in months
    assert (2025, 12) in months
    assert (2026, 1) in months


def test_build_avg_summary_invoice_outside_range_ignored():
    inv_old = {"total": 999.0, "billing_period_months": 1, "category": "other", "issue_date": "2025-01-01"}
    result = build_avg_summary([inv_old], n_months=2, end_year=2026, end_month=4)
    assert result["avg_actual"] == 0.0


def test_build_avg_summary_by_category():
    result = build_avg_summary([_INV_2026_03, _INV_2026_04], n_months=2, end_year=2026, end_month=4)
    assert "software" in result["by_category"]
    # (90+60) effective / 2 months = 75.0 per month
    assert result["by_category"]["software"] == 75.0


def test_build_avg_summary_empty():
    result = build_avg_summary([], n_months=3, end_year=2026, end_month=4)
    assert result["avg_actual"] == 0.0
    assert len(result["months"]) == 3
