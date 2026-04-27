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
        # Use outcome="yes" so bare-ticker aggregate (which iterates yes-only
        # to avoid double-counting after the granularity refactor) sees it.
        key = FlowKey(ticker="MKT-A", outcome="yes", book_side="ASK", price_bps=5000)
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
        key = FlowKey(ticker="MKT-A", outcome="yes", book_side="ASK", price_bps=5000)
        tracker._events[key] = [(old, 1000)]
        result = tracker.cpm("MKT-A", window_sec=300.0)
        assert result == 0.0

    def test_cpm_uses_actual_time_for_short_observation(self) -> None:
        tracker = CPMTracker()
        now = time.time()
        # 10 contracts (1000 fp100) just 30 seconds ago.
        key = FlowKey(ticker="MKT-A", outcome="yes", book_side="ASK", price_bps=5000)
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
        # Bare-ticker aggregate iterates yes-only, so seed yes-side.
        key = FlowKey(ticker="MKT-A", outcome="yes", book_side="ASK", price_bps=5000)
        tracker._events[key] = [(time.time() - 400, 1000)]
        assert tracker.is_partial("MKT-A", window_sec=300.0) is False


class TestETA:
    def test_eta_with_cpm(self) -> None:
        tracker = CPMTracker()
        now = time.time()
        # 60 contracts (6000 fp100) in ~5 min → ~12 CPM.
        # Bare-ticker eta_minutes uses bare-ticker cpm (yes-only iteration).
        key = FlowKey(ticker="MKT-A", outcome="yes", book_side="ASK", price_bps=5000)
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
        assert format_eta(8 * 60.0, round_hours_after=5.0) == "8h"

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


def test_ticker_aggregate_counts_each_trade_once_via_ingest():
    """Bare-ticker aggregate must NOT double-count after the granularity refactor.

    Ingesting one trade with count_fp=3 should yield aggregate flow of 3 contracts,
    not 6 (which would be the bug if we summed across both yes and no buckets).
    """
    tracker = CPMTracker()
    trade = _make_trade(taker_side="yes", yes_price_dollars="0.55", count_fp="3")
    tracker.ingest("KX-TEST", [trade])
    # cpm with all None bucket params = bare-ticker aggregate.
    rate = tracker.cpm("KX-TEST", window_sec=300.0)
    assert rate is not None
    # Each trade decomposes into 2 buckets at 300 fp100 each. The bare-ticker
    # aggregate restricts to one outcome side → 300 fp100 = 3 contracts (not 6).
    # Verify by also asserting the per-outcome aggregate equals the bare aggregate:
    rate_yes = tracker.cpm("KX-TEST", outcome="yes", window_sec=300.0)
    assert rate_yes is not None
    assert abs(rate - rate_yes) < 1e-9, (
        f"bare aggregate {rate} should equal yes-only {rate_yes}"
    )


def test_ticker_aggregate_with_n_trades_scales_linearly():
    """N trades on the same ticker with count_fp=1 each → aggregate ~ N, not 2N."""
    tracker = CPMTracker()
    trades = [
        _make_trade(
            trade_id=f"t{i}",
            taker_side="yes",
            yes_price_dollars="0.50",
            count_fp="1",
        )
        for i in range(10)
    ]
    tracker.ingest("KX-TEST", trades)
    # Sum of all flow_counts across yes-bucket only = 10 trades * 100 fp100 = 1000.
    yes_ask_5000 = FlowKey(ticker="KX-TEST", outcome="yes", book_side="ASK", price_bps=5000)
    no_bid_5000 = FlowKey(ticker="KX-TEST", outcome="no", book_side="BID", price_bps=5000)
    assert tracker.flow_count(yes_ask_5000) == 1000
    assert tracker.flow_count(no_bid_5000) == 1000  # decomposition produces both
    # But bare-ticker aggregate counts only ONE side → 10 contracts, not 20.
    rate = tracker.cpm("KX-TEST", window_sec=3600.0)
    assert rate is not None
    rate_yes = tracker.cpm("KX-TEST", outcome="yes", window_sec=3600.0)
    assert rate_yes is not None
    # Bare aggregate must equal the yes-only aggregate (one-side iteration).
    assert abs(rate - rate_yes) < 1e-9, (
        f"bare aggregate {rate} should equal yes-only {rate_yes}"
    )


