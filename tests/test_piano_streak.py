"""Tests for the piano streak calculation logic."""

from datetime import date, timedelta

from bot.modules.piano.services.streaks import calculate_streak

TODAY = date(2026, 4, 30)


def _calc(current, last_offset, credits=0, freeze_until=None, practiced_offset=0):
    """Helper: last = TODAY - last_offset days, practiced = TODAY - practiced_offset days."""
    last = TODAY - timedelta(days=last_offset) if last_offset is not None else None
    practiced = TODAY - timedelta(days=practiced_offset)
    return calculate_streak(current, current, credits, freeze_until, last, practiced)


# ---------------------------------------------------------------------------
# Basic streak progression
# ---------------------------------------------------------------------------


def test_first_ever_practice():
    r = _calc(0, last_offset=None)
    assert r["new_current"] == 1


def test_consecutive_day_increments():
    r = _calc(5, last_offset=1)
    assert r["new_current"] == 6


def test_backdated_log_keeps_current():
    r = _calc(10, last_offset=0, practiced_offset=1)  # practiced yesterday, last was today
    assert r["new_current"] == 10


# ---------------------------------------------------------------------------
# Free day (1 missed day always forgiven)
# ---------------------------------------------------------------------------


def test_one_missed_day_is_free():
    r = _calc(14, last_offset=2, credits=0)
    assert r["new_current"] == 15


def test_one_missed_day_free_does_not_consume_credit():
    r = _calc(14, last_offset=2, credits=1)
    assert r["new_current"] == 15
    assert r["freeze_credits"] == 1  # credit untouched


# ---------------------------------------------------------------------------
# Credit consumption
# ---------------------------------------------------------------------------


def test_two_missed_days_costs_one_credit():
    r = _calc(14, last_offset=3, credits=1)
    assert r["new_current"] == 15
    assert r["freeze_credits"] == 0


def test_three_missed_days_costs_two_credits():
    r = _calc(14, last_offset=4, credits=2)
    assert r["new_current"] == 15
    assert r["freeze_credits"] == 0


def test_two_missed_days_no_credits_resets():
    r = _calc(14, last_offset=3, credits=0)
    assert r["new_current"] == 1


def test_three_missed_days_one_credit_not_enough_resets():
    r = _calc(14, last_offset=4, credits=1)
    assert r["new_current"] == 1


def test_credits_carry_over_on_reset():
    r = _calc(21, last_offset=5, credits=2)  # 4 missed days, needs 3 credits, only 2 → reset
    assert r["new_current"] == 1
    assert r["freeze_credits"] == 2  # credits NOT wiped


# ---------------------------------------------------------------------------
# Milestone credit earning
# ---------------------------------------------------------------------------


def test_day_7_milestone_awards_credit():
    r = _calc(6, last_offset=1, credits=0)
    assert r["new_current"] == 7
    assert r["freeze_credits"] == 1


def test_day_14_milestone_awards_credit():
    r = _calc(13, last_offset=1, credits=0)
    assert r["new_current"] == 14
    assert r["freeze_credits"] == 1


def test_milestone_at_cap_skipped_silently():
    r = _calc(6, last_offset=1, credits=2)
    assert r["new_current"] == 7
    assert r["freeze_credits"] == 2  # already at cap, no change


def test_milestone_increments_to_cap():
    r = _calc(6, last_offset=1, credits=1)
    assert r["new_current"] == 7
    assert r["freeze_credits"] == 2


# ---------------------------------------------------------------------------
# Travel freeze
# ---------------------------------------------------------------------------


def test_freeze_covers_large_gap():
    freeze_until = TODAY + timedelta(days=2)
    r = _calc(14, last_offset=5, credits=0, freeze_until=freeze_until)
    assert r["new_current"] == 15


def test_freeze_clears_when_practiced_past_freeze_until():
    freeze_until = TODAY - timedelta(days=1)  # freeze expired yesterday
    r = _calc(14, last_offset=3, credits=0, freeze_until=freeze_until)
    # freeze_until is in the past relative to practiced_at (today), so it clears
    assert r["freeze_until"] is None


def test_freeze_preserved_while_still_active():
    freeze_until = TODAY + timedelta(days=5)
    r = _calc(14, last_offset=1, credits=0, freeze_until=freeze_until)
    assert r["freeze_until"] == freeze_until


def test_freeze_takes_priority_over_credits():
    freeze_until = TODAY + timedelta(days=2)
    r = _calc(14, last_offset=5, credits=2, freeze_until=freeze_until)
    # freeze covers it — credits should NOT be consumed
    assert r["new_current"] == 15
    assert r["freeze_credits"] == 2
