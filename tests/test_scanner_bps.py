"""Scanner bps/fp100 migration tests (Task 7a).

Covers:
  1. Whole-cent parity between legacy cents fields and new bps/fp100 siblings.
  2. Sub-cent correctness — exact edge on DJT-class markets.
  3. Sub-cent rejection of negative-edge cases.
  4. ``fee_edge_bps`` matches ``fee_adjusted_edge_bps`` exactly.
  5. Opportunity dual-field invariant for every whole-cent input.
  6. Admission check uses the exact-bps path (cents-rounded == 0 path).
  7. Sort order uses the exact-bps path.
"""

from __future__ import annotations

import time

import pytest

from talos.fees import fee_adjusted_edge_bps
from talos.models.market import OrderBookLevel
from talos.models.ws import OrderBookSnapshot
from talos.orderbook import OrderBookManager
from talos.scanner import ArbitrageScanner


def _setup_whole_cent_books(
    manager: OrderBookManager,
    no_a: int,
    qty_a: int,
    no_b: int,
    qty_b: int,
    *,
    ticker_a: str = "GAME-STAN",
    ticker_b: str = "GAME-MIA",
) -> None:
    """Apply standard whole-cent snapshots via the WS path.

    This exercises the ``_level_price_bps`` fallback (price_bps=0 in the
    constructed levels → derived from ``price`` via ``cents_to_bps``).
    """
    manager.apply_snapshot(
        ticker_a,
        OrderBookSnapshot(
            market_ticker=ticker_a, market_id="u1", yes=[], no=[[no_a, qty_a]]
        ),
    )
    manager.apply_snapshot(
        ticker_b,
        OrderBookSnapshot(
            market_ticker=ticker_b, market_id="u2", yes=[], no=[[no_b, qty_b]]
        ),
    )


def _install_subcent_book(
    manager: OrderBookManager,
    ticker: str,
    price: int,
    price_bps: int,
    quantity: int,
    quantity_fp100: int,
) -> None:
    """Install a single-level NO book with an exact-bps level.

    The WS parser only populates cents/contracts; sub-cent values must be
    injected by constructing an OrderBookLevel with both legacy and new
    fields populated, then replacing the manager's level list.
    """
    manager.apply_snapshot(
        ticker,
        OrderBookSnapshot(
            market_ticker=ticker, market_id="u", yes=[], no=[[price, quantity]]
        ),
    )
    book = manager.get_book(ticker)
    assert book is not None
    book.no = [
        OrderBookLevel(
            price=price,
            quantity=quantity,
            price_bps=price_bps,
            quantity_fp100=quantity_fp100,
        )
    ]
    book.last_update = time.time()


class TestWholeCentParity:
    """Whole-cent markets — legacy cents fields and bps/fp100 siblings agree."""

    @pytest.mark.parametrize(
        "no_a,no_b",
        [(38, 55), (45, 50), (10, 85), (1, 98), (49, 49)],
    )
    def test_parity_across_representative_prices(
        self, no_a: int, no_b: int
    ) -> None:
        manager = OrderBookManager()
        scanner = ArbitrageScanner(manager)
        scanner.add_pair("EVT", "GAME-STAN", "GAME-MIA")
        _setup_whole_cent_books(manager, no_a=no_a, qty_a=100, no_b=no_b, qty_b=200)
        scanner.scan("GAME-STAN")

        # Some of these pairs have non-positive edge; pull from snapshots
        # for a uniform view.
        opp = scanner.all_snapshots["EVT"]
        assert opp.no_a_bps == opp.no_a * 100
        assert opp.no_b_bps == opp.no_b * 100
        assert opp.qty_a_fp100 == opp.qty_a * 100
        assert opp.qty_b_fp100 == opp.qty_b * 100
        assert opp.raw_edge_bps == opp.raw_edge * 100


class TestSubCentCorrectness:
    """Sub-cent markets — the reason Task 7a exists."""

    def test_djt_class_positive_edge_admitted(self) -> None:
        """DJT-like book: 3.8¢ / 96.1¢ → legacy edge=0, bps edge=10."""
        manager = OrderBookManager()
        scanner = ArbitrageScanner(manager)
        scanner.add_pair("DJT-EVT", "DJT-A", "DJT-B")

        # 3.8¢ rounds to 4¢ legacy, 380 bps exact
        _install_subcent_book(
            manager,
            "DJT-A",
            price=4,
            price_bps=380,
            quantity=100,
            quantity_fp100=10_000,
        )
        # 96.1¢ rounds to 96¢ legacy, 9610 bps exact
        _install_subcent_book(
            manager,
            "DJT-B",
            price=96,
            price_bps=9610,
            quantity=200,
            quantity_fp100=20_000,
        )

        scanner.scan("DJT-A")
        opps = scanner.opportunities
        assert len(opps) == 1, "sub-cent positive-edge opp must be admitted"
        opp = opps[0]

        # Exact bps edge: 10000 - 380 - 9610 = 10 bps = 0.10¢.
        assert opp.raw_edge_bps == 10

        # Legacy cents view: 100 - 4 - 96 = 0, but scanner derives via
        # bps_to_cents_round(10) = 0. Stored value is the rounded view.
        assert opp.raw_edge == 0
        # The critical correctness property: admission keyed on bps, not cents.
        assert opp in scanner.opportunities

    def test_genuinely_negative_subcent_rejected(self) -> None:
        """Negative-edge sub-cent market → not admitted."""
        manager = OrderBookManager()
        scanner = ArbitrageScanner(manager)
        scanner.add_pair("NEG-EVT", "NEG-A", "NEG-B")

        # 52.5¢ + 47.6¢ = 100.1¢ → edge = -10 bps
        _install_subcent_book(
            manager,
            "NEG-A",
            price=53,
            price_bps=5250,
            quantity=100,
            quantity_fp100=10_000,
        )
        _install_subcent_book(
            manager,
            "NEG-B",
            price=48,
            price_bps=4760,
            quantity=100,
            quantity_fp100=10_000,
        )
        scanner.scan("NEG-A")
        opps = scanner.opportunities
        assert len(opps) == 0

        snap = scanner.all_snapshots["NEG-EVT"]
        assert snap.raw_edge_bps == -10
        assert snap.raw_edge_bps <= 0


