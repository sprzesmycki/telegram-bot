"""Tests for piano repertoire pure helpers."""

from bot.modules.piano.services.repertoire import (
    format_pieces_list,
    parse_piece_title,
    status_emoji,
    summarize_in_progress,
)


# ---------------------------------------------------------------------------
# status_emoji
# ---------------------------------------------------------------------------


def test_status_emoji_known():
    assert status_emoji("learning") == "\U0001f4d6"
    assert status_emoji("polishing") == "\U0001f527"
    assert status_emoji("mastered") == "✅"
    assert status_emoji("needs_review") == "\U0001f504"


def test_status_emoji_unknown_fallback():
    assert status_emoji("unknown_status") == "\U0001f3b5"


# ---------------------------------------------------------------------------
# parse_piece_title
# ---------------------------------------------------------------------------


def test_parse_piece_title_with_composer():
    title, composer = parse_piece_title("Nocturne by Chopin")
    assert title == "Nocturne"
    assert composer == "Chopin"


def test_parse_piece_title_no_composer():
    title, composer = parse_piece_title("Scales and arpeggios")
    assert title == "Scales and arpeggios"
    assert composer is None


def test_parse_piece_title_by_case_insensitive():
    title, composer = parse_piece_title("Invention 1 BY Bach")
    assert title == "Invention 1"
    assert composer == "Bach"


def test_parse_piece_title_empty():
    title, composer = parse_piece_title("")
    assert title == ""
    assert composer is None


def test_parse_piece_title_strips_whitespace():
    title, composer = parse_piece_title("  Moonlight Sonata by Beethoven  ")
    assert title == "Moonlight Sonata"
    assert composer == "Beethoven"


def test_parse_piece_title_composer_only_word():
    title, composer = parse_piece_title("Waltz by Chopin")
    assert title == "Waltz"
    assert composer == "Chopin"


# ---------------------------------------------------------------------------
# format_pieces_list
# ---------------------------------------------------------------------------


def test_format_pieces_list_empty():
    result = format_pieces_list([])
    assert "No pieces" in result
    assert "/piano piece add" in result


def test_format_pieces_list_single_piece():
    pieces = [{"title": "Nocturne", "composer": "Chopin", "status": "learning"}]
    result = format_pieces_list(pieces)
    assert "Nocturne" in result
    assert "Chopin" in result


def test_format_pieces_list_no_composer():
    pieces = [{"title": "Scales", "composer": None, "status": "polishing"}]
    result = format_pieces_list(pieces)
    assert "Scales" in result
    assert " — " not in result


def test_format_pieces_list_grouped_by_status():
    pieces = [
        {"title": "Sonata", "composer": None, "status": "mastered"},
        {"title": "Nocturne", "composer": "Chopin", "status": "learning"},
    ]
    result = format_pieces_list(pieces)
    assert "Nocturne" in result
    assert "Sonata" in result
    # learning comes before mastered in the order
    assert result.index("Nocturne") < result.index("Sonata")


# ---------------------------------------------------------------------------
# summarize_in_progress
# ---------------------------------------------------------------------------


def test_summarize_in_progress_none():
    pieces = [{"title": "Sonata", "status": "mastered"}]
    assert summarize_in_progress(pieces) == "no pieces in progress"


def test_summarize_in_progress_empty():
    assert summarize_in_progress([]) == "no pieces in progress"


def test_summarize_in_progress_single():
    pieces = [{"title": "Nocturne", "status": "learning"}]
    result = summarize_in_progress(pieces)
    assert result == "Nocturne"


def test_summarize_in_progress_multiple():
    pieces = [
        {"title": "Nocturne", "status": "learning"},
        {"title": "Waltz", "status": "polishing"},
        {"title": "Etude", "status": "needs_review"},
    ]
    result = summarize_in_progress(pieces)
    assert "Nocturne" in result
    assert "Waltz" in result
    assert "Etude" in result


def test_summarize_in_progress_truncates_at_five():
    pieces = [{"title": f"Piece {i}", "status": "learning"} for i in range(7)]
    result = summarize_in_progress(pieces)
    assert "+2 more" in result
