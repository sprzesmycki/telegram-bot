"""Tests for invoice billing-period math, summary aggregation, and formatting."""
import pytest

from bot.modules.calories.scheduled import _parse_schedule_time
from bot.modules.invoices.handlers.invoices import _fmt_amount
from bot.modules.invoices.services.summary import (
    _effective,
    _effective_sub,
    build_month_summary,
)

# ---------------------------------------------------------------------------
# _parse_schedule_time
# ---------------------------------------------------------------------------

def test_parse_schedule_time_valid():
    assert _parse_schedule_time("21:00", "x") == (21, 0)


def test_parse_schedule_time_midnight():
    assert _parse_schedule_time("00:00", "x") == (0, 0)


def test_parse_schedule_time_end_of_day():
    assert _parse_schedule_time("23:59", "x") == (23, 59)


def test_parse_schedule_time_leading_zero():
    assert _parse_schedule_time("09:05", "x") == (9, 5)


def test_parse_schedule_time_invalid_hour():
    with pytest.raises(ValueError, match="invalid_summary_time"):
        _parse_schedule_time("25:00", "invalid_summary_time")


def test_parse_schedule_time_invalid_minute():
    with pytest.raises(ValueError):
        _parse_schedule_time("21:60", "x")


def test_parse_schedule_time_bad_format():
    with pytest.raises(ValueError):
        _parse_schedule_time("2100", "x")


def test_parse_schedule_time_empty():
    with pytest.raises(ValueError):
        _parse_schedule_time("", "x")


# ---------------------------------------------------------------------------
# _effective / _effective_sub
# ---------------------------------------------------------------------------

def test_effective_monthly():
    assert _effective({"total": 120.0, "billing_period_months": 1}) == pytest.approx(120.0)


def test_effective_quarterly():
    assert _effective({"total": 120.0, "billing_period_months": 3}) == pytest.approx(40.0)


def test_effective_annual():
    assert _effective({"total": 120.0, "billing_period_months": 12}) == pytest.approx(10.0)


def test_effective_invalid_period_falls_back_to_monthly():
    assert _effective({"total": 120.0, "billing_period_months": 6}) == pytest.approx(120.0)


def test_effective_none_total_is_zero():
    assert _effective({"total": None, "billing_period_months": 1}) == pytest.approx(0.0)


def test_effective_missing_period_defaults_to_1():
    assert _effective({"total": 100.0}) == pytest.approx(100.0)


def test_effective_sub_annual():
    assert _effective_sub({"amount": 120.0, "billing_period_months": 12}) == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# build_month_summary
# ---------------------------------------------------------------------------

def test_build_month_summary_empty():
    s = build_month_summary([])
    assert s["total_actual"] == 0.0
    assert s["total_effective"] == 0.0
    assert s["invoice_count"] == 0
    assert s["subscription_count"] == 0


def test_build_month_summary_single_monthly():
    inv = {
        "total": 100.0,
        "billing_period_months": 1,
        "category": "utilities",
        "recurring": False,
        "vendor": "Enea",
    }
    s = build_month_summary([inv])
    assert s["total_actual"] == pytest.approx(100.0)
    assert s["total_effective"] == pytest.approx(100.0)
    assert s["invoice_count"] == 1
    assert s["one_time"]["count"] == 1
    assert s["recurring"]["count"] == 0


def test_build_month_summary_quarterly_effective():
    inv = {
        "total": 300.0,
        "billing_period_months": 3,
        "category": "insurance",
        "recurring": True,
        "vendor": "PZU",
    }
    s = build_month_summary([inv])
    assert s["total_actual"] == pytest.approx(300.0)
    assert s["total_effective"] == pytest.approx(100.0)
    assert s["recurring"]["count"] == 1


def test_build_month_summary_top_vendors_ordering():
    invoices = [
        {"total": 100.0, "billing_period_months": 1, "category": "food", "recurring": False, "vendor": "A"},
        {"total": 500.0, "billing_period_months": 1, "category": "equipment", "recurring": False, "vendor": "B"},
    ]
    s = build_month_summary(invoices)
    assert s["top_vendors"][0]["vendor"] == "B"
    assert s["top_vendors"][1]["vendor"] == "A"


def test_build_month_summary_category_grouping():
    invoices = [
        {"total": 200.0, "billing_period_months": 1, "category": "utilities", "recurring": True, "vendor": "X"},
        {"total": 100.0, "billing_period_months": 1, "category": "utilities", "recurring": True, "vendor": "Y"},
    ]
    s = build_month_summary(invoices)
    assert s["by_category"]["utilities"]["actual"] == pytest.approx(300.0)
    assert s["by_category"]["utilities"]["count"] == 2


def test_build_month_summary_with_subscriptions():
    subs = [{"amount": 60.0, "billing_period_months": 1, "category": "software", "name": "Spotify"}]
    s = build_month_summary([], subscriptions=subs)
    assert s["total_actual"] == pytest.approx(60.0)
    assert s["subscription_count"] == 1
    assert s["recurring"]["count"] == 1


# ---------------------------------------------------------------------------
# _fmt_amount
# ---------------------------------------------------------------------------

def test_fmt_amount_basic():
    assert _fmt_amount(1234.5, "PLN") == "1,234.50 PLN"


def test_fmt_amount_none_returns_dash():
    assert _fmt_amount(None, "PLN") == "—"


def test_fmt_amount_empty_currency():
    assert _fmt_amount(100.0, "") == "100.00"


def test_fmt_amount_zero():
    assert _fmt_amount(0, "EUR") == "0.00 EUR"


def test_fmt_amount_string_number():
    assert _fmt_amount("99.9", "USD") == "99.90 USD"