class TestFeeEdgeBps:
    """``fee_edge_bps`` is computed via the exact bps formula."""

    def test_matches_fee_adjusted_edge_bps_exactly(self) -> None:
        manager = OrderBookManager()
        scanner = ArbitrageScanner(manager)
        scanner.add_pair("EVT", "GAME-STAN", "GAME-MIA", fee_rate=0.0175)
        # 25¢ / 50¢ — representative whole-cent pair.
        _setup_whole_cent_books(manager, no_a=25, qty_a=100, no_b=50, qty_b=100)
        scanner.scan("GAME-STAN")

        opp = scanner.opportunities[0]
        expected = fee_adjusted_edge_bps(2500, 5000, rate=0.0175)
        assert opp.fee_edge_bps == expected


class TestOpportunityDualFieldInvariant:
    """For whole-cent inputs, parallel fields satisfy ×100 invariant."""

    def test_invariant_holds_for_emitted_opportunity(self) -> None:
        manager = OrderBookManager()
        scanner = ArbitrageScanner(manager)
        scanner.add_pair("EVT", "GAME-STAN", "GAME-MIA")
        _setup_whole_cent_books(manager, no_a=38, qty_a=100, no_b=55, qty_b=200)
        scanner.scan("GAME-STAN")

        opp = scanner.opportunities[0]
        assert opp.raw_edge_bps == opp.raw_edge * 100
        assert opp.no_a_bps == opp.no_a * 100
        assert opp.no_b_bps == opp.no_b * 100
        assert opp.qty_a_fp100 == opp.qty_a * 100
        assert opp.qty_b_fp100 == opp.qty_b * 100
        assert opp.tradeable_qty_fp100 == opp.tradeable_qty * 100
        assert opp.cost_bps == opp.cost * 100


class TestAdmissionGatedByBps:
    """Scanner admits opportunities when cents-rounded edge is 0 but
    exact-bps edge is strictly positive."""

    def test_hundred_bps_edge_on_subcent_market_admitted(self) -> None:
        """100 bps edge (= 1¢, but across sub-cent boundary)."""
        manager = OrderBookManager()
        scanner = ArbitrageScanner(manager)
        scanner.add_pair("SUB-EVT", "SUB-A", "SUB-B")

        # 4.5¢ + 94.5¢ = 99¢ → edge = 100 bps = 1¢
        # Both round to nearest cent, but legacy display may still produce
        # raw_edge==1. Construct a case where sub-cent rounding in the
        # bank-even direction produces raw_edge==0 but bps==100:
        # A: 4.50¢ → legacy rounds to 4¢ (banker's), bps=450.
        # B: 95.00¢ → legacy 95¢, bps=9500.
        # edge_bps = 10000 - 450 - 9500 = 50 bps. raw_edge via
        # bps_to_cents_round(50) = 0 (banker's round-half-even: 0.5 → 0).
        # So this is a 50-bps edge that rounds to zero cents.
        _install_subcent_book(
            manager,
            "SUB-A",
            price=4,
            price_bps=450,
            quantity=100,
            quantity_fp100=10_000,
        )
        _install_subcent_book(
            manager,
            "SUB-B",
            price=95,
            price_bps=9500,
            quantity=100,
            quantity_fp100=10_000,
        )
        scanner.scan("SUB-A")

        opps = scanner.opportunities
        assert len(opps) == 1, (
            "scanner must admit when raw_edge_bps > 0 even if cents view rounds to 0"
        )
        opp = opps[0]
        assert opp.raw_edge_bps == 50
        # Cents rounded view of 50 bps is 0 (half-even).
        assert opp.raw_edge == 0


class TestSortOrderUsesBps:
    """When two sub-cent opportunities tie on cents but differ on bps,
    the exact-bps value determines ordering."""

    def test_sort_by_bps_when_cents_equal(self) -> None:
        manager = OrderBookManager()
        scanner = ArbitrageScanner(manager)
        scanner.add_pair("E1", "A1", "B1")
        scanner.add_pair("E2", "A2", "B2")

        # E1: edge_bps=210 (2.10¢, rounds to 2¢)
        # A1: 4¢, 380 bps; B1: 94¢, 9410 bps → 10000-380-9410=210
        _install_subcent_book(
            manager, "A1", price=4, price_bps=380, quantity=100, quantity_fp100=10_000
        )
        _install_subcent_book(
            manager, "B1", price=94, price_bps=9410, quantity=100, quantity_fp100=10_000
        )
        # E2: edge_bps=190 (1.90¢, rounds to 2¢)
        # A2: 4¢, 390 bps; B2: 94¢, 9420 bps → 10000-390-9420=190
        _install_subcent_book(
            manager, "A2", price=4, price_bps=390, quantity=100, quantity_fp100=10_000
        )
        _install_subcent_book(
            manager, "B2", price=94, price_bps=9420, quantity=100, quantity_fp100=10_000
        )

        scanner.scan("A1")
        scanner.scan("A2")
        opps = scanner.opportunities
        assert len(opps) == 2
        # Both round to raw_edge==2, but bps edges differ.
        assert opps[0].raw_edge == 2
        assert opps[1].raw_edge == 2
        assert opps[0].raw_edge_bps == 210
        assert opps[1].raw_edge_bps == 190
