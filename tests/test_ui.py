"""Tests for Talos TUI dashboard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MethodType, SimpleNamespace
from typing import cast

from rich.text import Text as RichText
from textual.binding import Binding

from talos.auto_accept_log import AutoAcceptLogger
from talos.engine import TradingEngine
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
        async with app.run_test(size=(200, 50)):
            assert app.query_one("#opportunities-table") is not None

    async def test_app_has_header_and_footer(self) -> None:
        app = TalosApp()
        async with app.run_test(size=(200, 50)):
            from textual.widgets import Footer, Header

            assert len(app.query(Header)) == 1
            assert len(app.query(Footer)) == 1

    async def test_app_has_bottom_panels(self) -> None:
        app = TalosApp()
        async with app.run_test(size=(200, 50)):
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


class _FakeProposalQueue:
    def pending(self) -> list[object]:
        return []


class _FakeTracker:
    def __init__(self) -> None:
        self.on_change = None


class _FakeEngine:
    def __init__(self, *, ws_connected: bool = True, seconds_since_update: float = 0.0) -> None:
        self.scanner = ArbitrageScanner(OrderBookManager())
        self.tracker = _FakeTracker()
        self.game_status_resolver = None
        self.on_notification = None
        self.ws_connected = ws_connected
        self._seconds_since_update = seconds_since_update
        self.proposal_queue = _FakeProposalQueue()
        self.automation_config = SimpleNamespace(
            edge_threshold_cents=1.0,
            stability_seconds=5.0,
        )
        self.unit_size = 5
        self.balance = 0
        self.portfolio_value = 0
        self.position_summaries: list[EventPositionSummary] = []
        self.order_data: list[dict[str, object]] = []

    def performance_settlement_rows(self) -> list[dict[str, object]]:
        return []

    def seconds_since_last_book_update(self) -> float:
        return self._seconds_since_update

    async def start_feed(self) -> None:
        return

    async def refresh_balance(self) -> None:
        return

    async def refresh_account(self) -> None:
        return

    async def refresh_queue_positions(self) -> None:
        return

    async def refresh_trades(self) -> None:
        return

    def recompute_positions(self) -> None:
        return

    async def get_all_settlements(self) -> list[object]:
        return []

    @property
    def has_settlement_cache(self) -> bool:
        return False

    async def refresh_volumes(self) -> None:
        return

    async def refresh_game_status(self) -> None:
        return

    def on_top_of_market_change(self, ticker: str) -> None:
        return


def _disable_startup_workers(app: TalosApp) -> None:
    app._start_feed = MethodType(lambda self: None, app)
    app._start_watchdog = MethodType(lambda self: None, app)
    app._poll_balance = MethodType(lambda self: None, app)
    app._refresh_volumes = MethodType(lambda self: None, app)


class TestOpportunitiesTable:
    async def test_table_shows_opportunity_row(self) -> None:
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test(size=(200, 50)) as pilot:
            table = app.query_one(OpportunitiesTable)
            app.refresh_opportunities()
            await pilot.pause()
            assert table.row_count == 2  # two rows per event (team A + team B)

    async def test_table_formats_prices_as_cents(self) -> None:
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test(size=(200, 50)) as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            # Row 0 = team A, Row 1 = team B (two-row layout)
            row_a = table.get_row_at(0)
            row_b = table.get_row_at(1)
            # Col 7 = Price per leg (after ID column)
            assert "38¢" in str(row_a[7])
            assert "55¢" in str(row_b[7])
            # Col 13 = Edge (on row A only)
            assert "6.2" in str(row_a[13])

    async def test_short_event_label_displayed(self) -> None:
        """Table should show team names from leg_labels."""
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test(size=(200, 50)) as pilot:
            table = app.query_one(OpportunitiesTable)
            table.update_leg_labels({"EVT-STANMIA": ("Stanford", "Miami")})
            app.refresh_opportunities()
            await pilot.pause()
            row_a = table.get_row_at(0)
            row_b = table.get_row_at(1)
            assert str(row_a[2]) == "Stanford"  # col 2 = Team name (after ID)
            assert str(row_b[2]) == "Miami"

    async def test_missing_label_falls_back_to_ticker(self) -> None:
        """Without leg_labels, market tickers display as fallback."""
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test(size=(200, 50)) as pilot:
            table = app.query_one(OpportunitiesTable)
            # No leg_labels set
            app.refresh_opportunities()
            await pilot.pause()
            row_a = table.get_row_at(0)
            assert str(row_a[2]) == "GAME-STAN"  # falls back to ticker_a

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
        async with app.run_test(size=(200, 50)) as pilot:
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
            assert "-" in str(row_a[13])  # Edge col 13 shows negative


class TestPortfolioPanel:
    async def test_renders_balance(self) -> None:
        app = TalosApp()
        async with app.run_test(size=(200, 50)) as pilot:
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
        async with app.run_test(size=(200, 50)) as pilot:
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
            # Col 9 = Pos, Col 10 = Queue, Col 11 = CPM, Col 12 = ETA (shifted +1 for ID col)
            assert "3/5 31.4" in str(row_a[9])  # Pos-A with fee-adjusted avg
            assert "3/5 67.4" in str(row_b[9])  # Pos-B with fee-adjusted avg
            assert "8" in str(row_a[10])  # Queue-A
            assert "15" in str(row_b[10])  # Queue-B
            assert "12.5" in str(row_a[11])  # CPM-A
            assert "6.00*" in str(row_b[11])  # CPM-B (partial)
            assert "1m" in str(row_a[12])  # ETA-A
            assert "2m*" in str(row_b[12])  # ETA-B (partial)
            # Col 16 = Locked profit
            assert "0.02" in str(row_a[16])  # Locked (2.38 cents ≈ $0.02)

    async def test_table_shows_odds_without_positions(self) -> None:
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test(size=(200, 50)) as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            row_a = table.get_row_at(0)
            row_b = table.get_row_at(1)
            assert str(row_a[9]) == "—"  # Pos col shows dim dash (no positions)
            assert str(row_b[9]) == "—"


class TestRichTextCells:
    async def test_empty_cells_are_dim_rich_text(self) -> None:
        """Em-dash placeholders should be dim Rich Text, not plain strings."""
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test(size=(200, 50)) as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            row_a = table.get_row_at(0)
            # Pos (col 8) should be a dim Rich Text em-dash (no positions loaded)
            pos = row_a[9]
            assert isinstance(pos, RichText)
            assert str(pos) == "—"

    async def test_numeric_cells_are_right_aligned(self) -> None:
        """Numeric columns should be right-justified Rich Text."""
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test(size=(200, 50)) as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            row_a = table.get_row_at(0)
            # Price (col 6) should be right-aligned
            no_a = row_a[7]
            assert isinstance(no_a, RichText)
            assert no_a.justify == "right"
            assert "38¢" in str(no_a)

    async def test_edge_positive_is_green(self) -> None:
        """Positive edge should be green Rich Text."""
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test(size=(200, 50)) as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            row_a = table.get_row_at(0)
            edge = row_a[13]  # Edge column (col 13)
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
            no_price_bps=3800,
            remaining_count_fp100=500,
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
        async with app.run_test(size=(200, 50)) as pilot:
            table = app.query_one(OpportunitiesTable)
            table.refresh_from_scanner(scanner, tracker)
            await pilot.pause()
            row_a = table.get_row_at(0)
            q_a = row_a[10]  # Queue column (col 10) on row A
            assert isinstance(q_a, RichText)
            assert "!!" in str(q_a)

    async def test_imbalanced_fills_highlighted_yellow(self) -> None:
        """When fills are imbalanced, the behind side should be yellow."""
        from talos.ui.theme import YELLOW

        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test(size=(200, 50)) as pilot:
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
            pos_a = row_a[9]  # Pos column (col 9) on row A
            pos_b = row_b[9]  # Pos column (col 9) on row B
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
        async with app.run_test(size=(200, 50)) as pilot:
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
        async with app.run_test(size=(200, 50)) as pilot:
            log = app.query_one(OrderLog)
            log.update_orders([])
            await pilot.pause()
            content = str(log.content)
            assert "No orders" in content


class TestAddGamesModal:
    async def test_modal_opens_on_a_key(self) -> None:
        from talos.ui.screens import AddGamesScreen

        app = TalosApp()
        async with app.run_test(size=(200, 50)) as pilot:
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, AddGamesScreen)

    async def test_modal_closes_on_escape(self) -> None:
        from talos.ui.screens import AddGamesScreen

        app = TalosApp()
        async with app.run_test(size=(200, 50)) as pilot:
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
        async with app.run_test(size=(200, 50)) as pilot:
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
        async with app.run_test(size=(200, 50)) as pilot:
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
        async with app.run_test(size=(200, 50)):
            panels = app.query(ProposalPanel)
            assert len(panels) == 0


class TestExecutionModeGovernance:
    async def test_status_bar_manual_and_stale_without_engine(self) -> None:
        app = TalosApp()
        app._execution_mode.enter_manual()
        async with app.run_test(size=(200, 50)) as pilot:
            app._refresh_proposals()
            await pilot.pause()
            assert app.sub_title == "SPORTS | MODE: MANUAL | DATA: STALE"

    async def test_status_bar_auto_live_and_count(self) -> None:
        engine = _FakeEngine(ws_connected=True, seconds_since_update=5.0)
        app = TalosApp(
            engine=cast(TradingEngine, engine),
            startup_execution_mode="manual",
        )
        _disable_startup_workers(app)
        async with app.run_test(size=(200, 50)) as pilot:
            app._execution_mode.enter_automatic()
            app._execution_mode.accepted_count = 12
            app._refresh_proposals()
            await pilot.pause()
            assert app.sub_title == "SPORTS | MODE: AUTO | DATA: LIVE | 12 accepted"

    async def test_startup_manual_mode(self) -> None:
        engine = _FakeEngine()
        app = TalosApp(
            engine=cast(TradingEngine, engine),
            startup_execution_mode="manual",
        )
        _disable_startup_workers(app)
        async with app.run_test(size=(200, 50)) as pilot:
            app._refresh_proposals()
            await pilot.pause()
            assert app._execution_mode.is_automatic is False
            assert app.sub_title == "SPORTS | MODE: MANUAL | DATA: LIVE"

    async def test_startup_timed_automatic_mode(self) -> None:
        engine = _FakeEngine()
        app = TalosApp(
            engine=cast(TradingEngine, engine),
            startup_execution_mode="automatic",
            startup_auto_stop_hours=2.0,
        )
        _disable_startup_workers(app)

        called: list[float | None] = []

        def _fake_enter_automatic_mode(self: TalosApp, hours: float | None = None) -> None:
            called.append(hours)
            self._execution_mode.enter_automatic(hours=hours)

        app._enter_automatic_mode = MethodType(_fake_enter_automatic_mode, app)

        async with app.run_test(size=(200, 50)) as pilot:
            app._refresh_proposals()
            await pilot.pause()
            assert called == [2.0]
            assert app._execution_mode.is_automatic is True
            assert app._execution_mode.auto_stop_at is not None

    def test_toggle_auto_accept_manual_opens_modal(self) -> None:
        app = TalosApp(
            engine=cast(TradingEngine, _FakeEngine()),
            startup_execution_mode="manual",
        )
        called: list[bool] = []
        app._open_auto_accept = MethodType(lambda self: called.append(True), app)
        app._execution_mode.enter_manual()

        app.action_toggle_auto_accept()

        assert called == [True]

    def test_toggle_auto_accept_automatic_ends_session(self) -> None:
        app = TalosApp(engine=cast(TradingEngine, _FakeEngine()))
        called: list[bool] = []
        app._end_automatic_session = MethodType(lambda self: called.append(True), app)
        app._execution_mode.enter_automatic()

        app.action_toggle_auto_accept()

        assert called == [True]

    def test_end_automatic_session_switches_to_manual_and_clears_logger(self) -> None:
        from talos.auto_accept import ExecutionMode

        app = TalosApp(engine=cast(TradingEngine, _FakeEngine()))
        app._execution_mode.enter_automatic()
        app._execution_mode.accepted_count = 3
        app._execution_mode.started_at = datetime.now(UTC) - timedelta(minutes=2)

        session_ended: list[tuple[ExecutionMode, dict[str, object]]] = []

        class _Logger:
            def log_session_end(
                self,
                state: ExecutionMode,
                final_positions: dict[str, object],
            ) -> None:
                session_ended.append((state, final_positions))

        app._auto_accept_logger = cast(AutoAcceptLogger, _Logger())

        app._end_automatic_session()

        assert len(session_ended) == 1
        assert app._execution_mode.is_automatic is False
        assert app._auto_accept_logger is None

    async def test_auto_accept_modal_rejects_zero_duration(self) -> None:
        from textual.widgets import Button, Input, Label

        from talos.ui.screens import AutoAcceptScreen

        app = TalosApp()
        async with app.run_test(size=(200, 50)) as pilot:
            screen = AutoAcceptScreen()
            app.push_screen(screen)
            await pilot.pause()
            screen.query_one("#hours-input", Input).value = "0"
            start = screen.query_one("#start-btn", Button)
            screen.on_button_pressed(Button.Pressed(start))
            await pilot.pause()
            error = screen.query_one("#modal-error", Label)
            assert "greater than 0" in str(error.render())


class TestHorizontalScrollUX:
    """Coverage for the fixed-column + horizontal-scroll-binding patch.

    Regression targets:
      - Narrow-viewport users need horizontal-scroll keybindings because
        cursor_type='row' disables arrow-key horizontal navigation.
      - The leading identifier columns must stay pinned while scrolling so
        the row remains identifiable.
      - The render override must preserve Textual's fixed-band width
        contract (sum of the first ``fixed_columns`` column render widths).
    """

    @staticmethod
    def _binding_map() -> dict[str, Binding]:
        """Index OpportunitiesTable.BINDINGS by key, filtering out any
        tuple-form entries so pyright can narrow to Binding."""
        result: dict[str, Binding] = {}
        for b in OpportunitiesTable.BINDINGS:
            if isinstance(b, Binding):
                result[b.key] = b
        return result

    def test_bindings_wire_brackets_to_page_scroll(self) -> None:
        """`[` / `]` must invoke page_left / page_right (not scroll_left /
        scroll_right) so keyboard navigation can jump a viewport at a time
        on narrow terminals."""
        bindings = self._binding_map()
        assert bindings["["].action == "page_left"
        assert bindings["]"].action == "page_right"

    def test_bindings_wire_shift_arrows_to_single_cell_scroll(self) -> None:
        """shift+arrow must invoke fine-grained single-cell scroll."""
        bindings = self._binding_map()
        assert bindings["shift+left"].action == "scroll_left"
        assert bindings["shift+right"].action == "scroll_right"

    def test_scroll_bindings_are_hidden_from_footer(self) -> None:
        """Footer is already crowded — these bindings must not add to it."""
        bindings = self._binding_map()
        for key in ("[", "]", "shift+left", "shift+right"):
            assert bindings[key].show is False, f"{key} must be show=False"

    async def test_full_mode_pins_three_leading_columns(self) -> None:
        """Full mode pins id + dot + team (indices 0-2)."""
        app = TalosApp()
        async with app.run_test(size=(200, 50)):
            table = app.query_one(OpportunitiesTable)
            assert table._compact is False
            assert table.fixed_columns == OpportunitiesTable._FIXED_COLS_FULL
            assert table.fixed_columns == 3

    async def test_compact_mode_pins_one_leading_column(self) -> None:
        """Compact mode drops id + dot so only team should be pinned."""
        app = TalosApp()
        async with app.run_test(size=(200, 50)) as pilot:
            table = app.query_one(OpportunitiesTable)
            table.set_compact(True)
            await pilot.pause()
            assert table._compact is True
            assert table.fixed_columns == OpportunitiesTable._FIXED_COLS_COMPACT
            assert table.fixed_columns == 1

    async def test_compact_toggle_updates_fixed_columns(self) -> None:
        """Toggling compact twice returns fixed_columns to the full value."""
        app = TalosApp()
        async with app.run_test(size=(200, 50)) as pilot:
            table = app.query_one(OpportunitiesTable)
            assert table.fixed_columns == 3
            table.set_compact(True)
            await pilot.pause()
            assert table.fixed_columns == 1
            table.set_compact(False)
            await pilot.pause()
            assert table.fixed_columns == 3

    async def test_fixed_band_width_matches_textual_contract(self) -> None:
        """Render override must not widen the pinned band — Textual crops
        the scrollable region using the sum of the first fixed_columns
        column render widths, so adding cells to `fixed` desyncs the crop.
        """
        from textual.coordinate import Coordinate

        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test(size=(120, 30)) as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)

            expected = sum(
                c.get_render_width(table)
                for c in table.ordered_columns[: table.fixed_columns]
            )
            first_row_key = next(iter(table._data.keys()))
            fixed, _scrollable = table._render_line_in_row(
                first_row_key,
                0,
                table.rich_style,
                Coordinate(-1, -1),
                Coordinate(-1, -1),
            )
            actual = sum(sum(len(seg.text) for seg in cell) for cell in fixed)
            assert actual == expected, (
                f"Fixed-band width {actual} does not match Textual contract {expected}. "
                "Adding separator cells to `fixed` desyncs the scroll crop."
            )

    async def test_scrollable_band_crop_boundary_aligns_with_fixed_width(
        self,
    ) -> None:
        """Scroll-geometry invariant: Textual crops the scrollable band at
        ``fixed_width`` (the summed render widths of the first
        ``fixed_columns`` columns). If our separator insertion widens the
        scrollable prefix beyond that, the first visible content after the
        pinned band is not the first non-fixed cell but leftover padding
        from the pinned-column duplicate, and horizontal scroll consumes
        padding instead of advancing through real content.
        """
        from textual.coordinate import Coordinate

        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test(size=(80, 30)) as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            fc = table.fixed_columns
            fixed_width = sum(
                c.get_render_width(table) for c in table.ordered_columns[:fc]
            )
            first_row_key = next(iter(table._data.keys()))
            _fixed, scrollable = table._render_line_in_row(
                first_row_key,
                0,
                table.rich_style,
                Coordinate(-1, -1),
                Coordinate(-1, -1),
            )
            prefix_width = sum(
                sum(len(seg.text) for seg in cell) for cell in scrollable[:fc]
            )
            assert prefix_width == fixed_width, (
                f"scrollable prefix width {prefix_width} must equal fixed_width "
                f"{fixed_width}. If larger, the scroll crop lands inside the "
                "first non-fixed column and early scroll steps consume "
                "duplicated padding instead of column content."
            )

    async def test_first_non_fixed_cell_is_at_crop_boundary(self) -> None:
        """Stronger form of the crop-alignment invariant: the cell at index
        ``fixed_columns`` in scrollable_out must be an actual column cell
        (non-empty, not a separator) and its first character sits at
        position ``fixed_width`` in the concatenated scrollable stream.
        """
        from textual.coordinate import Coordinate

        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test(size=(80, 30)) as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            fc = table.fixed_columns
            fixed_width = sum(
                c.get_render_width(table) for c in table.ordered_columns[:fc]
            )
            expected_first_col_width = table.ordered_columns[fc].get_render_width(table)

            first_row_key = next(iter(table._data.keys()))
            _fixed, scrollable = table._render_line_in_row(
                first_row_key,
                0,
                table.rich_style,
                Coordinate(-1, -1),
                Coordinate(-1, -1),
            )
            first_scrollable_cell_width = sum(
                len(seg.text) for seg in scrollable[fc]
            )
            # Cell at index fc must be the first non-fixed column (no
            # separator cell has been inserted before it).
            assert first_scrollable_cell_width == expected_first_col_width, (
                f"scrollable[{fc}] width is {first_scrollable_cell_width}, "
                f"expected {expected_first_col_width} (the first non-fixed "
                "column's render width). If this is 1, a separator cell was "
                "inserted at the seam and the crop boundary is misaligned."
            )
            # The cell just past the first non-fixed column should be a
            # separator (1-char `│`), confirming we only insert separators
            # AFTER the crop boundary, not before.
            if fc + 1 < len(scrollable) and fc + 2 < len(scrollable):
                sep_width = sum(len(seg.text) for seg in scrollable[fc + 1])
                assert sep_width == 1, (
                    f"expected separator (width 1) at scrollable[{fc + 1}], "
                    f"got width {sep_width}"
                )
            # And prefix text content before the crop boundary must be
            # exactly fixed_width characters.
            prefix_text = "".join(
                seg.text
                for cell in scrollable[:fc]
                for seg in cell
            )
            assert len(prefix_text) == fixed_width

    async def test_shift_right_advances_scroll_x(self) -> None:
        """End-to-end wiring: pressing shift+right must invoke the
        scroll_right action, which increments the widget's scroll_x state.
        This catches regressions where the binding is unreachable (focus
        routing) or wired to the wrong action.
        """
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test(size=(80, 30)) as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            table.focus()
            await pilot.pause()

            # Row count and structural invariants preserved across scroll.
            row_count_before = table.row_count
            scroll_x_start = table.scroll_x

            await pilot.press("shift+right")
            await pilot.pause()
            scroll_x_after_one = table.scroll_x

            await pilot.press("shift+right")
            await pilot.pause()
            scroll_x_after_two = table.scroll_x

            assert table.row_count == row_count_before
            assert scroll_x_after_one > scroll_x_start, (
                "shift+right must advance scroll_x (binding not reaching "
                "scroll_right action)"
            )
            assert scroll_x_after_two > scroll_x_after_one, (
                "second shift+right must advance scroll_x further"
            )

            # shift+left should reverse it.
            await pilot.press("shift+left")
            await pilot.pause()
            assert table.scroll_x < scroll_x_after_two
