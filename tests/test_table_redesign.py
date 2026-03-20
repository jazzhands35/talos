"""Tests for table redesign features."""

from talos.game_manager import extract_leg_labels
from talos.ui.widgets import _fmt_freshness


def test_extract_leg_labels_from_subtitle():
    """sub_title like 'Boston Bruins vs Washington Capitals (Mar 19)' → tuple."""
    result = extract_leg_labels("Boston Bruins vs Washington Capitals (Mar 19)")
    assert result == ("Boston Bruins", "Washington Capitals")


def test_extract_leg_labels_no_date_suffix():
    result = extract_leg_labels("LA Lakers vs NY Knicks")
    assert result == ("LA Lakers", "NY Knicks")


def test_extract_leg_labels_at_separator():
    """'Wake Forest at Virginia Tech (Mar 10)' → tuple."""
    result = extract_leg_labels("Wake Forest at Virginia Tech (Mar 10)")
    assert result == ("Wake Forest", "Virginia Tech")


def test_extract_leg_labels_unparseable():
    """Fallback to full string for both if no separator found."""
    result = extract_leg_labels("Some Weird Title")
    assert result == ("Some Weird Title", "Some Weird Title")


def test_extract_leg_labels_empty():
    result = extract_leg_labels("")
    assert result == ("", "")


def test_freshness_dot_fresh():
    """< 5 seconds → green dot."""
    result = _fmt_freshness(2.0)
    assert "●" in str(result)


def test_freshness_dot_warming():
    """5-30 seconds → yellow dot."""
    result = _fmt_freshness(15.0)
    assert "●" in str(result)


def test_freshness_dot_stale():
    """30+ seconds → red dot."""
    result = _fmt_freshness(45.0)
    assert "●" in str(result)


def test_freshness_dot_never_connected():
    """No data yet (age=None) → dim dot."""
    result = _fmt_freshness(None)
    assert "○" in str(result)
