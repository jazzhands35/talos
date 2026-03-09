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
            # NO-A=38¢, NO-B=55¢, Edge=5.9¢ (fee-adjusted)
            assert "38¢" in str(row_data[1])
            assert "55¢" in str(row_data[2])
            assert "5.9" in str(row_data[3])  # fee_edge ≈ 5.9¢

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
            assert "-" in str(row[3])  # fee_edge column shows negative


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


class TestTablePositions:
    async def test_table_shows_position_data(self) -> None:
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            table = app.query_one(OpportunitiesTable)
            table.update_positions(
                [
                    EventPositionSummary(
                        event_ticker="EVT-STANMIA",
                        leg_a=LegSummary(
                            ticker="GAME-STAN",
                            no_price=31,
                            filled_count=3,
                            resting_count=2,
                            total_fill_cost=3 * 31,
                            queue_position=8,
                            cpm=12.5,
                            cpm_partial=False,
                            eta_minutes=0.64,
                        ),
                        leg_b=LegSummary(
                            ticker="GAME-MIA",
                            no_price=67,
                            filled_count=3,
                            resting_count=2,
                            total_fill_cost=3 * 67,
                            queue_position=15,
                            cpm=6.0,
                            cpm_partial=True,
                            eta_minutes=2.5,
                        ),
                        matched_pairs=3,
                        locked_profit_cents=2.38,
                        unmatched_a=0,
                        unmatched_b=0,
                        exposure_cents=0,
                    )
                ]
            )
            app.refresh_opportunities()
            await pilot.pause()
            row = table.get_row_at(0)
            row_str = str(row)
            # Pos-A: 3/5 at fee_adjusted_cost(31) = 31 + 69*0.0175 = 32.2¢
            # Pos-B: 3/5 at fee_adjusted_cost(67) = 67 + 33*0.0175 = 67.6¢
            assert "3/5 32.2" in row_str  # Pos-A with fee-adjusted avg
            assert "3/5 67.6" in row_str  # Pos-B with fee-adjusted avg
            assert "'8'" in row_str  # Q-A column
            assert "'15'" in row_str  # Q-B column
            assert "12.5" in row_str  # CPM-A
            assert "6.00*" in row_str  # CPM-B (partial)
            assert "1m" in row_str  # ETA-A (0.64 min rounds to 1m)
            assert "2m*" in row_str  # ETA-B (2.5 rounds to 2m via banker's rounding, partial)
            assert "0.02" in row_str  # P&L (fee-adjusted profit, no exposure)
            # Net/Odds: both scenarios positive → guaranteed profit
            # 3 fills each at 31¢/67¢ (total costs 93/201):
            # net_a = (300-201)*0.9825 - 93 = 4.27¢, net_b = (300-93)*0.9825 - 201 = 2.38¢
            # GTD shows worst-case (smaller) profit: $0.02
            assert "GTD $0.02" in row_str

    async def test_table_shows_odds_without_positions(self) -> None:
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            row = table.get_row_at(0)
            row_str = str(row)
            assert "—" in row_str  # Position columns show dashes
            # Net/Odds shows per-leg odds only when no positions
            assert "+160/-124" in row_str


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
            fee_edge=5.9,
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
            fee_edge=5.9,
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


class TestProposalPanel:
    async def test_proposal_panel_exists_and_hidden(self) -> None:
        from talos.ui.proposal_panel import ProposalPanel

        app = TalosApp()
        async with app.run_test():
            panel = app.query_one(ProposalPanel)
            assert panel is not None
            panel.refresh_proposals()
            assert panel.display is False
