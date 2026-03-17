"""Tests for strategy models (ArbPair, Opportunity, BidConfirmation)."""

from __future__ import annotations

from talos.models.strategy import ArbPair, BidConfirmation, Opportunity


class TestArbPair:
    def test_construction(self):
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        assert pair.event_ticker == "EVT-1"
        assert pair.ticker_a == "TK-A"
        assert pair.ticker_b == "TK-B"

    def test_defaults(self):
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        assert pair.fee_type == "quadratic_with_maker_fees"
        assert pair.fee_rate == 0.0175
        assert pair.close_time is None

    def test_custom_fee_rate(self):
        pair = ArbPair(
            event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B",
            fee_type="flat", fee_rate=0.02,
        )
        assert pair.fee_type == "flat"
        assert pair.fee_rate == 0.02

    def test_with_close_time(self):
        pair = ArbPair(
            event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B",
            close_time="2026-03-15T22:00:00Z",
        )
        assert pair.close_time == "2026-03-15T22:00:00Z"


class TestOpportunity:
    def _make(self, **overrides) -> Opportunity:
        defaults = dict(
            event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B",
            no_a=45, no_b=48, qty_a=100, qty_b=50,
            raw_edge=7, fee_edge=5.5, tradeable_qty=50,
            timestamp="2026-03-13T12:00:00Z",
        )
        defaults.update(overrides)
        return Opportunity(**defaults)

    def test_construction(self):
        opp = self._make()
        assert opp.event_ticker == "EVT-1"
        assert opp.no_a == 45
        assert opp.no_b == 48

    def test_cost_property(self):
        opp = self._make(no_a=45, no_b=48)
        assert opp.cost == 93

    def test_cost_at_breakeven(self):
        opp = self._make(no_a=50, no_b=50)
        assert opp.cost == 100

    def test_tradeable_qty_stored(self):
        """tradeable_qty is set by the scanner, not computed by the model."""
        opp = self._make(tradeable_qty=30)
        assert opp.tradeable_qty == 30

    def test_defaults(self):
        opp = self._make()
        assert opp.close_time is None
        assert opp.fee_rate == 0.0175

    def test_custom_fee_rate(self):
        opp = self._make(fee_rate=0.03)
        assert opp.fee_rate == 0.03


class TestBidConfirmation:
    def test_construction(self):
        bid = BidConfirmation(
            ticker_a="TK-A", ticker_b="TK-B",
            no_a=45, no_b=48, qty=10,
        )
        assert bid.ticker_a == "TK-A"
        assert bid.no_a == 45
        assert bid.qty == 10
