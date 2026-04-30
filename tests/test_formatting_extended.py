"""Tests for the remaining pure helpers in bot/utils/formatting.py."""

from datetime import datetime
from zoneinfo import ZoneInfo

from bot.utils.formatting import (
    _format_eaten_at,
    format_liquid_logged,
    format_profile_list,
    format_supplement_list,
    format_summary,
    format_week,
)

_WARSAW = ZoneInfo("Europe/Warsaw")
_UTC = ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# _format_eaten_at
# ---------------------------------------------------------------------------


def test_format_eaten_at_aware_utc_converts_to_warsaw():
    # 12:00 UTC = 14:00 Warsaw (CEST, UTC+2)
    dt = datetime(2026, 6, 15, 12, 0, 0, tzinfo=_UTC)
    assert _format_eaten_at(dt) == "14:00"


def test_format_eaten_at_aware_warsaw_unchanged():
    dt = datetime(2026, 6, 15, 14, 0, 0, tzinfo=_WARSAW)
    assert _format_eaten_at(dt) == "14:00"


def test_format_eaten_at_naive_datetime():
    dt = datetime(2026, 6, 15, 9, 30)
    assert _format_eaten_at(dt) == "09:30"


def test_format_eaten_at_iso_string():
    assert _format_eaten_at("2026-06-15T09:30:00") == "09:30"


def test_format_eaten_at_iso_string_with_microseconds():
    assert _format_eaten_at("2026-06-15 09:30:00.000000") == "09:30"


def test_format_eaten_at_short_string_passthrough():
    assert _format_eaten_at("09:30") == "09:30"


# ---------------------------------------------------------------------------
# format_liquid_logged
# ---------------------------------------------------------------------------

_LIQUID_TOTAL = {"calories": 1500, "protein_g": 60.0, "carbs_g": 150.0, "fat_g": 50.0}
_GOAL = {"daily_calories": 2000, "daily_protein_g": None, "daily_carbs_g": None, "daily_fat_g": None}


def test_format_liquid_logged_under_goal():
    result = format_liquid_logged(
        profile_name="Me",
        description="coffee",
        amount_ml=250,
        cals=5,
        protein=0.0,
        carbs=1.0,
        fat=0.0,
        daily_total=_LIQUID_TOTAL,
        goal=_GOAL,
        hydration_ml=750,
    )
    assert "[Me]" in result
    assert "coffee" in result
    assert "250ml" in result
    assert "750 ml" in result
    assert "500 remaining" in result


def test_format_liquid_logged_over_goal():
    over_total = {"calories": 2300, "protein_g": 60.0, "carbs_g": 150.0, "fat_g": 50.0}
    result = format_liquid_logged(
        profile_name="Me",
        description="juice",
        amount_ml=500,
        cals=200,
        protein=0.0,
        carbs=40.0,
        fat=0.0,
        daily_total=over_total,
        goal=_GOAL,
        hydration_ml=500,
    )
    assert "300 over goal" in result


def test_format_liquid_logged_hydration_line():
    result = format_liquid_logged(
        "Me", "water", 500, 0, 0.0, 0.0, 0.0, _LIQUID_TOTAL, _GOAL, 1500,
    )
    assert "Daily Hydration: 1500 ml" in result


# ---------------------------------------------------------------------------
# format_summary
# ---------------------------------------------------------------------------

_SUMMARY_GOAL = {"daily_calories": 2000, "daily_protein_g": 150.0, "daily_carbs_g": 200.0, "daily_fat_g": 65.0}
_SUMMARY_TOTAL = {"calories": 1800, "protein_g": 120.0, "carbs_g": 180.0, "fat_g": 55.0}


def test_format_summary_no_food_no_drinks():
    result = format_summary("Me", [], [], _SUMMARY_TOTAL, _SUMMARY_GOAL, 500)
    assert "Daily Summary for Me" in result
    assert "Hydration: 500 ml" in result
    assert "1800 / 2000 kcal" in result


