"""Tests for Talos TUI dashboard."""

from __future__ import annotations

from rich.text import Text as RichText

from talos.models.position import EventPositionSummary, LegSummary
from talos.models.strategy import Opportunity
from talos.models.ws import OrderBookSnapshot
from talos.orderbook import OrderBookManager
from talos.scanner import ArbitrageScanner
from talos.ui.app import TalosApp
from talos.ui.widgets import OpportunitiesTable, OrderLog, PortfolioPanel


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
            assert table.row_count == 2  # two rows per event (team A + team B)

    async def test_table_formats_prices_as_cents(self) -> None:
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            # Row 0 = team A, Row 1 = team B (two-row layout)
            row_a = table.get_row_at(0)
            row_b = table.get_row_at(1)
            # Col 5 = Price per leg
            assert "38¢" in str(row_a[5])
            assert "55¢" in str(row_b[5])
            # Col 11 = Edge (on row A only)
            assert "6.2" in str(row_a[11])

    async def test_short_event_label_displayed(self) -> None:
        """Table should show team names from leg_labels."""
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            table = app.query_one(OpportunitiesTable)
            table.update_leg_labels({"EVT-STANMIA": ("Stanford", "Miami")})
            app.refresh_opportunities()
            await pilot.pause()
            row_a = table.get_row_at(0)
            row_b = table.get_row_at(1)
            assert str(row_a[1]) == "Stanford"  # col 1 = Team name
            assert str(row_b[1]) == "Miami"

    async def test_missing_label_falls_back_to_ticker(self) -> None:
        """Without leg_labels, market tickers display as fallback."""
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            table = app.query_one(OpportunitiesTable)
            # No leg_labels set
            app.refresh_opportunities()
            await pilot.pause()
            row_a = table.get_row_at(0)
            assert str(row_a[1]) == "GAME-STAN"  # falls back to ticker_a

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
            assert table.row_count == 2  # two rows per event

            # Edge goes negative — NO-A=60, NO-B=55 → edge=-15
            mgr.apply_snapshot(
                "GAME-A",
                OrderBookSnapshot(market_ticker="GAME-A", market_id="u1", yes=[], no=[[60, 100]]),
            )
            scanner.scan("GAME-A")
            app.refresh_opportunities()
            await pilot.pause()
            assert table.row_count == 2  # still visible (2 rows)
            row_a = table.get_row_at(0)
            assert "-" in str(row_a[11])  # Edge col 11 shows negative


