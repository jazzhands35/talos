"""Tests for ArbitrageScanner."""

# Tests in this file construct models with legacy wire-shape parameter
# names that the models' _migrate_fp validators remap to canonical
# bps/fp100 fields at runtime. Pyright doesn't see validator remapping as
# part of the constructor signature.
# pyright: reportCallIssue=false

from __future__ import annotations

import time

import pytest

from talos.models.ws import OrderBookSnapshot
from talos.orderbook import OrderBookManager
from talos.scanner import ArbitrageScanner


def _setup_books(
    manager: OrderBookManager,
    no_a: int,
    qty_a: int,
    no_b: int,
    qty_b: int,
) -> None:
    """Set up two books with given NO bid prices and quantities."""
    manager.apply_snapshot(
        "GAME-STAN",
        OrderBookSnapshot(market_ticker="GAME-STAN", market_id="u1", yes=[], no=[[no_a, qty_a]]),
    )
    manager.apply_snapshot(
        "GAME-MIA",
        OrderBookSnapshot(market_ticker="GAME-MIA", market_id="u2", yes=[], no=[[no_b, qty_b]]),
    )


class TestPairManagement:
    @pytest.fixture()
    def scanner(self) -> ArbitrageScanner:
        return ArbitrageScanner(OrderBookManager())

    def test_add_pair(self, scanner: ArbitrageScanner) -> None:
        scanner.add_pair("EVT-1", "TICK-A", "TICK-B")
        assert len(scanner.pairs) == 1
        assert scanner.pairs[0].event_ticker == "EVT-1"

    def test_remove_pair(self, scanner: ArbitrageScanner) -> None:
        scanner.add_pair("EVT-1", "TICK-A", "TICK-B")
        scanner.remove_pair("EVT-1")
        assert len(scanner.pairs) == 0

    def test_remove_nonexistent_pair_is_noop(self, scanner: ArbitrageScanner) -> None:
        scanner.remove_pair("EVT-NOPE")
        assert len(scanner.pairs) == 0

    def test_duplicate_add_pair_is_noop(self, scanner: ArbitrageScanner) -> None:
        scanner.add_pair("EVT-1", "TICK-A", "TICK-B")
        scanner.add_pair("EVT-1", "TICK-A", "TICK-B")
        assert len(scanner.pairs) == 1

    def test_remove_pair_cleans_up_scan(self) -> None:
        manager = OrderBookManager()
        scanner = ArbitrageScanner(manager)
        scanner.add_pair("EVT-1", "GAME-STAN", "GAME-MIA")
        _setup_books(manager, no_a=38, qty_a=100, no_b=55, qty_b=200)
        scanner.remove_pair("EVT-1")
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 0


class TestScanFindsOpportunity:
    @pytest.fixture()
    def manager(self) -> OrderBookManager:
        return OrderBookManager()

    @pytest.fixture()
    def scanner(self, manager: OrderBookManager) -> ArbitrageScanner:
        s = ArbitrageScanner(manager)
        s.add_pair("GAME-1", "GAME-STAN", "GAME-MIA")
        return s

    def test_detects_positive_edge(
        self, scanner: ArbitrageScanner, manager: OrderBookManager
    ) -> None:
        # NO-A=38, NO-B=55 → cost=93, edge=7
        _setup_books(manager, no_a=38, qty_a=100, no_b=55, qty_b=200)
        scanner.scan("GAME-STAN")
        opps = scanner.opportunities
        assert len(opps) == 1
        assert opps[0].raw_edge == 7
        assert opps[0].no_a == 38
        assert opps[0].no_b == 55

    def test_tradeable_qty_is_min(
        self, scanner: ArbitrageScanner, manager: OrderBookManager
    ) -> None:
        _setup_books(manager, no_a=38, qty_a=100, no_b=55, qty_b=200)
        scanner.scan("GAME-STAN")
        assert scanner.opportunities[0].tradeable_qty == 100

    def test_scan_from_either_leg(
        self, scanner: ArbitrageScanner, manager: OrderBookManager
    ) -> None:
        _setup_books(manager, no_a=38, qty_a=100, no_b=55, qty_b=200)
        scanner.scan("GAME-MIA")
        assert len(scanner.opportunities) == 1