def test_format_summary_with_meals():
    meals = [{"eaten_at": "2026-06-15T12:00:00", "description": "oatmeal", "calories": 350}]
    result = format_summary("Me", meals, [], _SUMMARY_TOTAL, _SUMMARY_GOAL, 300)
    assert "oatmeal" in result
    assert "350 kcal" in result
    assert "--- Food ---" in result


def test_format_summary_with_drinks():
    liquids = [{"drunk_at": "2026-06-15T08:00:00", "description": "coffee", "amount_ml": 200, "calories": 5}]
    result = format_summary("Me", [], liquids, _SUMMARY_TOTAL, _SUMMARY_GOAL, 200)
    assert "coffee" in result
    assert "200ml" in result
    assert "--- Drinks ---" in result


def test_format_summary_macro_progress():
    result = format_summary("Me", [], [], _SUMMARY_TOTAL, _SUMMARY_GOAL, 0)
    assert "120" in result   # protein actual
    assert "150" in result   # protein goal


# ---------------------------------------------------------------------------
# format_week
# ---------------------------------------------------------------------------

_WEEK_GOAL = {"daily_calories": 2000}


def test_format_week_header():
    result = format_week("Me", [], _WEEK_GOAL)
    assert "Weekly Summary for Me" in result


def test_format_week_over_goal():
    days = [{"date": "2026-06-15", "calories": 2300}]
    result = format_week("Me", days, _WEEK_GOAL)
    assert "300 over goal" in result


def test_format_week_under_goal():
    days = [{"date": "2026-06-15", "calories": 1600}]
    result = format_week("Me", days, _WEEK_GOAL)
    assert "400 under goal" in result


def test_format_week_on_goal():
    days = [{"date": "2026-06-15", "calories": 2000}]
    result = format_week("Me", days, _WEEK_GOAL)
    assert "on goal" in result


def test_format_week_average():
    days = [
        {"date": "2026-06-14", "calories": 1800},
        {"date": "2026-06-15", "calories": 2200},
    ]
    result = format_week("Me", days, _WEEK_GOAL)
    assert "2000 kcal/day" in result


# ---------------------------------------------------------------------------
# format_profile_list
# ---------------------------------------------------------------------------


def test_format_profile_list_marks_active():
    profiles = [
        {"id": 1, "name": "Me"},
        {"id": 2, "name": "Wife"},
    ]
    result = format_profile_list(profiles, active_id=1)
    assert "✓" in result or "✓" in result
    lines = result.splitlines()
    active_line = next(l for l in lines if "Me" in l)
    assert "✓" in active_line


def test_format_profile_list_no_active():
    profiles = [{"id": 1, "name": "Me"}]
    result = format_profile_list(profiles, active_id=None)
    assert "✓" not in result


def test_format_profile_list_all_profiles_present():
    profiles = [{"id": 1, "name": "Me"}, {"id": 2, "name": "Wife"}]
    result = format_profile_list(profiles, active_id=2)
    assert "Me" in result
    assert "Wife" in result


# ---------------------------------------------------------------------------
# format_supplement_list
# ---------------------------------------------------------------------------


def test_format_supplement_list_empty():
    result = format_supplement_list([])
    assert "Supplements:" in result


def test_format_supplement_list_with_items():
    sups = [
        {"name": "Vitamin D", "dose": "2000 IU", "reminder_time": "08:00"},
        {"name": "Magnesium", "dose": None, "reminder_time": "21:00"},
    ]
    result = format_supplement_list(sups)
    assert "Vitamin D" in result
    assert "2000 IU" in result
    assert "Magnesium" in result
    assert "21:00" in result


def test_format_supplement_list_no_dose_no_parens():
    sups = [{"name": "Zinc", "dose": None, "reminder_time": "09:00"}]
    result = format_supplement_list(sups)
    assert "()" not in result
