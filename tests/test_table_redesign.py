"""Tests for table redesign features."""

from unittest.mock import MagicMock

from talos.game_manager import extract_leg_labels
from talos.ui.widgets import OpportunitiesTable, _fmt_freshness, _fmt_pnl_with_roi


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


def test_pnl_with_roi_positive():
    result = _fmt_pnl_with_roi(640, 15600)  # $6.40 P&L on $156 invested
    assert "$6.40" in result
    assert "4.1%" in result


def test_pnl_with_roi_zero_invested():
    """Zero invested → no ROI shown."""
    result = _fmt_pnl_with_roi(0, 0)
    assert "%" not in result


def test_build_two_rows_returns_pair():
    """_build_row_pair returns two tuples (row1, row2)."""
    table = OpportunitiesTable()
    table._leg_labels = {"EVT-TEST": ("Boston Bruins", "Washington Capitals")}
    table._freshness = {"MKT-A": 1.0, "MKT-B": 2.0}

    opp = MagicMock()
    opp.event_ticker = "EVT-TEST"
    opp.ticker_a = "MKT-A"
    opp.ticker_b = "MKT-B"
    opp.no_a = 42
    opp.no_b = 44
    opp.fee_edge = 3.2

    row1, row2 = table._build_row_pair(opp, tracker=None)
    # Row 1 should have team name "Boston Bruins"
    assert "Boston Bruins" in str(row1[1])
    # Row 2 should have team name "Washington Capitals"
    assert "Washington Capitals" in str(row2[1])
    # Both rows have 14 cells
    assert len(row1) == 14
    assert len(row2) == 14
