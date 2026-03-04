"""Tests for ArbitrageScanner."""

from __future__ import annotations

import pytest

from talos.models.ws import OrderBookSnapshot
from talos.orderbook import OrderBookManager
from talos.scanner import ArbitrageScanner


def _setup_books(
    manager: OrderBookManager,
    bid_a: int,
    qty_a: int,
    bid_b: int,
    qty_b: int,
) -> None:
    """Set up two books with given YES bid prices and quantities."""
    manager.apply_snapshot(
        "GAME-STAN",
        OrderBookSnapshot(market_ticker="GAME-STAN", market_id="u1", yes=[[bid_a, qty_a]], no=[]),
    )
    manager.apply_snapshot(
        "GAME-MIA",
        OrderBookSnapshot(market_ticker="GAME-MIA", market_id="u2", yes=[[bid_b, qty_b]], no=[]),
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
        _setup_books(manager, bid_a=62, qty_a=100, bid_b=45, qty_b=200)
        scanner.scan("GAME-STAN")
        opps = scanner.opportunities
        assert len(opps) == 1
        assert opps[0].raw_edge == 7
        assert opps[0].no_a == 38
        assert opps[0].no_b == 55

    def test_tradeable_qty_is_min(
        self, scanner: ArbitrageScanner, manager: OrderBookManager
    ) -> None:
        _setup_books(manager, bid_a=62, qty_a=100, bid_b=45, qty_b=200)
        scanner.scan("GAME-STAN")
        assert scanner.opportunities[0].tradeable_qty == 100

    def test_scan_from_either_leg(
        self, scanner: ArbitrageScanner, manager: OrderBookManager
    ) -> None:
        _setup_books(manager, bid_a=62, qty_a=100, bid_b=45, qty_b=200)
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

    def test_no_edge_when_sum_under_100(
        self, scanner: ArbitrageScanner, manager: OrderBookManager
    ) -> None:
        _setup_books(manager, bid_a=40, qty_a=100, bid_b=50, qty_b=100)
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 0

    def test_no_edge_when_sum_exactly_100(
        self, scanner: ArbitrageScanner, manager: OrderBookManager
    ) -> None:
        _setup_books(manager, bid_a=50, qty_a=100, bid_b=50, qty_b=100)
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 0

    def test_removes_vanished_opportunity(
        self, scanner: ArbitrageScanner, manager: OrderBookManager
    ) -> None:
        _setup_books(manager, bid_a=62, qty_a=100, bid_b=45, qty_b=200)
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 1
        _setup_books(manager, bid_a=40, qty_a=100, bid_b=50, qty_b=200)
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
            OrderBookSnapshot(market_ticker="GAME-STAN", market_id="u1", yes=[[62, 100]], no=[]),
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
            OrderBookSnapshot(market_ticker="GAME-MIA", market_id="u2", yes=[[45, 100]], no=[]),
        )
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 0

    def test_stale_book_skipped(self, scanner: ArbitrageScanner, manager: OrderBookManager) -> None:
        _setup_books(manager, bid_a=62, qty_a=100, bid_b=45, qty_b=200)
        book = manager.get_book("GAME-STAN")
        assert book is not None
        book.stale = True
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 0

    def test_scan_unrelated_ticker_is_noop(
        self, scanner: ArbitrageScanner, manager: OrderBookManager
    ) -> None:
        _setup_books(manager, bid_a=62, qty_a=100, bid_b=45, qty_b=200)
        scanner.scan("UNRELATED")
        assert len(scanner.opportunities) == 0

    def test_updates_existing_opportunity(
        self, scanner: ArbitrageScanner, manager: OrderBookManager
    ) -> None:
        _setup_books(manager, bid_a=62, qty_a=100, bid_b=45, qty_b=200)
        scanner.scan("GAME-STAN")
        assert scanner.opportunities[0].raw_edge == 7
        _setup_books(manager, bid_a=65, qty_a=150, bid_b=45, qty_b=200)
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
        manager.apply_snapshot(
            "GAME-A1",
            OrderBookSnapshot(market_ticker="GAME-A1", market_id="u1", yes=[[55, 100]], no=[]),
        )
        manager.apply_snapshot(
            "GAME-B1",
            OrderBookSnapshot(market_ticker="GAME-B1", market_id="u2", yes=[[50, 100]], no=[]),
        )
        manager.apply_snapshot(
            "GAME-A2",
            OrderBookSnapshot(market_ticker="GAME-A2", market_id="u3", yes=[[60, 100]], no=[]),
        )
        manager.apply_snapshot(
            "GAME-B2",
            OrderBookSnapshot(market_ticker="GAME-B2", market_id="u4", yes=[[50, 100]], no=[]),
        )
        scanner.scan("GAME-A1")
        scanner.scan("GAME-A2")
        opps = scanner.opportunities
        assert len(opps) == 2
        assert opps[0].raw_edge == 10
        assert opps[1].raw_edge == 5