class TestScanNoOpportunity:
    @pytest.fixture()
    def manager(self) -> OrderBookManager:
        return OrderBookManager()

    @pytest.fixture()
    def scanner(self, manager: OrderBookManager) -> ArbitrageScanner:
        s = ArbitrageScanner(manager)
        s.add_pair("GAME-1", "GAME-STAN", "GAME-MIA")
        return s

    def test_no_edge_when_sum_over_100(
        self, scanner: ArbitrageScanner, manager: OrderBookManager
    ) -> None:
        # NO-A=60, NO-B=50 → cost=110, edge=-10
        _setup_books(manager, no_a=60, qty_a=100, no_b=50, qty_b=100)
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 0

    def test_no_edge_when_sum_exactly_100(
        self, scanner: ArbitrageScanner, manager: OrderBookManager
    ) -> None:
        _setup_books(manager, no_a=50, qty_a=100, no_b=50, qty_b=100)
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 0

    def test_removes_vanished_opportunity(
        self, scanner: ArbitrageScanner, manager: OrderBookManager
    ) -> None:
        _setup_books(manager, no_a=38, qty_a=100, no_b=55, qty_b=200)
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 1
        _setup_books(manager, no_a=60, qty_a=100, no_b=50, qty_b=200)
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 0


class TestScanEdgeCases:
    @pytest.fixture()
    def manager(self) -> OrderBookManager:
        return OrderBookManager()

    @pytest.fixture()
    def scanner(self, manager: OrderBookManager) -> ArbitrageScanner:
        s = ArbitrageScanner(manager)
        s.add_pair("GAME-1", "GAME-STAN", "GAME-MIA")
        return s

    def test_missing_book_for_one_leg(
        self, scanner: ArbitrageScanner, manager: OrderBookManager
    ) -> None:
        manager.apply_snapshot(
            "GAME-STAN",
            OrderBookSnapshot(market_ticker="GAME-STAN", market_id="u1", yes=[], no=[[38, 100]]),
        )
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 0

    def test_empty_book_no_bids(self, scanner: ArbitrageScanner, manager: OrderBookManager) -> None:
        manager.apply_snapshot(
            "GAME-STAN",
            OrderBookSnapshot(market_ticker="GAME-STAN", market_id="u1", yes=[], no=[]),
        )
        manager.apply_snapshot(
            "GAME-MIA",
            OrderBookSnapshot(market_ticker="GAME-MIA", market_id="u2", yes=[], no=[[55, 100]]),
        )
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 0

    def test_stale_book_skipped(self, scanner: ArbitrageScanner, manager: OrderBookManager) -> None:
        _setup_books(manager, no_a=38, qty_a=100, no_b=55, qty_b=200)
        book = manager.get_book("GAME-STAN")
        assert book is not None
        # Make book stale by setting last_update to >120s ago
        book.last_update = time.time() - 121.0
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 0

    def test_scan_unrelated_ticker_is_noop(
        self, scanner: ArbitrageScanner, manager: OrderBookManager
    ) -> None:
        _setup_books(manager, no_a=38, qty_a=100, no_b=55, qty_b=200)
        scanner.scan("UNRELATED")
        assert len(scanner.opportunities) == 0

    def test_updates_existing_opportunity(
        self, scanner: ArbitrageScanner, manager: OrderBookManager
    ) -> None:
        _setup_books(manager, no_a=38, qty_a=100, no_b=55, qty_b=200)
        scanner.scan("GAME-STAN")
        assert scanner.opportunities[0].raw_edge == 7
        # Improve edge: NO-A drops from 38 to 35
        _setup_books(manager, no_a=35, qty_a=150, no_b=55, qty_b=200)
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 1
        assert scanner.opportunities[0].raw_edge == 10
        assert scanner.opportunities[0].tradeable_qty == 150


class TestOpportunitySorting:
    def test_sorted_by_edge_descending(self) -> None:
        manager = OrderBookManager()
        scanner = ArbitrageScanner(manager)
        scanner.add_pair("GAME-1", "GAME-A1", "GAME-B1")
        scanner.add_pair("GAME-2", "GAME-A2", "GAME-B2")
        # Pair 1: NO=45+50=95, edge=5
        manager.apply_snapshot(
            "GAME-A1",
            OrderBookSnapshot(market_ticker="GAME-A1", market_id="u1", yes=[], no=[[45, 100]]),
        )
        manager.apply_snapshot(
            "GAME-B1",
            OrderBookSnapshot(market_ticker="GAME-B1", market_id="u2", yes=[], no=[[50, 100]]),
        )
        # Pair 2: NO=40+50=90, edge=10
        manager.apply_snapshot(
            "GAME-A2",
            OrderBookSnapshot(market_ticker="GAME-A2", market_id="u3", yes=[], no=[[40, 100]]),
        )
        manager.apply_snapshot(
            "GAME-B2",
            OrderBookSnapshot(market_ticker="GAME-B2", market_id="u4", yes=[], no=[[50, 100]]),
        )
        scanner.scan("GAME-A1")
        scanner.scan("GAME-A2")
        opps = scanner.opportunities
        assert len(opps) == 2
        assert opps[0].raw_edge == 10
        assert opps[1].raw_edge == 5


