"""Tests for CPM tracker and formatting."""

from __future__ import annotations

import time

from talos.cpm import CPMTracker, format_cpm, format_eta
from talos.models.market import Trade


def _trade(trade_id: str, count: int, ts: str = "2026-03-06T12:00:00Z") -> Trade:
    return Trade(
        ticker="MKT-A",
        trade_id=trade_id,
        price=50,
        count=count,
        side="no",
        created_time=ts,
    )


class TestIngest:
    def test_ingests_trades(self) -> None:
        tracker = CPMTracker()
        tracker.ingest("MKT-A", [_trade("t1", 10), _trade("t2", 20)])
        assert len(tracker._events["MKT-A"]) == 2

    def test_deduplicates_by_trade_id(self) -> None:
        tracker = CPMTracker()
        tracker.ingest("MKT-A", [_trade("t1", 10)])
        tracker.ingest("MKT-A", [_trade("t1", 10), _trade("t2", 5)])
        assert len(tracker._events["MKT-A"]) == 2

    def test_caps_events_per_key(self) -> None:
        tracker = CPMTracker()
        trades = [_trade(f"t{i}", 1) for i in range(400)]
        tracker.ingest("MKT-A", trades)
        assert len(tracker._events["MKT-A"]) == CPMTracker._MAX_EVENTS_PER_KEY


class TestCPM:
    def test_cpm_with_recent_trades(self) -> None:
        tracker = CPMTracker()
        now = time.time()
        # 30 contracts in a 5-minute window → 6.0 CPM
        tracker._events["MKT-A"] = [(now - 60 * i, 10.0) for i in range(3)]
        result = tracker.cpm("MKT-A", window_sec=300.0)
        assert result is not None
        assert result > 0

    def test_cpm_none_for_unknown_ticker(self) -> None:
        tracker = CPMTracker()
        assert tracker.cpm("UNKNOWN") is None

    def test_cpm_zero_when_no_trades_in_window(self) -> None:
        tracker = CPMTracker()
        # All trades are old (1 hour ago)
        old = time.time() - 3600
        tracker._events["MKT-A"] = [(old, 10.0)]
        result = tracker.cpm("MKT-A", window_sec=300.0)
        assert result == 0.0

    def test_cpm_uses_actual_time_for_short_observation(self) -> None:
        tracker = CPMTracker()
        now = time.time()
        # 10 contracts just 30 seconds ago
        tracker._events["MKT-A"] = [(now - 30, 10.0)]
        result = tracker.cpm("MKT-A", window_sec=300.0)
        assert result is not None
        # Should be ~20 CPM (10 contracts / 0.5 min), not 2.0 (10/5)
        assert result > 10.0


class TestIsPartial:
    def test_partial_when_no_data(self) -> None:
        tracker = CPMTracker()
        assert tracker.is_partial("UNKNOWN") is True

    def test_partial_when_short_observation(self) -> None:
        tracker = CPMTracker()
        tracker._events["MKT-A"] = [(time.time() - 60, 10.0)]
        assert tracker.is_partial("MKT-A", window_sec=300.0) is True

    def test_not_partial_with_long_observation(self) -> None:
        tracker = CPMTracker()
        tracker._events["MKT-A"] = [(time.time() - 400, 10.0)]
        assert tracker.is_partial("MKT-A", window_sec=300.0) is False


class TestETA:
    def test_eta_with_cpm(self) -> None:
        tracker = CPMTracker()
        now = time.time()
        # 60 contracts in ~5 min → ~12 CPM
        tracker._events["MKT-A"] = [(now - 250, 60.0)]
        eta = tracker.eta_minutes("MKT-A", queue_position=24)
        assert eta is not None
        assert eta > 0

    def test_eta_none_when_no_cpm(self) -> None:
        tracker = CPMTracker()
        assert tracker.eta_minutes("UNKNOWN", 100) is None

    def test_eta_none_when_cpm_zero(self) -> None:
        tracker = CPMTracker()
        old = time.time() - 3600
        tracker._events["MKT-A"] = [(old, 10.0)]
        assert tracker.eta_minutes("MKT-A", 100) is None


class TestPrune:
    def test_removes_old_events(self) -> None:
        tracker = CPMTracker()
        old = time.time() - 5000
        tracker._events["MKT-A"] = [(old, 10.0), (time.time(), 5.0)]
        tracker.prune(max_age=3700.0)
        assert len(tracker._events["MKT-A"]) == 1

    def test_removes_empty_keys(self) -> None:
        tracker = CPMTracker()
        old = time.time() - 5000
        tracker._events["MKT-A"] = [(old, 10.0)]
        tracker.prune(max_age=3700.0)
        assert "MKT-A" not in tracker._events

    def test_clears_seen_when_over_limit(self) -> None:
        tracker = CPMTracker()
        tracker._seen = {str(i) for i in range(25_000)}
        tracker.prune()
        assert len(tracker._seen) == 0


class TestFormatCPM:
    def test_none(self) -> None:
        assert format_cpm(None) == "—"

    def test_small_value(self) -> None:
        assert format_cpm(5.12) == "5.12"

    def test_medium_value(self) -> None:
        assert format_cpm(15.7) == "15.7"

    def test_large_value(self) -> None:
        assert format_cpm(150.0) == "150"

    def test_very_large(self) -> None:
        assert format_cpm(1500.0) == "1,500"

    def test_partial_flag(self) -> None:
        assert format_cpm(5.12, partial=True) == "5.12*"


class TestFormatETA:
    def test_none(self) -> None:
        assert format_eta(None) == "—"

    def test_minutes(self) -> None:
        assert format_eta(5.0) == "5m"

    def test_minimum_one_minute(self) -> None:
        assert format_eta(0.3) == "1m"

    def test_hours(self) -> None:
        assert format_eta(150.0) == "2.5h"

    def test_large_hours_rounded(self) -> None:
        assert format_eta(480.0) == "8h"

    def test_infinity(self) -> None:
        assert format_eta(float("inf")) == "∞"

    def test_partial_flag(self) -> None:
        assert format_eta(5.0, partial=True) == "5m*"