def test_per_bucket_isolation_vs_bare_aggregate():
    """Two trades on opposite outcomes: per-bucket CPM isolates one trade's
    contribution; bare-ticker aggregate (yes-only after C1 fix) counts each
    trade exactly once.
    """
    import time as _time
    from unittest.mock import patch

    tracker = CPMTracker()
    now = _time.time()
    with patch("talos.cpm.time.time", return_value=now):
        with patch("talos.cpm._parse_iso", return_value=now - 60):
            tracker.ingest(
                "KX-TEST",
                [
                    _make_trade(
                        trade_id="t1",
                        taker_side="yes",
                        yes_price_dollars="0.55",
                        count_fp="6",
                    ),
                    _make_trade(
                        trade_id="t2",
                        taker_side="no",
                        yes_price_dollars="0.45",
                        count_fp="6",
                    ),
                ],
            )

        # Per-bucket: yes-ASK-5500 sees t1's contribution only.
        # 6 contracts over observed=60s → use_sec=60s → CPM = 6 / 1 min = 6.0.
        per_bucket = tracker.cpm(
            "KX-TEST", "yes", "ASK", 5500, window_sec=300.0
        )
        assert per_bucket is not None
        assert abs(per_bucket - 6.0) < 0.01

        # Bare-ticker aggregate (yes-only after C1 fix): yes-ASK-5500 (t1, 6)
        # + yes-BID-5500 (t2, 6) = 12 contracts / 1 min = 12.0.
        aggregate = tracker.cpm("KX-TEST", window_sec=300.0)
        assert aggregate is not None
        assert abs(aggregate - 12.0) < 0.01


def test_eta_minutes_fallback_chain():
    """eta_minutes broadens its CPM source when narrower buckets have no data.

    Chain: (outcome, book_side, price_bps)
         -> (outcome, book_side, None)    # drop price
         -> (outcome, None, None)         # drop book_side
         -> (None, None, None)            # drop outcome (ticker aggregate)
    """
    import time as _time
    from unittest.mock import patch

    tracker = CPMTracker()
    now = _time.time()
    with patch("talos.cpm.time.time", return_value=now):
        with patch("talos.cpm._parse_iso", return_value=now - 60):
            # Single trade: produces yes-ASK-5500 and no-BID-4500, each 6 contracts.
            tracker.ingest(
                "KX-TEST",
                [
                    _make_trade(
                        trade_id="t1",
                        taker_side="yes",
                        yes_price_dollars="0.55",
                        count_fp="6",
                    ),
                ],
            )

        # Exact bucket has data: per-bucket CPM applies.
        # 6 contracts / 1 min = 6 cpm → ETA = 12 / 6 = 2 min.
        eta_exact = tracker.eta_minutes(
            "KX-TEST",
            queue_position=12,
            outcome="yes",
            book_side="ASK",
            price_bps=5500,
            window_sec=300.0,
        )
        assert eta_exact is not None
        assert abs(eta_exact - 2.0) < 0.01

        # Different price on same outcome+book_side: per-bucket empty →
        # fall back to (yes, ASK, *) which sees yes-ASK-5500.
        eta_drop_price = tracker.eta_minutes(
            "KX-TEST",
            queue_position=12,
            outcome="yes",
            book_side="ASK",
            price_bps=5400,
            window_sec=300.0,
        )
        assert eta_drop_price is not None
        assert abs(eta_drop_price - 2.0) < 0.01

        # yes-BID at 5500 has no data; (yes, BID, *) is empty (only yes-ASK exists).
        # Falls through to (yes, *, *) which sees yes-ASK-5500.
        eta_drop_book = tracker.eta_minutes(
            "KX-TEST",
            queue_position=12,
            outcome="yes",
            book_side="BID",
            price_bps=5500,
            window_sec=300.0,
        )
        assert eta_drop_book is not None
        assert abs(eta_drop_book - 2.0) < 0.01

        # no-ASK at 9999 has no data; (no, ASK, *) empty; (no, *, *) sees
        # no-BID-4500 → 6 cpm → ETA = 12 / 6 = 2 min.
        eta_drop_outcome = tracker.eta_minutes(
            "KX-TEST",
            queue_position=12,
            outcome="no",
            book_side="ASK",
            price_bps=9999,
            window_sec=300.0,
        )
        assert eta_drop_outcome is not None
        assert abs(eta_drop_outcome - 2.0) < 0.01


def test_ingest_skips_trade_with_invalid_price():
    """Trade with price_bps=0 (no recoverable price) is dropped, not bucketed at extremes."""
    # Build a trade directly with no usable price (bypass model_validate dollar path).
    bad_trade = Trade(
        ticker="KX-TEST",
        trade_id="bad",
        side="yes",
        created_time="2026-04-26T00:00:00Z",
        price_bps=0,
        count_fp100=100,
    )
    tracker = CPMTracker()
    tracker.ingest("KX-TEST", [bad_trade])
    # Nothing should be in any bucket at the extremes.
    yes_at_0 = FlowKey(ticker="KX-TEST", outcome="yes", book_side="ASK", price_bps=0)
    no_at_10000 = FlowKey(ticker="KX-TEST", outcome="no", book_side="BID", price_bps=10_000)
    assert tracker.flow_count(yes_at_0) == 0
    assert tracker.flow_count(no_at_10000) == 0
    # Not added to _seen — could re-evaluate if price becomes recoverable.
    assert "bad" not in tracker._seen