class TestGetOpportunity:
    def test_returns_opportunity_with_positive_edge(self) -> None:
        manager = OrderBookManager()
        scanner = ArbitrageScanner(manager)
        scanner.add_pair("EVT-1", "MKT-A", "MKT-B")
        manager.apply_snapshot(
            "MKT-A",
            OrderBookSnapshot(market_ticker="MKT-A", market_id="u1", yes=[], no=[[45, 100]]),
        )
        manager.apply_snapshot(
            "MKT-B",
            OrderBookSnapshot(market_ticker="MKT-B", market_id="u2", yes=[], no=[[50, 100]]),
        )
        scanner.scan("MKT-A")
        opp = scanner.get_opportunity("EVT-1")
        assert opp is not None
        assert opp.raw_edge == 5

    def test_returns_none_when_no_edge(self) -> None:
        manager = OrderBookManager()
        scanner = ArbitrageScanner(manager)
        scanner.add_pair("EVT-1", "MKT-A", "MKT-B")
        manager.apply_snapshot(
            "MKT-A",
            OrderBookSnapshot(market_ticker="MKT-A", market_id="u1", yes=[], no=[[55, 100]]),
        )
        manager.apply_snapshot(
            "MKT-B",
            OrderBookSnapshot(market_ticker="MKT-B", market_id="u2", yes=[], no=[[50, 100]]),
        )
        scanner.scan("MKT-A")
        assert scanner.get_opportunity("EVT-1") is None

    def test_returns_none_for_unknown_event(self) -> None:
        scanner = ArbitrageScanner(OrderBookManager())
        assert scanner.get_opportunity("NONEXISTENT") is None


class TestAllSnapshots:
    def test_includes_negative_edge_pairs(self) -> None:
        manager = OrderBookManager()
        scanner = ArbitrageScanner(manager)
        scanner.add_pair("EVT-1", "MKT-A", "MKT-B")
        manager.apply_snapshot(
            "MKT-A",
            OrderBookSnapshot(market_ticker="MKT-A", market_id="u1", yes=[], no=[[55, 100]]),
        )
        manager.apply_snapshot(
            "MKT-B",
            OrderBookSnapshot(market_ticker="MKT-B", market_id="u2", yes=[], no=[[50, 100]]),
        )
        scanner.scan("MKT-A")
        # No positive-edge opportunity
        assert len(scanner.opportunities) == 0
        # But all_snapshots still has the pair
        snaps = scanner.all_snapshots
        assert "EVT-1" in snaps
        assert snaps["EVT-1"].raw_edge == -5

    def test_placeholder_before_book_data(self) -> None:
        """Pair appears in all_snapshots immediately after add_pair, before any scan."""
        scanner = ArbitrageScanner(OrderBookManager())
        scanner.add_pair("EVT-1", "MKT-A", "MKT-B")
        snaps = scanner.all_snapshots
        assert "EVT-1" in snaps
        assert snaps["EVT-1"].no_a == 0
        assert snaps["EVT-1"].no_b == 0


