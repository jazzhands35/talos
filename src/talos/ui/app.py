"""Main Talos TUI application.

Thin UI shell — all trading logic lives in TradingEngine.
"""

from __future__ import annotations

from typing import cast

import structlog
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.notifications import SeverityLevel
from textual.widgets import DataTable, Footer, Header

from talos.engine import TradingEngine
from talos.models.adjustment import ProposedAdjustment
from talos.models.strategy import BidConfirmation
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
        ("x", "clear_games", "Clear All"),
        ("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        engine: TradingEngine | None = None,
        scanner: ArbitrageScanner | None = None,
    ) -> None:
        super().__init__()
        self._engine = engine
        # Test mode: scanner-only for table tests without a full engine
        self._scanner = scanner or (engine.scanner if engine else None)

    def compose(self) -> ComposeResult:
        yield Header()
        yield OpportunitiesTable(id="opportunities-table")
        with Horizontal(id="bottom-panels"):
            yield AccountPanel(id="account-panel")
            yield OrderLog(id="order-log")
        yield Footer()

    def on_mount(self) -> None:
        """Start polling timers and wire engine callbacks."""
        if self._scanner is not None:
            self.set_interval(0.5, self.refresh_opportunities)
        if self._engine is not None:
            self.set_interval(10.0, self._poll_account)
            self.set_interval(3.0, self._poll_queue)
            self.set_interval(10.0, self._poll_trades)
            self._engine.on_notification = self._on_engine_notification
            self._engine.adjuster.on_proposal = self._on_adjustment_proposed
            self._engine.tracker.on_change = self._engine.on_top_of_market_change
            self._start_feed()

    # ── Engine callbacks ──────────────────────────────────────────

    def _on_engine_notification(self, message: str, severity: str) -> None:
        """Forward engine notifications to Textual toasts."""
        self.notify(message, severity=cast(SeverityLevel, severity), markup=False)

    def _on_adjustment_proposed(self, proposal: ProposedAdjustment) -> None:
        """Show bid adjustment proposal for operator approval."""
        self.notify(
            f"Adjustment proposed: {proposal.event_ticker} side {proposal.side}\n"
            f"{proposal.reason}\n"
            f"Before: {proposal.position_before}\n"
            f"After: {proposal.position_after}\n"
            f"Safety: {proposal.safety_check}",
            severity="warning",
            timeout=30,
        )

    # ── Polling delegations ───────────────────────────────────────

    @work(thread=False)
    async def _start_feed(self) -> None:
        if self._engine is not None:
            await self._engine.start_feed()

    @work(thread=False)
    async def _poll_account(self) -> None:
        if self._engine is None:
            return
        await self._engine.refresh_account()
        self.query_one(AccountPanel).update_balance(
            self._engine.balance, self._engine.portfolio_value
        )
        self.query_one(OpportunitiesTable).update_positions(
            self._engine.position_summaries
        )
        self.query_one(OrderLog).update_orders(self._engine.order_data)

    @work(thread=False)
    async def _poll_queue(self) -> None:
        if self._engine is None:
            return
        await self._engine.refresh_queue_positions()
        self.query_one(OpportunitiesTable).update_positions(
            self._engine.position_summaries
        )

    @work(thread=False)
    async def _poll_trades(self) -> None:
        if self._engine is not None:
            await self._engine.refresh_trades()

    def refresh_opportunities(self) -> None:
        """Update the opportunities table from scanner state."""
        tracker = self._engine.tracker if self._engine else None
        self.query_one(OpportunitiesTable).refresh_from_scanner(
            self._scanner, tracker
        )

    # ── Actions ───────────────────────────────────────────────────

    def action_add_games(self) -> None:
        self._open_add_games()

    @work(thread=False, exclusive=True, group="add_games")
    async def _open_add_games(self) -> None:
        urls = await self.push_screen_wait(AddGamesScreen())
        if urls is not None and self._engine is not None:
            await self._engine.add_games(urls)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self._scanner is None:
            return
        event_ticker = str(event.row_key.value)
        opp = self._scanner.get_opportunity(event_ticker)
        if opp is None:
            opp = self._scanner.all_snapshots.get(event_ticker)
        if opp is not None:
            self.push_screen(BidScreen(opp), callback=self._on_bid_confirmed)

    def _on_bid_confirmed(self, result: BidConfirmation | None) -> None:
        if result is not None and self._engine is not None:
            self._place_bids(result)

    @work(thread=False)
    async def _place_bids(self, bid: BidConfirmation) -> None:
        if self._engine is not None:
            await self._engine.place_bids(bid)

    def action_remove_game(self) -> None:
        if self._scanner is None:
            return
        table = self.query_one(OpportunitiesTable)
        if table.cursor_row is not None:
            try:
                row_key = table.get_row_at(table.cursor_row)
                event_ticker = str(row_key[0])
                self._remove_game(event_ticker)
            except Exception:
                logger.debug("remove_game_no_selection")

    @work(thread=False)
    async def _remove_game(self, event_ticker: str) -> None:
        if self._engine is not None:
            await self._engine.remove_game(event_ticker)

    def action_clear_games(self) -> None:
        if self._engine is not None:
            self._clear_all_games()

    @work(thread=False)
    async def _clear_all_games(self) -> None:
        if self._engine is not None:
            await self._engine.clear_games()

    @work(thread=False)
    async def approve_adjustment(
        self, event_ticker: str, side_value: str
    ) -> None:
        if self._engine is not None:
            await self._engine.approve_adjustment(event_ticker, side_value)

    def reject_adjustment(self, event_ticker: str, side_value: str) -> None:
        if self._engine is not None:
            self._engine.reject_adjustment(event_ticker, side_value)
