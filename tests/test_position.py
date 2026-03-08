"""Tests for position display computation via compute_display_positions.

These tests construct PositionLedger instances with record_fill()/record_resting()
instead of building raw Order objects.  Same scenarios as the original
compute_event_positions tests, adapted for the ledger-based API.
"""

from __future__ import annotations

import pytest

from talos.cpm import CPMTracker
from talos.fees import MAKER_FEE_RATE
from talos.models.strategy import ArbPair
from talos.position_ledger import PositionLedger, Side, compute_display_positions

PAIR = ArbPair(event_ticker="EVT-AB", ticker_a="MKT-A", ticker_b="MKT-B")


def _ledger(
    *,
    fill_a: tuple[int, int] | None = None,
    fill_b: tuple[int, int] | None = None,
    resting_a: tuple[str, int, int] | None = None,
    resting_b: tuple[str, int, int] | None = None,
) -> PositionLedger:
    """Helper to build a ledger with optional fills and resting orders."""
    ledger = PositionLedger(event_ticker="EVT-AB", unit_size=10)
    if fill_a is not None:
        ledger.record_fill(Side.A, count=fill_a[0], price=fill_a[1])
    if fill_b is not None:
        ledger.record_fill(Side.B, count=fill_b[0], price=fill_b[1])
    if resting_a is not None:
        ledger.record_resting(Side.A, order_id=resting_a[0], count=resting_a[1], price=resting_a[2])
    if resting_b is not None:
        ledger.record_resting(Side.B, order_id=resting_b[0], count=resting_b[1], price=resting_b[2])
    return ledger


def _compute(ledger: PositionLedger, queue_cache: dict[str, int] | None = None):
    return compute_display_positions(
        {"EVT-AB": ledger}, [PAIR], queue_cache or {}, CPMTracker()
    )


class TestNoPositions:
    def test_empty_ledger_returns_empty(self) -> None:
        ledger = PositionLedger(event_ticker="EVT-AB", unit_size=10)
        assert _compute(ledger) == []

    def test_no_pairs_returns_empty(self) -> None:
        ledger = _ledger(fill_a=(3, 31))
        result = compute_display_positions({"EVT-AB": ledger}, [], {}, CPMTracker())
        assert result == []


class TestBothLegsMatched:
    def test_equal_fills_compute_locked_profit(self) -> None:
        ledger = _ledger(fill_a=(5, 31), fill_b=(5, 67))
        result = _compute(ledger)
        assert len(result) == 1
        s = result[0]
        assert s.matched_pairs == 5
        # Raw: 100 - 31 - 67 = 2c/pair -> 10c total
        # Fee (worst case, B wins): (500 - 155) * 0.0175 = 6.0375
        raw = 5 * (100 - 31 - 67)
        worst_fee = (5 * 100 - 5 * 31) * MAKER_FEE_RATE
        assert s.locked_profit_cents == pytest.approx(raw - worst_fee)
        assert s.unmatched_a == 0
        assert s.unmatched_b == 0
        assert s.exposure_cents == 0


class TestOneLegAhead:
    def test_leg_a_ahead_shows_exposure(self) -> None:
        ledger = _ledger(fill_a=(5, 31), fill_b=(3, 67))
        result = _compute(ledger)
        s = result[0]
        assert s.matched_pairs == 3
        raw = 3 * (100 - 31 - 67)
        worst_fee = (3 * 100 - 3 * 31) * MAKER_FEE_RATE
        assert s.locked_profit_cents == pytest.approx(raw - worst_fee)
        assert s.unmatched_a == 2
        assert s.unmatched_b == 0
        assert s.exposure_cents == 2 * 31

    def test_leg_b_ahead_shows_exposure(self) -> None:
        ledger = _ledger(fill_a=(2, 31), fill_b=(5, 67))
        result = _compute(ledger)
        s = result[0]
        assert s.matched_pairs == 2
        assert s.unmatched_a == 0
        assert s.unmatched_b == 3
        assert s.exposure_cents == 3 * 67


