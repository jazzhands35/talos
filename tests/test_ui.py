"""Tests for Talos TUI dashboard."""

from __future__ import annotations

from talos.models.position import EventPositionSummary, LegSummary
from talos.models.strategy import Opportunity
from talos.models.ws import OrderBookSnapshot
from talos.orderbook import OrderBookManager
from talos.scanner import ArbitrageScanner
from talos.ui.app import TalosApp
from talos.ui.widgets import AccountPanel, OpportunitiesTable, OrderLog


class TestAppMount:
    async def test_app_mounts_without_error(self) -> None:
        app = TalosApp()
        async with app.run_test():
            assert app.query_one("#opportunities-table") is not None

    async def test_app_has_header_and_footer(self) -> None:
        app = TalosApp()
        async with app.run_test():
            from textual.widgets import Footer, Header

            assert len(app.query(Header)) == 1
            assert len(app.query(Footer)) == 1

    async def test_app_has_bottom_panels(self) -> None:
        app = TalosApp()
        async with app.run_test():
            assert app.query_one("#account-panel") is not None
            assert app.query_one("#order-log") is not None


def _make_scanner_with_opportunity() -> ArbitrageScanner:
    """Create a scanner with one detected opportunity (maker NO prices)."""
    mgr = OrderBookManager()
    scanner = ArbitrageScanner(mgr)
    scanner.add_pair("EVT-STANMIA", "GAME-STAN", "GAME-MIA")
    # NO-A=38, NO-B=55 → cost=93, edge=7
    mgr.apply_snapshot(
        "GAME-STAN",
        OrderBookSnapshot(market_ticker="GAME-STAN", market_id="u1", yes=[], no=[[38, 100]]),
    )
    mgr.apply_snapshot(
        "GAME-MIA",
        OrderBookSnapshot(market_ticker="GAME-MIA", market_id="u2", yes=[], no=[[55, 200]]),
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
            # NO-A=38¢, NO-B=55¢, Cost=93¢, Edge=7¢, Depth=100, $/pair=$0.07
            assert "38¢" in str(row_data[1])
            assert "55¢" in str(row_data[2])
            assert "7¢" in str(row_data[4])

    async def test_table_shows_negative_edge_pairs(self) -> None:
        mgr = OrderBookManager()
        scanner = ArbitrageScanner(mgr)
        scanner.add_pair("EVT-1", "GAME-A", "GAME-B")
        # NO-A=38, NO-B=55 → edge=7
        mgr.apply_snapshot(
            "GAME-A",
            OrderBookSnapshot(market_ticker="GAME-A", market_id="u1", yes=[], no=[[38, 100]]),
        )
        mgr.apply_snapshot(
            "GAME-B",
            OrderBookSnapshot(market_ticker="GAME-B", market_id="u2", yes=[], no=[[55, 100]]),
        )
        scanner.scan("GAME-A")

        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            assert table.row_count == 1

            # Edge goes negative — NO-A=60, NO-B=55 → edge=-15
            mgr.apply_snapshot(
                "GAME-A",
                OrderBookSnapshot(market_ticker="GAME-A", market_id="u1", yes=[], no=[[60, 100]]),
            )
            scanner.scan("GAME-A")
            app.refresh_opportunities()
            await pilot.pause()
            assert table.row_count == 1  # still visible
            row = table.get_row_at(0)
            assert "-" in str(row[4])  # edge column shows negative


class TestAccountPanel:
    async def test_renders_balance(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            panel = app.query_one(AccountPanel)
            panel.update_balance(balance_cents=125000, portfolio_cents=210050)
            await pilot.pause()
            content = str(panel.content)
            assert "$1,250.00" in content
            assert "$2,100.50" in content

    async def test_renders_event_positions(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            panel = app.query_one(AccountPanel)
            panel.update_event_positions(
                [
                    EventPositionSummary(
                        event_ticker="EVT-STANMIA",
                        leg_a=LegSummary(
                            ticker="GAME-STAN", no_price=31, filled_count=3, resting_count=2
                        ),
                        leg_b=LegSummary(
                            ticker="GAME-MIA", no_price=67, filled_count=3, resting_count=2
                        ),
                        matched_pairs=3,
                        locked_profit_cents=6,
                        unmatched_a=0,
                        unmatched_b=0,
                        exposure_cents=0,
                    )
                ]
            )
            await pilot.pause()
            content = str(panel.content)
            assert "EVT-STANMIA" in content
            assert "GAME-STAN" in content
            assert "3/5 filled" in content
            assert "Matched: 3 pairs" in content
            assert "$0.06" in content

    async def test_empty_event_positions(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            panel = app.query_one(AccountPanel)
            panel.update_event_positions([])
            await pilot.pause()
            content = str(panel.content)
            assert "ACCOUNT" in content


class TestOrderLog:
    async def test_renders_orders(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            log = app.query_one(OrderLog)
            log.update_orders(
                [
                    {
                        "ticker": "GAME-STAN",
                        "side": "no",
                        "price": 38,
                        "filled": 3,
                        "total": 5,
                        "remaining": 2,
                        "status": "resting",
                        "time": "12:33",
                        "queue_pos": None,
                    },
                    {
                        "ticker": "GAME-MIA",
                        "side": "no",
                        "price": 55,
                        "filled": 5,
                        "total": 5,
                        "remaining": 0,
                        "status": "executed",
                        "time": "12:33",
                        "queue_pos": 4,
                    },
                ]
            )
            await pilot.pause()
            content = str(log.content)
            assert "GAME-STAN" in content
            assert "GAME-MIA" in content
            assert "3/5" in content
            assert "2 resting" in content
            assert "#4" in content

    async def test_empty_orders(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            log = app.query_one(OrderLog)
            log.update_orders([])
            await pilot.pause()
            content = str(log.content)
            assert "No orders" in content


class TestAddGamesModal:
    async def test_modal_opens_on_a_key(self) -> None:
        from talos.ui.screens import AddGamesScreen

        app = TalosApp()
        async with app.run_test() as pilot:
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, AddGamesScreen)

    async def test_modal_closes_on_escape(self) -> None:
        from talos.ui.screens import AddGamesScreen

        app = TalosApp()
        async with app.run_test() as pilot:
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, AddGamesScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, AddGamesScreen)


class TestBidModal:
    async def test_bid_modal_shows_opportunity_data(self) -> None:
        from talos.ui.screens import BidScreen

        opp = Opportunity(
            event_ticker="EVT-STANMIA",
            ticker_a="GAME-STAN",
            ticker_b="GAME-MIA",
            no_a=38,
            no_b=55,
            qty_a=100,
            qty_b=200,
            raw_edge=7,
            tradeable_qty=100,
            timestamp="2026-03-04T12:00:00Z",
        )
        app = TalosApp()
        async with app.run_test() as pilot:
            app.push_screen(BidScreen(opp))
            await pilot.pause()
            assert isinstance(app.screen, BidScreen)

    async def test_bid_modal_cancel(self) -> None:
        from talos.ui.screens import BidScreen

        opp = Opportunity(
            event_ticker="EVT-STANMIA",
            ticker_a="GAME-STAN",
            ticker_b="GAME-MIA",
            no_a=38,
            no_b=55,
            qty_a=100,
            qty_b=200,
            raw_edge=7,
            tradeable_qty=100,
            timestamp="2026-03-04T12:00:00Z",
        )
        app = TalosApp()
        async with app.run_test() as pilot:
            app.push_screen(BidScreen(opp))
            await pilot.pause()
            assert isinstance(app.screen, BidScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, BidScreen)
