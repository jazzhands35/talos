"""Tests for settlement P&L aggregation."""

from datetime import datetime
from zoneinfo import ZoneInfo

from talos.settlement_tracker import aggregate_settlements, reconcile_event

PT = ZoneInfo("America/Los_Angeles")


def test_aggregate_today():
    """Settlements from today (PT) sum correctly."""
    now_pt = datetime.now(PT)
    today_str = now_pt.strftime("%Y-%m-%dT%H:%M:%SZ")

    settlements = [
        {"revenue": 640, "no_total_cost": 400, "settled_time": today_str, "event_ticker": "E1"},
        {"revenue": 320, "no_total_cost": 300, "settled_time": today_str, "event_ticker": "E2"},
    ]

    result = aggregate_settlements(settlements, now_pt)
    assert result["today_pnl"] == 260  # (640-400) + (320-300) = 240+20 profit
    assert result["today_invested"] == 700  # 400 + 300 cost


def test_aggregate_empty():
    now_pt = datetime.now(PT)
    result = aggregate_settlements([], now_pt)
    assert result["today_pnl"] == 0
    assert result["yesterday_pnl"] == 0
    assert result["week_pnl"] == 0


def test_reconcile_matching():
    """Our P&L matches Kalshi's — no discrepancy."""
    result = reconcile_event(
        our_revenue=640,
        kalshi_revenue=640,
        event_ticker="EVT-TEST",
    )
    assert result is None


def test_reconcile_mismatch():
    """Our P&L differs from Kalshi's — returns discrepancy."""
    result = reconcile_event(
        our_revenue=640,
        kalshi_revenue=600,
        event_ticker="EVT-TEST",
    )
    assert result is not None
    assert result["difference"] == 40
    assert result["event_ticker"] == "EVT-TEST"
