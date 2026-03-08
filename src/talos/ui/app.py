"""Main Talos TUI application."""

from __future__ import annotations

import structlog
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header

from talos.bid_adjuster import BidAdjuster
from talos.cpm import CPMTracker
from talos.game_manager import GameManager
from talos.market_feed import MarketFeed
from talos.models.adjustment import ProposedAdjustment
from talos.models.order import Order
from talos.models.position import EventPositionSummary
from talos.models.strategy import BidConfirmation
from talos.position import compute_event_positions
from talos.position_ledger import Side
from talos.rest_client import KalshiRESTClient
from talos.scanner import ArbitrageScanner
from talos.top_of_market import TopOfMarketTracker
from talos.ui.screens import AddGamesScreen, BidScreen
from talos.ui.theme import APP_CSS
from talos.ui.widgets import AccountPanel, OpportunitiesTable, OrderLog

logger = structlog.get_logger()


def _merge_queue(existing: int | None, incoming: int) -> int:
    """Conservative queue position merge — keep smallest positive value."""
    if existing is None:
        return incoming
    if incoming <= 0 < existing:
        return existing
    if existing <= 0 < incoming:
        return incoming
    return min(existing, incoming)


class TalosApp(App):
    """Talos arbitrage trading dashboard."""

    CSS = APP_CSS
    TITLE = "TALOS"
    BINDINGS = [
        ("a", "add_games", "Add Games"),
        ("d", "remove_game", "Remove Game"),
        ("x", "clear_games", "Clear All"),
        ("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        scanner: ArbitrageScanner | None = None,
        game_manager: GameManager | None = None,
        rest_client: KalshiRESTClient | None = None,
        market_feed: MarketFeed | None = None,
        tracker: TopOfMarketTracker | None = None,
        adjuster: BidAdjuster | None = None,
        initial_games: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._scanner = scanner
        self._game_manager = game_manager
        self._rest = rest_client
        self._feed = market_feed
        self._tracker = tracker
        self._adjuster = adjuster
        self._initial_games = initial_games or []
        self._queue_cache: dict[str, int] = {}
        self._orders_cache: list[Order] = []
        self._cpm = CPMTracker()

    def compose(self) -> ComposeResult:
        yield Header()
        yield OpportunitiesTable(id="opportunities-table")
        with Horizontal(id="bottom-panels"):
            yield AccountPanel(id="account-panel")
            yield OrderLog(id="order-log")
        yield Footer()

    def on_mount(self) -> None:
        """Start polling timers and WebSocket feed."""
        if self._scanner is not None:
            self.set_interval(0.5, self.refresh_opportunities)
        if self._rest is not None:
            self.set_interval(10.0, self.refresh_account)
            self.set_interval(3.0, self.refresh_queue_positions)
            self.set_interval(10.0, self.refresh_trades)
        if self._tracker is not None:
            self._tracker.on_change = self._on_top_of_market_change
        if self._adjuster is not None:
            self._adjuster.on_proposal = self._on_adjustment_proposed
        if self._feed is not None:
            self._start_feed()

    @work(thread=False)
    async def _start_feed(self) -> None:
        """Connect WebSocket, restore saved games, and listen for market data."""
        if self._feed is None:
            return
        try:
            await self._feed.connect()
            self.notify("WebSocket connected", severity="information")
            # Restore saved games now that WS is ready
            if self._game_manager is not None and self._initial_games:
                restored = 0
                for ticker in self._initial_games:
                    try:
                        await self._game_manager.add_game(ticker)
                        restored += 1
                    except Exception:
                        logger.warning("restore_game_failed", game=ticker)
                if restored:
                    self.notify(f"Restored {restored} game(s)", severity="information")
                self._initial_games.clear()
            await self._feed.start()
        except Exception as e:
            logger.exception("feed_connection_error")
            self.notify(
                f"WebSocket error: {type(e).__name__}: {e}",
                severity="error",
                markup=False,
                timeout=15,
            )

    def _active_market_tickers(self) -> list[str]:
        """Collect market tickers from all active scanner pairs."""
        if self._scanner is None:
            return []
        tickers: list[str] = []
        for pair in self._scanner.pairs:
            tickers.append(pair.ticker_a)
            tickers.append(pair.ticker_b)
        return tickers

    def _on_top_of_market_change(self, ticker: str, at_top: bool) -> None:
        """Handle top-of-market state transition — show toast and evaluate adjustment."""
        if self._tracker is None:
            return
        resting = self._tracker.resting_price(ticker)
        if at_top:
            self.notify(
                f"Back at top: {ticker} ({resting}c)",
                severity="information",
                timeout=10,
            )
        else:
            top_price = self._tracker.book_top_price(ticker) or "?"
            self.notify(
                f"Jumped: {ticker} (you: {resting}c, top: {top_price}c)",
                severity="warning",
                timeout=15,
            )

        # Evaluate for bid adjustment
        if self._adjuster is not None:
            proposal = self._adjuster.evaluate_jump(ticker, at_top)
            if proposal is not None:
                logger.info(
                    "adjustment_proposed",
                    event_ticker=proposal.event_ticker,
                    side=proposal.side,
                    old_price=proposal.cancel_price,
                    new_price=proposal.new_price,
                    reason=proposal.reason,
                )

    def _on_adjustment_proposed(self, proposal: ProposedAdjustment) -> None:
        """Handle a new bid adjustment proposal — show for operator approval."""
        self.notify(
            f"Adjustment proposed: {proposal.event_ticker} side {proposal.side}\n"
            f"{proposal.reason}\n"
            f"Before: {proposal.position_before}\n"
            f"After: {proposal.position_after}\n"
            f"Safety: {proposal.safety_check}",
            severity="warning",
            timeout=30,
        )

    @work(thread=False)
    async def approve_adjustment(
        self, event_ticker: str, side_value: str
    ) -> None:
        """Execute an approved bid adjustment via amend."""
        if self._adjuster is None or self._rest is None:
            return
        side = Side(side_value)
        proposal = self._adjuster.get_proposal(event_ticker, side)
        if proposal is None:
            self.notify("No pending proposal to approve", severity="warning")
            return
        try:
            await self._adjuster.execute(proposal, self._rest)
            self.notify(
                f"Adjustment executed: {event_ticker} side {side_value} "
                f"→ {proposal.new_price}c",
                severity="information",
                timeout=10,
            )
        except Exception as e:
            self.notify(
                f"Adjustment FAILED: {type(e).__name__}: {e}",
                severity="error",
                markup=False,
                timeout=30,
            )
            logger.exception(
                "adjustment_execute_error",
                event_ticker=event_ticker,
                side=side_value,
            )

    def reject_adjustment(self, event_ticker: str, side_value: str) -> None:
        """Reject a pending bid adjustment proposal."""
        if self._adjuster is None:
            return
        side = Side(side_value)
        self._adjuster.clear_proposal(event_ticker, side)
        self.notify(
            f"Adjustment rejected: {event_ticker} side {side_value}",
            severity="information",
        )

    def refresh_opportunities(self) -> None:
        """Update the opportunities table from scanner state."""
        table = self.query_one(OpportunitiesTable)
        table.refresh_from_scanner(self._scanner, self._tracker)

    @work(thread=False)
    async def refresh_account(self) -> None:
        """Fetch balance and orders, derive positions from order data."""
        if self._rest is None:
            return
        try:
            balance = await self._rest.get_balance()
            panel = self.query_one(AccountPanel)
            panel.update_balance(balance.balance, balance.portfolio_value)

            orders = await self._rest.get_orders(limit=50)
            self._orders_cache = orders

            # Update top-of-market tracker with current orders
            if self._tracker is not None and self._scanner is not None:
                self._tracker.update_orders(orders, self._scanner.pairs)

            # Fetch queue positions and merge into cache (only positive values)
            try:
                tickers = self._active_market_tickers()
                if tickers:
                    new_qp = await self._rest.get_queue_positions(market_tickers=tickers)
                    for oid, qp in new_qp.items():
                        if qp > 0:
                            self._queue_cache[oid] = _merge_queue(
                                self._queue_cache.get(oid), qp
                            )
            except Exception:
                logger.debug("queue_positions_fetch_failed")

            # Apply cached queue positions to orders
            for order in orders:
                qp = self._queue_cache.get(order.order_id)
                if qp is not None:
                    order.queue_position = qp

            # Prune cache entries for orders no longer active
            active_ids = {o.order_id for o in orders if o.remaining_count > 0}
            self._queue_cache = {
                oid: v for oid, v in self._queue_cache.items() if oid in active_ids
            }

            # Derive position summaries from orders + scanner pairs → table
            if self._scanner is not None:
                summaries = compute_event_positions(orders, self._scanner.pairs)
                self._enrich_with_cpm(summaries)
                table = self.query_one(OpportunitiesTable)
                table.update_positions(summaries)

                # Sync position ledgers for bid adjustment safety (Principle 15)
                if self._adjuster is not None:
                    for pair in self._scanner.pairs:
                        try:
                            ledger = self._adjuster.get_ledger(pair.event_ticker)
                            ledger.sync_from_orders(
                                orders, ticker_a=pair.ticker_a, ticker_b=pair.ticker_b
                            )
                            # Check for completed sides → re-evaluate deferred jumps
                            for side in (Side.A, Side.B):
                                if ledger.is_unit_complete(side):
                                    self._adjuster.on_side_complete(
                                        pair.event_ticker, side
                                    )
                        except KeyError:
                            pass  # Pair not registered with adjuster yet

            # Build enriched order dicts for the order log
            order_data = [
                {
                    "ticker": o.ticker,
                    "side": o.side,
                    "price": o.no_price if o.side == "no" else o.yes_price,
                    "filled": o.fill_count,
                    "total": o.initial_count,
                    "remaining": o.remaining_count,
                    "status": o.status,
                    "time": o.created_time[11:16] if len(o.created_time) > 16 else o.created_time,
                    "queue_pos": o.queue_position,
                }
                for o in orders
            ]
            log = self.query_one(OrderLog)
            log.update_orders(order_data)
        except Exception:
            logger.exception("refresh_account_error")

    @work(thread=False)
    async def refresh_queue_positions(self) -> None:
        """Poll queue positions on a fast cadence (3s) and update display."""
        if self._rest is None or self._scanner is None:
            return
        try:
            tickers = self._active_market_tickers()
            if not tickers:
                return
            new_qp = await self._rest.get_queue_positions(market_tickers=tickers)
            for oid, qp in new_qp.items():
                if qp > 0:
                    self._queue_cache[oid] = _merge_queue(self._queue_cache.get(oid), qp)
        except Exception:
            logger.debug("queue_poll_failed")
            return

        if not self._orders_cache:
            return

        # Apply updated cache to cached orders and recompute positions
        for order in self._orders_cache:
            qp = self._queue_cache.get(order.order_id)
            if qp is not None:
                order.queue_position = qp
        summaries = compute_event_positions(self._orders_cache, self._scanner.pairs)
        self._enrich_with_cpm(summaries)
        table = self.query_one(OpportunitiesTable)
        table.update_positions(summaries)

    @work(thread=False)
    async def refresh_trades(self) -> None:
        """Fetch recent trades for active tickers and update CPM tracker."""
        if self._rest is None or self._scanner is None:
            return
        tickers = self._active_market_tickers()
        if not tickers:
            return
        for ticker in tickers:
            try:
                trades = await self._rest.get_trades(ticker, limit=50)
                self._cpm.ingest(ticker, trades)
                logger.debug("trades_ingested", ticker=ticker, count=len(trades))
            except Exception:
                logger.warning("trade_fetch_failed", ticker=ticker, exc_info=True)
        self._cpm.prune()

    def _enrich_with_cpm(self, summaries: list[EventPositionSummary]) -> None:
        """Set CPM and ETA fields on each leg summary from the CPM tracker."""
        for s in summaries:
            for leg in (s.leg_a, s.leg_b):
                leg.cpm = self._cpm.cpm(leg.ticker)
                leg.cpm_partial = self._cpm.is_partial(leg.ticker)
                if leg.queue_position is not None and leg.queue_position > 0:
                    leg.eta_minutes = self._cpm.eta_minutes(
                        leg.ticker, leg.queue_position
                    )

    def action_add_games(self) -> None:
        """Open the Add Games modal."""
        self._open_add_games()

    @work(thread=False, exclusive=True, group="add_games")
    async def _open_add_games(self) -> None:
        urls = await self.push_screen_wait(AddGamesScreen())
        if urls is None or self._game_manager is None:
            return
        await self._do_add_games(urls)

    async def _do_add_games(self, urls: list[str]) -> None:
        if self._game_manager is None:
            return
        try:
            await self._game_manager.add_games(urls)
            self.notify(f"Added {len(urls)} game(s)", severity="information")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error", markup=False)
            logger.exception("add_games_error")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open bid modal when a row is selected."""
        if self._scanner is None:
            return
        event_ticker = str(event.row_key.value)
        opp = self._scanner.get_opportunity(event_ticker)
        if opp is None:
            opp = self._scanner.all_snapshots.get(event_ticker)
        if opp is not None:
            self.push_screen(BidScreen(opp), callback=self._on_bid_confirmed)

    def _on_bid_confirmed(self, result: BidConfirmation | None) -> None:
        """Handle result from Bid modal."""
        if result is None or self._rest is None:
            return
        self._place_bids(result)

    @work(thread=False)
    async def _place_bids(self, bid: BidConfirmation) -> None:
        """Place NO orders on both legs."""
        if self._rest is None:
            return
        try:
            order_a = await self._rest.create_order(
                ticker=bid.ticker_a,
                action="buy",
                side="no",
                no_price=bid.no_a,
                count=bid.qty,
            )
            logger.info("order_placed", ticker=bid.ticker_a, order_id=order_a.order_id)
            order_b = await self._rest.create_order(
                ticker=bid.ticker_b,
                action="buy",
                side="no",
                no_price=bid.no_b,
                count=bid.qty,
            )
            logger.info("order_placed", ticker=bid.ticker_b, order_id=order_b.order_id)
            self.notify(
                f"Orders placed: {bid.ticker_a} @ {bid.no_a}¢, {bid.ticker_b} @ {bid.no_b}¢",
                severity="information",
                timeout=10,
            )
        except Exception as e:
            self.notify(
                f"Order error: {type(e).__name__}: {e}",
                severity="error",
                markup=False,
                timeout=15,
            )
            logger.exception("place_bids_error")

    def action_remove_game(self) -> None:
        """Remove the currently selected game."""
        if self._game_manager is None or self._scanner is None:
            return
        table = self.query_one(OpportunitiesTable)
        if table.cursor_row is not None:
            try:
                row_key = table.get_row_at(table.cursor_row)
                event_ticker = str(row_key[0])  # first column is event_ticker
                self._remove_game_async(event_ticker)
            except Exception:
                logger.debug("remove_game_no_selection")

    @work(thread=False)
    async def _remove_game_async(self, event_ticker: str) -> None:
        """Remove a game in background."""
        if self._game_manager is None:
            return
        try:
            await self._game_manager.remove_game(event_ticker)
            self.notify(f"Removed {event_ticker}", severity="information")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error", markup=False)

    def action_clear_games(self) -> None:
        """Clear all monitored games."""
        if self._game_manager is None:
            return
        self._clear_all_games()

    @work(thread=False)
    async def _clear_all_games(self) -> None:
        if self._game_manager is None:
            return
        try:
            count = len(self._game_manager.active_games)
            await self._game_manager.clear_all_games()
            self.notify(f"Cleared {count} game(s)", severity="information")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error", markup=False)
