"""Tests for coach.format_streak."""

from datetime import date, timedelta

from bot.modules.piano.services.coach import format_streak


def test_no_streak():
    assert format_streak(0) == "No active streak — today is a good day to start."


def test_day_one():
    result = format_streak(1)
    assert "Day 1" in result
    assert "nice start" in result


def test_multi_day():
    result = format_streak(14)
    assert "Day 14" in result
    assert "in a row" in result


def test_with_minutes():
    result = format_streak(5, streak_minutes=120)
    assert "120 min this streak" in result


def test_zero_minutes_not_shown():
    result = format_streak(5, streak_minutes=0)
    assert "min this streak" not in result


def test_none_minutes_not_shown():
    result = format_streak(5, streak_minutes=None)
    assert "min this streak" not in result


def test_with_freeze_until():
    until = date(2026, 5, 10)
    result = format_streak(7, freeze_until=until)
    assert "freeze until" in result
    assert "2026-05-10" in result


def test_freeze_badge_not_shown_when_none():
    result = format_streak(7, freeze_until=None)
    assert "freeze" not in result


def test_minutes_and_freeze_together():
    until = date(2026, 5, 10)
    result = format_streak(14, streak_minutes=300, freeze_until=until)
    assert "300 min this streak" in result
    assert "freeze until" in result


def test_day_one_no_extras():
    result = format_streak(1)
    assert "min this streak" not in result
    assert "freeze" not in result