class TestMultipleFillsAccumulate:
    def test_two_fills_same_side_accumulate(self) -> None:
        ledger = PositionLedger(event_ticker="EVT-AB", unit_size=10)
        ledger.record_fill(Side.A, count=3, price=31)
        ledger.record_fill(Side.A, count=2, price=33)
        ledger.record_fill(Side.B, count=5, price=67)
        result = _compute(ledger)
        s = result[0]
        assert s.leg_a.filled_count == 5  # 3 + 2
        # Weighted average: (31*3 + 33*2) / 5 = 159/5 = 31
        assert s.leg_a.no_price == 31
        assert s.matched_pairs == 5
        raw = 500 - 159 - 335
        worst_fee = (500 - 159) * MAKER_FEE_RATE
        assert s.locked_profit_cents == pytest.approx(raw - worst_fee)


class TestMixedPricePnL:
    def test_mixed_prices_both_legs_correct_pnl(self) -> None:
        ledger = PositionLedger(event_ticker="EVT-AB", unit_size=20)
        ledger.record_fill(Side.A, count=5, price=34)
        ledger.record_fill(Side.A, count=5, price=35)
        ledger.record_fill(Side.B, count=5, price=64)
        ledger.record_fill(Side.B, count=5, price=67)
        result = _compute(ledger)
        s = result[0]
        assert s.matched_pairs == 10
        # Costs: A = 170+175=345, B = 320+335=655, total=1000
        assert s.locked_profit_cents < 0  # fees make break-even into a loss
        assert s.exposure_cents == 0


class TestLegSummaryFields:
    def test_leg_summary_populated(self) -> None:
        ledger = _ledger(
            fill_a=(3, 31), fill_b=(3, 67),
            resting_a=("ord-a", 2, 31), resting_b=("ord-b", 2, 67),
        )
        result = _compute(ledger)
        s = result[0]
        assert s.leg_a.ticker == "MKT-A"
        assert s.leg_a.no_price == 31
        assert s.leg_a.filled_count == 3
        assert s.leg_a.resting_count == 2
        assert s.leg_b.ticker == "MKT-B"
        assert s.leg_b.no_price == 67

    def test_total_fill_cost_propagated(self) -> None:
        ledger = _ledger(fill_a=(3, 31), fill_b=(3, 67))
        result = _compute(ledger)
        s = result[0]
        assert s.leg_a.total_fill_cost == 3 * 31
        assert s.leg_b.total_fill_cost == 3 * 67


class TestRestingOnlyPair:
    def test_resting_only_still_shows_summary(self) -> None:
        ledger = _ledger(
            resting_a=("ord-a", 5, 31),
            resting_b=("ord-b", 5, 67),
        )
        result = _compute(ledger)
        assert len(result) == 1
        s = result[0]
        assert s.matched_pairs == 0
        assert s.locked_profit_cents == 0
        assert s.exposure_cents == 0


class TestQueuePosition:
    def test_queue_position_from_cache(self) -> None:
        ledger = _ledger(resting_a=("ord-a", 5, 31), resting_b=("ord-b", 5, 67))
        result = _compute(ledger, queue_cache={"ord-a": 8, "ord-b": 42})
        assert result[0].leg_a.queue_position == 8
        assert result[0].leg_b.queue_position == 42

    def test_queue_position_none_when_no_cache(self) -> None:
        ledger = _ledger(resting_a=("ord-a", 5, 31), resting_b=("ord-b", 5, 67))
        result = _compute(ledger)
        assert result[0].leg_a.queue_position is None
        assert result[0].leg_b.queue_position is None

    def test_queue_position_none_when_no_resting(self) -> None:
        ledger = _ledger(fill_a=(5, 31), fill_b=(5, 67))
        result = _compute(ledger, queue_cache={"ord-a": 8})
        assert result[0].leg_a.queue_position is None
        assert result[0].leg_b.queue_position is None