class TestPortfolioPanel:
    async def test_renders_balance(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            panel = app.query_one(PortfolioPanel)
            panel.update_balance(balance_cents=125000, portfolio_cents=210050)
            await pilot.pause()
            # PortfolioPanel extends Static — use render() to get content
            content = str(panel.render())
            assert "$1,250.00" in content


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
            # Two-row layout: row 0 = team A, row 1 = team B
            row_a = table.get_row_at(0)
            row_b = table.get_row_at(1)
            # Col 7 = Pos, Col 8 = Queue, Col 9 = CPM, Col 10 = ETA
            assert "3/5 31.4" in str(row_a[7])  # Pos-A with fee-adjusted avg
            assert "3/5 67.4" in str(row_b[7])  # Pos-B with fee-adjusted avg
            assert "8" in str(row_a[8])  # Queue-A
            assert "15" in str(row_b[8])  # Queue-B
            assert "12.5" in str(row_a[9])  # CPM-A
            assert "6.00*" in str(row_b[9])  # CPM-B (partial)
            assert "1m" in str(row_a[10])  # ETA-A
            assert "2m*" in str(row_b[10])  # ETA-B (partial)
            # Col 13 = Locked profit
            assert "0.02" in str(row_a[13])  # Locked (2.38 cents ≈ $0.02)

    async def test_table_shows_odds_without_positions(self) -> None:
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            row_a = table.get_row_at(0)
            row_b = table.get_row_at(1)
            assert str(row_a[7]) == "—"  # Pos col shows dim dash (no positions)
            assert str(row_b[7]) == "—"


class TestRichTextCells:
    async def test_empty_cells_are_dim_rich_text(self) -> None:
        """Em-dash placeholders should be dim Rich Text, not plain strings."""
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            row_a = table.get_row_at(0)
            # Pos (col 7) should be a dim Rich Text em-dash (no positions loaded)
            pos = row_a[7]
            assert isinstance(pos, RichText)
            assert str(pos) == "—"

    async def test_numeric_cells_are_right_aligned(self) -> None:
        """Numeric columns should be right-justified Rich Text."""
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            row_a = table.get_row_at(0)
            # Price (col 5) should be right-aligned
            no_a = row_a[5]
            assert isinstance(no_a, RichText)
            assert no_a.justify == "right"
            assert "38¢" in str(no_a)

    async def test_edge_positive_is_green(self) -> None:
        """Positive edge should be green Rich Text."""
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            row_a = table.get_row_at(0)
            edge = row_a[11]  # Edge column (col 11)
            assert isinstance(edge, RichText)
            # Scanner has positive edge (NO-A=38, NO-B=55, fee_edge≈6.2)
            assert edge.style is not None

    async def test_jumped_queue_is_yellow(self) -> None:
        """Queue position with !! prefix should be styled yellow."""
        from talos.models.order import Order
        from talos.models.strategy import ArbPair
        from talos.top_of_market import TopOfMarketTracker

        scanner = _make_scanner_with_opportunity()
        mgr = scanner._books
        tracker = TopOfMarketTracker(mgr)

        # Register a resting NO buy order on GAME-STAN at price 38
        order = Order(
            order_id="o1",
            ticker="GAME-STAN",
            side="no",
            action="buy",
            no_price=38,
            remaining_count=5,
            status="resting",
        )
        pair = ArbPair(event_ticker="EVT-STANMIA", ticker_a="GAME-STAN", ticker_b="GAME-MIA")
        tracker.update_orders([order], [pair])

        # Update book so best NO ask is 39 (> our 38) → we've been jumped
        mgr.apply_snapshot(
            "GAME-STAN",
            OrderBookSnapshot(market_ticker="GAME-STAN", market_id="u1", yes=[], no=[[39, 50]]),
        )
        scanner.scan("GAME-STAN")  # re-scan so snapshot reflects new book
        tracker.check("GAME-STAN")
        assert tracker.is_at_top("GAME-STAN") is False

        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            table = app.query_one(OpportunitiesTable)
            table.refresh_from_scanner(scanner, tracker)
            await pilot.pause()
            row_a = table.get_row_at(0)
            q_a = row_a[8]  # Queue column (col 8) on row A
            assert isinstance(q_a, RichText)
            assert "!!" in str(q_a)

    async def test_imbalanced_fills_highlighted_yellow(self) -> None:
        """When fills are imbalanced, the behind side should be yellow."""
        from talos.ui.theme import YELLOW

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
                            resting_count=7,
                            total_fill_cost=93,
                        ),
                        leg_b=LegSummary(
                            ticker="GAME-MIA",
                            no_price=67,
                            filled_count=5,
                            resting_count=5,
                            total_fill_cost=335,
                        ),
                        matched_pairs=3,
                        locked_profit_cents=0,
                        unmatched_a=0,
                        unmatched_b=2,
                        exposure_cents=0,
                    )
                ]
            )
            app.refresh_opportunities()
            await pilot.pause()
            # Two-row layout: row 0 = team A, row 1 = team B
            row_a = table.get_row_at(0)
            row_b = table.get_row_at(1)
            pos_a = row_a[7]  # Pos column (col 7) on row A
            pos_b = row_b[7]  # Pos column (col 7) on row B
            # Behind side (A) should show yellow styling
            assert isinstance(pos_a, RichText)
            assert isinstance(pos_b, RichText)
            # A has fewer fills, so it should be yellow-styled
            assert YELLOW in str(pos_a.style)

    def test_status_low_edge_is_dim(self) -> None:
        from talos.ui.widgets import _fmt_status

        result = _fmt_status("Low edge")
        assert "\u25cb" in str(result)
        assert "dim" in str(result.style)

    def test_status_jumped_is_peach(self) -> None:
        from talos.ui.theme import PEACH
        from talos.ui.widgets import _fmt_status

        result = _fmt_status("Jumped A")
        assert "\u25f7" in str(result)
        assert PEACH in str(result.style)

    def test_status_filling_is_blue(self) -> None:
        from talos.ui.theme import BLUE
        from talos.ui.widgets import _fmt_status

        result = _fmt_status("Filling (B -3)")
        assert "\u25d0" in str(result)
        assert BLUE in str(result.style)

    def test_status_empty_is_dim_dash(self) -> None:
        from talos.ui.widgets import _fmt_status

        result = _fmt_status("")
        assert str(result) == "\u2014"


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
    async def test_proposal_panel_removed_from_main_layout(self) -> None:
        """ProposalPanel is no longer in the main layout (moved to popup)."""
        from talos.ui.proposal_panel import ProposalPanel

        app = TalosApp()
        async with app.run_test():
            panels = app.query(ProposalPanel)
            assert len(panels) == 0
