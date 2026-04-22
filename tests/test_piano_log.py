"""Tests for the piano log body parser."""
import pytest

from bot.modules.piano.handlers.piano import _parse_log_body


def test_explicit_duration_and_pieces():
    duration, pieces, notes = _parse_log_body("30 min Chopin, scales")
    assert duration == 30
    assert pieces == ["Chopin", "scales"]
    assert notes is None


def test_minutes_spelled_out():
    duration, pieces, notes = _parse_log_body("45 minutes Bach")
    assert duration == 45
    assert pieces == ["Bach"]


def test_bare_leading_number():
    duration, pieces, notes = _parse_log_body("30 Beethoven Sonata")
    assert duration == 30
    assert pieces == ["Beethoven Sonata"]


def test_duration_only():
    duration, pieces, notes = _parse_log_body("30 min")
    assert duration == 30
    assert pieces == []
    assert notes is None


def test_pieces_only_no_duration():
    duration, pieces, notes = _parse_log_body("Chopin Nocturne")
    assert duration is None
    assert pieces == ["Chopin Nocturne"]


def test_notes_via_separator():
    duration, pieces, notes = _parse_log_body("30 min Chopin -- felt great today")
    assert duration == 30
    assert pieces == ["Chopin"]
    assert notes == "felt great today"


def test_notes_separator_no_pieces():
    duration, pieces, notes = _parse_log_body("45 min -- warm-up only")
    assert duration == 45
    assert pieces == []
    assert notes == "warm-up only"


def test_multiple_pieces_comma_separated():
    duration, pieces, notes = _parse_log_body("60 min Bach, Chopin, scales, arpeggios")
    assert duration == 60
    assert pieces == ["Bach", "Chopin", "scales", "arpeggios"]


def test_empty_body():
    duration, pieces, notes = _parse_log_body("")
    assert duration is None
    assert pieces == []
    assert notes is None


def test_whitespace_only():
    duration, pieces, notes = _parse_log_body("   ")
    assert duration is None
    assert pieces == []
    assert notes is None


def test_m_abbreviation():
    duration, pieces, notes = _parse_log_body("20m Czerny")
    assert duration == 20
    assert pieces == ["Czerny"]
