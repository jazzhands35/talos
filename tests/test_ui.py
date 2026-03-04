"""Tests for Talos TUI dashboard."""

from __future__ import annotations

import pytest

from talos.models.strategy import Opportunity
from talos.models.ws import OrderBookSnapshot
from talos.orderbook import OrderBookManager
from talos.scanner import ArbitrageScanner
from talos.ui.app import TalosApp
from talos.ui.widgets import OpportunitiesTable


class TestAppMount:
    async def test_app_mounts_without_error(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            assert app.query_one("#opportunities-table") is not None

    async def test_app_has_header_and_footer(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            from textual.widgets import Footer, Header

            assert len(app.query(Header)) == 1
            assert len(app.query(Footer)) == 1

    async def test_app_has_bottom_panels(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            assert app.query_one("#account-panel") is not None
            assert app.query_one("#order-log") is not None


def _make_scanner_with_opportunity() -> ArbitrageScanner:
    """Create a scanner with one detected opportunity."""
    mgr = OrderBookManager()
    scanner = ArbitrageScanner(mgr)
    scanner.add_pair("EVT-STANMIA", "GAME-STAN", "GAME-MIA")
    mgr.apply_snapshot(
        "GAME-STAN",
        OrderBookSnapshot(market_ticker="GAME-STAN", market_id="u1", yes=[[62, 100]], no=[]),
    )
    mgr.apply_snapshot(
        "GAME-MIA",
        OrderBookSnapshot(market_ticker="GAME-MIA", market_id="u2", yes=[[45, 200]], no=[]),
    )
    scanner.scan("GAME-STAN")
    return scanner


class TestOpportunitiesTable:
    async def test_table_shows_opportunity_row(self) -> None:
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            table = app.query_one(OpportunitiesTable)
            app.refresh_opportunities()
            await pilot.pause()
            assert table.row_count == 1

    async def test_table_formats_prices_as_cents(self) -> None:
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            # Row 0 should have formatted price data
            row_data = table.get_row_at(0)
            # NO-A=38¢, NO-B=55¢, Cost=93¢, Edge=7¢, Qty=100, Profit=$7.00
            assert "38¢" in str(row_data[1])
            assert "55¢" in str(row_data[2])
            assert "7¢" in str(row_data[4])

    async def test_table_clears_vanished_opportunities(self) -> None:
        mgr = OrderBookManager()
        scanner = ArbitrageScanner(mgr)
        scanner.add_pair("EVT-1", "GAME-A", "GAME-B")
        mgr.apply_snapshot(
            "GAME-A",
            OrderBookSnapshot(market_ticker="GAME-A", market_id="u1", yes=[[62, 100]], no=[]),
        )
        mgr.apply_snapshot(
            "GAME-B",
            OrderBookSnapshot(market_ticker="GAME-B", market_id="u2", yes=[[45, 100]], no=[]),
        )
        scanner.scan("GAME-A")

        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            assert table.row_count == 1

            # Remove the opportunity by changing price
            mgr.apply_snapshot(
                "GAME-A",
                OrderBookSnapshot(market_ticker="GAME-A", market_id="u1", yes=[[40, 100]], no=[]),
            )
            scanner.scan("GAME-A")
            app.refresh_opportunities()
            await pilot.pause()
            assert table.row_count == 0
