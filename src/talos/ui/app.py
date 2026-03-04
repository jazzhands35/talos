"""Main Talos TUI application."""

from __future__ import annotations

import structlog
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header

from talos.game_manager import GameManager
from talos.market_feed import MarketFeed
from talos.models.strategy import BidConfirmation
from talos.position import compute_event_positions
from talos.rest_client import KalshiRESTClient
from talos.scanner import ArbitrageScanner
from talos.ui.screens import AddGamesScreen, BidScreen
from talos.ui.theme import APP_CSS
from talos.ui.widgets import AccountPanel, OpportunitiesTable, OrderLog

logger = structlog.get_logger()


class TalosApp(App):
    """Talos arbitrage trading dashboard."""

    CSS = APP_CSS
    TITLE = "TALOS"
    BINDINGS = [
        ("a", "add_games", "Add Games"),
        ("d", "remove_game", "Remove Game"),
        ("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        scanner: ArbitrageScanner | None = None,
        game_manager: GameManager | None = None,
        rest_client: KalshiRESTClient | None = None,
        market_feed: MarketFeed | None = None,
    ) -> None:
        super().__init__()
        self._scanner = scanner
        self._game_manager = game_manager
        self._rest = rest_client
        self._feed = market_feed

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
        if self._feed is not None:
            self._start_feed()

    @work(thread=False)
    async def _start_feed(self) -> None:
        """Connect WebSocket and listen for market data."""
        if self._feed is None:
            return
        try:
            await self._feed.connect()
            self.notify("WebSocket connected", severity="information")
            await self._feed.start()
        except Exception as e:
            logger.exception("feed_connection_error")
            self.notify(
                f"WebSocket error: {type(e).__name__}: {e}",
                severity="error",
                markup=False,
                timeout=15,
            )

    def refresh_opportunities(self) -> None:
        """Update the opportunities table from scanner state."""
        table = self.query_one(OpportunitiesTable)
        table.refresh_from_scanner(self._scanner)

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

            # Derive position summaries from orders + scanner pairs
            if self._scanner is not None:
                summaries = compute_event_positions(orders, self._scanner.pairs)
                panel.update_event_positions(summaries)
            else:
                panel.update_event_positions([])

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
                    "time": o.created_time[11:16]
                    if len(o.created_time) > 16
                    else o.created_time,
                    "queue_pos": o.queue_position,
                }
                for o in orders
            ]
            log = self.query_one(OrderLog)
            log.update_orders(order_data)
        except Exception:
            logger.exception("refresh_account_error")

    def action_add_games(self) -> None:
        """Open the Add Games modal."""
        self.push_screen(AddGamesScreen(), callback=self._on_games_added)

    def _on_games_added(self, urls: list[str] | None) -> None:
        """Handle result from Add Games modal."""
        if urls is None or self._game_manager is None:
            return
        self._add_games_async(urls)

    @work(thread=False)
    async def _add_games_async(self, urls: list[str]) -> None:
        """Add games in background."""
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
        if opp and opp.raw_edge > 0:
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
