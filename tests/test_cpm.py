"""Tests for CPM tracker and formatting."""

from __future__ import annotations

import time

from talos.cpm import CPMTracker, FlowKey, format_cpm, format_eta
from talos.models.market import Trade


def _trade(trade_id: str, count: int, ts: str = "2026-03-06T12:00:00Z") -> Trade:
    return Trade(
        ticker="MKT-A",
        trade_id=trade_id,
        price_bps=5000,
        yes_price_bps=5000,
        no_price_bps=5000,
        count_fp100=count * 100,
        side="no",
        created_time=ts,
    )


def _make_trade(
    *,
    ticker: str = "KX-TEST",
    trade_id: str = "t1",
    taker_side: str,
    yes_price_dollars: str,
    count_fp: str = "1",
    created_time: str = "2026-04-26T00:00:00Z",
) -> Trade:
    """Build a Trade pydantic model from wire-shape kwargs."""
    no_price = f"{1 - float(yes_price_dollars):.4f}"
    return Trade.model_validate(
        {
            "ticker": ticker,
            "trade_id": trade_id,
            "taker_side": taker_side,
            "yes_price_dollars": yes_price_dollars,
            "no_price_dollars": no_price,
            "count_fp": count_fp,
            "created_time": created_time,
        }
    )


def _flow_total_events(tracker: CPMTracker, ticker: str) -> int:
    """Count total events across every FlowKey for a ticker."""
    return sum(len(evs) for k, evs in tracker._events.items() if k.ticker == ticker)


class TestIngest:
    def test_ingests_trades(self) -> None:
        tracker = CPMTracker()
        tracker.ingest("MKT-A", [_trade("t1", 10), _trade("t2", 20)])
        # Each trade decomposes into 2 flow events (yes + no buckets).
        assert _flow_total_events(tracker, "MKT-A") == 4

    def test_deduplicates_by_trade_id(self) -> None:
        tracker = CPMTracker()
        tracker.ingest("MKT-A", [_trade("t1", 10)])
        tracker.ingest("MKT-A", [_trade("t1", 10), _trade("t2", 5)])
        # 2 unique trade_ids → 2 trades × 2 buckets each = 4 events.
        assert _flow_total_events(tracker, "MKT-A") == 4

    def test_caps_events_per_key(self) -> None:
        tracker = CPMTracker()
        # All trades at the same price + same side → land in the same two
        # FlowKeys. Each FlowKey is independently capped at _MAX_EVENTS_PER_KEY.
        trades = [_trade(f"t{i}", 1) for i in range(400)]
        tracker.ingest("MKT-A", trades)
        for key, events in tracker._events.items():
            if key.ticker == "MKT-A":
                assert len(events) == CPMTracker._MAX_EVENTS_PER_KEY


class TestCPM:
    def test_cpm_with_recent_trades(self) -> None:
        tracker = CPMTracker()
        now = time.time()
        # Seed an arbitrary FlowKey directly in the new shape (int count_fp100).
        # 30 contracts (3000 fp100) over the 5-minute window → CPM > 0.
        key = FlowKey(ticker="MKT-A", outcome="no", book_side="ASK", price_bps=5000)
        tracker._events[key] = [(now - 60 * i, 1000) for i in range(3)]
        result = tracker.cpm("MKT-A", window_sec=300.0)
        assert result is not None
        assert result > 0

    def test_cpm_none_for_unknown_ticker(self) -> None:
        tracker = CPMTracker()
        assert tracker.cpm("UNKNOWN") is None

    def test_cpm_zero_when_no_trades_in_window(self) -> None:
        tracker = CPMTracker()
        # All trades are old (1 hour ago). Use new shape (int count_fp100).
        old = time.time() - 3600
        key = FlowKey(ticker="MKT-A", outcome="no", book_side="ASK", price_bps=5000)
        tracker._events[key] = [(old, 1000)]
        result = tracker.cpm("MKT-A", window_sec=300.0)
        assert result == 0.0

    def test_cpm_uses_actual_time_for_short_observation(self) -> None:
        tracker = CPMTracker()
        now = time.time()
        # 10 contracts (1000 fp100) just 30 seconds ago.
        key = FlowKey(ticker="MKT-A", outcome="no", book_side="ASK", price_bps=5000)
        tracker._events[key] = [(now - 30, 1000)]
        result = tracker.cpm("MKT-A", window_sec=300.0)
        assert result is not None
        # Should be ~20 CPM (10 contracts / 0.5 min), not 2.0 (10/5).
        assert result > 10.0


