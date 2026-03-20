"""Tests for settlement P&L aggregation and cache."""

import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from talos.models.portfolio import Settlement
from talos.settlement_tracker import SettlementCache, aggregate_settlements, reconcile_event

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


class TestSettlementCache:
    def test_upsert_and_read(self, tmp_path: Path) -> None:
        """Upsert a settlement and read it back."""
        cache = SettlementCache(tmp_path / "test.db")
        s = Settlement(
            ticker="MKT-A",
            event_ticker="EVT-1",
            revenue=1000,
            no_total_cost=380,
            market_result="no",
            no_count=10,
            settled_time="2026-03-20T12:00:00Z",
        )
        cache.upsert(s, est_pnl_cents=70, sub_title="Team A vs Team B")
        rows = cache.all_settlements()
        assert len(rows) == 1
        assert rows[0]["ticker"] == "MKT-A"
        assert rows[0]["est_pnl_cents"] == 70
        assert rows[0]["sub_title"] == "Team A vs Team B"
        cache.close()

    def test_upsert_preserves_est_pnl(self, tmp_path: Path) -> None:
        """Re-upserting without est_pnl should not overwrite existing value."""
        cache = SettlementCache(tmp_path / "test.db")
        s = Settlement(
            ticker="MKT-A",
            event_ticker="EVT-1",
            revenue=1000,
            no_total_cost=380,
            settled_time="2026-03-20T12:00:00Z",
        )
        cache.upsert(s, est_pnl_cents=70)
        # Re-upsert without est_pnl
        cache.upsert(s, est_pnl_cents=None)
        rows = cache.all_settlements()
        assert rows[0]["est_pnl_cents"] == 70  # preserved!
        cache.close()

    def test_latest_settled_time(self, tmp_path: Path) -> None:
        """latest_settled_time returns the most recent time."""
        cache = SettlementCache(tmp_path / "test.db")
        for i, ts in enumerate(["2026-03-18T12:00:00Z", "2026-03-20T12:00:00Z", "2026-03-19T12:00:00Z"]):
            s = Settlement(ticker=f"MKT-{i}", event_ticker=f"EVT-{i}", settled_time=ts)
            cache.upsert(s)
        assert cache.latest_settled_time() == "2026-03-20T12:00:00Z"
        cache.close()

    def test_settlements_as_models(self, tmp_path: Path) -> None:
        """settlements_as_models returns (Settlement, est_pnl, sub_title) tuples."""
        cache = SettlementCache(tmp_path / "test.db")
        s = Settlement(
            ticker="MKT-A",
            event_ticker="EVT-1",
            revenue=1000,
            no_total_cost=380,
            settled_time="2026-03-20T12:00:00Z",
        )
        cache.upsert(s, est_pnl_cents=70, sub_title="A vs B")
        result = cache.settlements_as_models()
        assert len(result) == 1
        settlement, est, sub = result[0]
        assert settlement.ticker == "MKT-A"
        assert settlement.revenue == 1000
        assert est == 70
        assert sub == "A vs B"
        cache.close()