class TestYesNoPairScanning:
    """Tests for YES/NO arb on a single market (same ticker)."""

    def test_add_yesno_pair(self):
        scanner = ArbitrageScanner(OrderBookManager())
        scanner.add_pair(
            "MKT-TICKER",
            "MKT-TICKER",
            "MKT-TICKER",
            side_a="yes",
            side_b="no",
        )
        assert len(scanner.pairs) == 1
        pair = scanner.pairs[0]
        assert pair.side_a == "yes"
        assert pair.side_b == "no"
        assert pair.is_same_ticker is True

    def test_yesno_edge_detection(self):
        """YES ask=48, NO ask=45 -> edge = 100-48-45 = 7."""
        mgr = OrderBookManager()
        scanner = ArbitrageScanner(mgr)
        scanner.add_pair(
            "MKT-TICKER",
            "MKT-TICKER",
            "MKT-TICKER",
            side_a="yes",
            side_b="no",
        )
        mgr.apply_snapshot(
            "MKT-TICKER",
            OrderBookSnapshot(
                market_ticker="MKT-TICKER",
                market_id="m1",
                yes=[[48, 100]],
                no=[[45, 200]],
            ),
        )
        scanner.scan("MKT-TICKER")
        opps = scanner.opportunities
        assert len(opps) == 1
        assert opps[0].raw_edge == 7
        assert opps[0].no_a == 48  # YES side price (leg A)
        assert opps[0].no_b == 45  # NO side price (leg B)
        assert opps[0].tradeable_qty == 100

    def test_yesno_no_edge(self):
        """YES ask=55, NO ask=48 -> edge = -3, no opportunity."""
        mgr = OrderBookManager()
        scanner = ArbitrageScanner(mgr)
        scanner.add_pair(
            "MKT-TICKER",
            "MKT-TICKER",
            "MKT-TICKER",
            side_a="yes",
            side_b="no",
        )
        mgr.apply_snapshot(
            "MKT-TICKER",
            OrderBookSnapshot(
                market_ticker="MKT-TICKER",
                market_id="m1",
                yes=[[55, 100]],
                no=[[48, 200]],
            ),
        )
        scanner.scan("MKT-TICKER")
        assert len(scanner.opportunities) == 0

    def test_yesno_missing_yes_side(self):
        """Empty YES book -> no opportunity."""
        mgr = OrderBookManager()
        scanner = ArbitrageScanner(mgr)
        scanner.add_pair(
            "MKT-TICKER",
            "MKT-TICKER",
            "MKT-TICKER",
            side_a="yes",
            side_b="no",
        )
        mgr.apply_snapshot(
            "MKT-TICKER",
            OrderBookSnapshot(
                market_ticker="MKT-TICKER",
                market_id="m1",
                yes=[],
                no=[[45, 200]],
            ),
        )
        scanner.scan("MKT-TICKER")
        assert len(scanner.opportunities) == 0

    def test_cross_no_still_works(self):
        """Existing NO+NO behavior is unbroken."""
        mgr = OrderBookManager()
        scanner = ArbitrageScanner(mgr)
        scanner.add_pair("EVT", "TK-A", "TK-B")
        mgr.apply_snapshot(
            "TK-A",
            OrderBookSnapshot(
                market_ticker="TK-A",
                market_id="m1",
                yes=[],
                no=[[45, 100]],
            ),
        )
        mgr.apply_snapshot(
            "TK-B",
            OrderBookSnapshot(
                market_ticker="TK-B",
                market_id="m2",
                yes=[],
                no=[[48, 100]],
            ),
        )
        scanner.scan("TK-A")
        opps = scanner.opportunities
        assert len(opps) == 1
        assert opps[0].raw_edge == 7


class TestDynamicFeeRate:
    """Phase 9: scanner uses pair-specific fee rate for edge calculation."""

    def test_fee_edge_uses_custom_rate(self) -> None:
        from talos.fees import fee_adjusted_edge_bps
        from talos.units import ONE_CENT_BPS

        manager = OrderBookManager()
        scanner = ArbitrageScanner(manager)
        scanner.add_pair("GAME-1", "GAME-STAN", "GAME-MIA", fee_rate=0.03)
        _setup_books(manager, no_a=38, qty_a=100, no_b=55, qty_b=100)
        scanner.scan("GAME-STAN")

        opp = scanner.opportunities[0]
        expected = (
            fee_adjusted_edge_bps(38 * ONE_CENT_BPS, 55 * ONE_CENT_BPS, rate=0.03) / ONE_CENT_BPS
        )
        assert opp.fee_edge == pytest.approx(expected)

    def test_default_fee_rate(self) -> None:
        from talos.fees import fee_adjusted_edge_bps
        from talos.units import ONE_CENT_BPS

        manager = OrderBookManager()
        scanner = ArbitrageScanner(manager)
        scanner.add_pair("GAME-1", "GAME-STAN", "GAME-MIA")  # default rate
        _setup_books(manager, no_a=38, qty_a=100, no_b=55, qty_b=100)
        scanner.scan("GAME-STAN")

        opp = scanner.opportunities[0]
        expected = (
            fee_adjusted_edge_bps(38 * ONE_CENT_BPS, 55 * ONE_CENT_BPS) / ONE_CENT_BPS
        )  # default rate
        assert opp.fee_edge == pytest.approx(expected)

    def test_pair_stores_fee_rate(self) -> None:
        scanner = ArbitrageScanner(OrderBookManager())
        scanner.add_pair("EVT-1", "A", "B", fee_type="flat", fee_rate=0.05)
        assert scanner.pairs[0].fee_type == "flat"
        assert scanner.pairs[0].fee_rate == 0.05
