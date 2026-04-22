"""Tests for pure parsing and formatting helpers in bot/utils/formatting.py."""


from bot.utils.formatting import (
    _format_macro_progress,
    format_meal_logged,
    parse_servings,
    parse_target,
    parse_time,
    strip_command_args,
)

# ---------------------------------------------------------------------------
# parse_target
# ---------------------------------------------------------------------------

def test_parse_target_name():
    name, is_both = parse_target("chicken salad @Wife at 12:00")
    assert name == "Wife"
    assert not is_both


def test_parse_target_both():
    name, is_both = parse_target("pizza @both")
    assert name is None
    assert is_both


def test_parse_target_both_case_insensitive():
    name, is_both = parse_target("pizza @BOTH")
    assert name is None
    assert is_both


def test_parse_target_none():
    name, is_both = parse_target("chicken salad no target here")
    assert name is None
    assert not is_both


# ---------------------------------------------------------------------------
# parse_time
# ---------------------------------------------------------------------------

def test_parse_time_found():
    dt = parse_time("lunch at 13:30")
    assert dt is not None
    assert dt.hour == 13
    assert dt.minute == 30


def test_parse_time_midnight():
    dt = parse_time("snack at 00:00")
    assert dt is not None
    assert dt.hour == 0
    assert dt.minute == 0


def test_parse_time_not_found():
    assert parse_time("just some text without a time") is None


def test_parse_time_invalid_hour_returns_none():
    assert parse_time("meal at 25:00") is None


def test_parse_time_case_insensitive():
    dt = parse_time("dinner AT 20:00")
    assert dt is not None
    assert dt.hour == 20


# ---------------------------------------------------------------------------
# parse_servings
# ---------------------------------------------------------------------------

def test_parse_servings_found():
    assert parse_servings("pasta for 2") == 2


def test_parse_servings_not_found():
    assert parse_servings("pasta no servings info") is None


def test_parse_servings_for_one():
    assert parse_servings("recipe for 1") == 1


# ---------------------------------------------------------------------------
# strip_command_args
# ---------------------------------------------------------------------------

def test_strip_command_args_removes_all():
    result = strip_command_args("chicken salad @Wife at 13:00 for 2")
    assert result == "chicken salad"


def test_strip_command_args_no_extras():
    assert strip_command_args("scrambled eggs") == "scrambled eggs"


def test_strip_command_args_collapses_whitespace():
    result = strip_command_args("  oats   with   milk  ")
    assert result == "oats with milk"


def test_strip_command_args_only_target():
    assert strip_command_args("salad @Me") == "salad"


# ---------------------------------------------------------------------------
# _format_macro_progress
# ---------------------------------------------------------------------------

def test_format_macro_progress_no_goal():
    assert _format_macro_progress("P", 50.0, None) == "P: 50g"


def test_format_macro_progress_zero_goal():
    assert _format_macro_progress("P", 50.0, 0) == "P: 50g"


def test_format_macro_progress_with_goal():
    assert _format_macro_progress("P", 50.0, 150.0) == "P: 50 / 150g"


def test_format_macro_progress_integer_display():
    assert _format_macro_progress("C", 200.0, 250.0) == "C: 200 / 250g"


# ---------------------------------------------------------------------------
# format_meal_logged — over/under goal logic
# ---------------------------------------------------------------------------

_DAILY_TOTAL = {"calories": 1800, "protein_g": 90.0, "carbs_g": 200.0, "fat_g": 60.0}
_GOAL_NO_MACROS = {"daily_calories": 2000, "daily_protein_g": None, "daily_carbs_g": None, "daily_fat_g": None}
_GOAL_WITH_MACROS = {"daily_calories": 2000, "daily_protein_g": 150.0, "daily_carbs_g": 250.0, "daily_fat_g": 65.0}


def test_format_meal_logged_under_goal():
    text = format_meal_logged(
        profile_name="Me",
        description="oatmeal",
        cals=350,
        protein=12.0,
        carbs=55.0,
        fat=7.0,
        daily_total=_DAILY_TOTAL,
        goal=_GOAL_NO_MACROS,
    )
    assert "200 remaining" in text
    assert "[Me]" in text
    assert "oatmeal" in text


def test_format_meal_logged_over_goal():
    over_total = {"calories": 2200, "protein_g": 90.0, "carbs_g": 200.0, "fat_g": 60.0}
    text = format_meal_logged(
        profile_name="Me",
        description="burger",
        cals=800,
        protein=40.0,
        carbs=50.0,
        fat=35.0,
        daily_total=over_total,
        goal=_GOAL_NO_MACROS,
    )
    assert "200 over goal" in text


def test_format_meal_logged_macro_goals_shown():
    text = format_meal_logged(
        profile_name="Me",
        description="chicken",
        cals=250,
        protein=45.0,
        carbs=0.0,
        fat=5.0,
        daily_total=_DAILY_TOTAL,
        goal=_GOAL_WITH_MACROS,
    )
    assert "/ 150g" in text  # protein goal shown
    assert "/ 250g" in text  # carbs goal shown