class TestIsPartial:
    def test_partial_when_no_data(self) -> None:
        tracker = CPMTracker()
        assert tracker.is_partial("UNKNOWN") is True

    def test_partial_when_short_observation(self) -> None:
        tracker = CPMTracker()
        key = FlowKey(ticker="MKT-A", outcome="no", book_side="ASK", price_bps=5000)
        tracker._events[key] = [(time.time() - 60, 1000)]
        assert tracker.is_partial("MKT-A", window_sec=300.0) is True

    def test_not_partial_with_long_observation(self) -> None:
        tracker = CPMTracker()
        key = FlowKey(ticker="MKT-A", outcome="no", book_side="ASK", price_bps=5000)
        tracker._events[key] = [(time.time() - 400, 1000)]
        assert tracker.is_partial("MKT-A", window_sec=300.0) is False


class TestETA:
    def test_eta_with_cpm(self) -> None:
        tracker = CPMTracker()
        now = time.time()
        # 60 contracts (6000 fp100) in ~5 min → ~12 CPM.
        key = FlowKey(ticker="MKT-A", outcome="no", book_side="ASK", price_bps=5000)
        tracker._events[key] = [(now - 250, 6000)]
        eta = tracker.eta_minutes("MKT-A", queue_position=24)
        assert eta is not None
        assert eta > 0

    def test_eta_none_when_no_cpm(self) -> None:
        tracker = CPMTracker()
        assert tracker.eta_minutes("UNKNOWN", 100) is None

    def test_eta_none_when_cpm_zero(self) -> None:
        tracker = CPMTracker()
        old = time.time() - 3600
        key = FlowKey(ticker="MKT-A", outcome="no", book_side="ASK", price_bps=5000)
        tracker._events[key] = [(old, 1000)]
        assert tracker.eta_minutes("MKT-A", 100) is None


class TestPrune:
    def test_removes_old_events(self) -> None:
        tracker = CPMTracker()
        old = time.time() - 5000
        key = FlowKey(ticker="MKT-A", outcome="no", book_side="ASK", price_bps=5000)
        tracker._events[key] = [(old, 1000), (time.time(), 500)]
        tracker.prune(max_age=3700.0)
        assert len(tracker._events[key]) == 1

    def test_removes_empty_keys(self) -> None:
        tracker = CPMTracker()
        old = time.time() - 5000
        key = FlowKey(ticker="MKT-A", outcome="no", book_side="ASK", price_bps=5000)
        tracker._events[key] = [(old, 1000)]
        tracker.prune(max_age=3700.0)
        assert key not in tracker._events

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
        assert format_eta(480.0) == "8.0h"

    def test_infinity(self) -> None:
        assert format_eta(float("inf")) == "∞"

    def test_partial_flag(self) -> None:
        assert format_eta(5.0, partial=True) == "5m*"


class TestDecomposition:
    def test_trade_decomposes_into_two_flow_events(self) -> None:
        """A YES-taker trade at YES=0.55 produces:
        - YES outcome ASK at 5500 bps
        - NO outcome BID at 4500 bps
        Each gets count_fp100 added to its flow.
        """
        tracker = CPMTracker()
        trade = _make_trade(taker_side="yes", yes_price_dollars="0.55", count_fp="3")
        tracker.ingest("KX-TEST", [trade])

        yes_ask_5500 = FlowKey(ticker="KX-TEST", outcome="yes", book_side="ASK", price_bps=5500)
        no_bid_4500 = FlowKey(ticker="KX-TEST", outcome="no", book_side="BID", price_bps=4500)

        assert tracker.flow_count(yes_ask_5500) == 300  # 3 contracts = 300 fp100
        assert tracker.flow_count(no_bid_4500) == 300

        yes_bid_5500 = FlowKey(ticker="KX-TEST", outcome="yes", book_side="BID", price_bps=5500)
        assert tracker.flow_count(yes_bid_5500) == 0

    def test_no_taker_decomposes_inversely(self) -> None:
        """A NO-taker trade at YES=0.55 produces:
        - NO outcome ASK at 4500 bps
        - YES outcome BID at 5500 bps
        """
        tracker = CPMTracker()
        trade = _make_trade(taker_side="no", yes_price_dollars="0.55", count_fp="2")
        tracker.ingest("KX-TEST", [trade])

        no_ask_4500 = FlowKey(ticker="KX-TEST", outcome="no", book_side="ASK", price_bps=4500)
        yes_bid_5500 = FlowKey(ticker="KX-TEST", outcome="yes", book_side="BID", price_bps=5500)
        assert tracker.flow_count(no_ask_4500) == 200
        assert tracker.flow_count(yes_bid_5500) == 200
