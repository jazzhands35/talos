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
from textual.widgets import DataTable, Footer, Header, Static
from textual.widgets._data_table import CellDoesNotExist

from talos.auto_accept import AutoAcceptState
from talos.auto_accept_log import AutoAcceptLogger
from talos.engine import TradingEngine
from talos.models.proposal import ProposalKey
from talos.models.strategy import BidConfirmation
from talos.proposal_queue import ProposalQueue
from talos.scanner import ArbitrageScanner
from talos.ui.proposal_panel import ProposalPanel
from talos.ui.screens import AddGamesScreen, AutoAcceptScreen, BidScreen, ScanScreen, UnitSizeScreen
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
        ("u", "set_unit_size", "Unit Size"),
        ("s", "toggle_suggestions", "Suggestions"),
        ("y", "approve_proposal", "Approve"),
        ("n", "reject_proposal", "Reject"),
        ("f", "toggle_auto_accept", "Auto-Accept"),
        ("p", "show_proposals", "Proposals"),
        ("c", "scan", "Scan"),
        ("o", "open_in_browser", "Open"),
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
        self._auto_accept = AutoAcceptState()
        self._poll_in_progress = False
        self._auto_accept_logger: AutoAcceptLogger | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            "WEBSOCKET DISCONNECTED — ALL PRICES ARE STALE — RESTART TALOS",
            id="ws-disconnect-banner",
        )
        yield OpportunitiesTable(id="opportunities-table")
        with Horizontal(id="bottom-panels"):
            yield AccountPanel(id="account-panel")
            yield OrderLog(id="order-log")
        yield Footer()

    def on_mount(self) -> None:
        """Start polling timers and wire engine callbacks."""
        if self._scanner is not None:
            self.set_interval(2.0, self.refresh_opportunities)
        if self._engine is not None:
            self.set_interval(10.0, self._poll_balance)
            self.set_interval(30.0, self._poll_account)  # backup sync — WS is primary
            self.set_interval(3.0, self._poll_queue)
            self.set_interval(30.0, self._poll_trades)
            self.set_interval(1.0, self._refresh_proposals)
            self.set_interval(1.0, self._auto_accept_tick)
            self.set_interval(3600.0, self._refresh_game_status)
            self.set_interval(300.0, self._refresh_volumes)
            self.set_interval(10.0, self._log_market_snapshots)
            self._engine.on_notification = self._on_engine_notification
            self._engine.tracker.on_change = self._engine.on_top_of_market_change
            if self._engine.game_status_resolver is not None:
                table = self.query_one(OpportunitiesTable)
                table.set_resolver(self._engine.game_status_resolver)
            # Auto-accept on by default (24h), press F to toggle off
            self._start_auto_accept(24.0)
            self._start_feed()

    # ── Engine callbacks ──────────────────────────────────────────

    def _on_engine_notification(self, message: str, severity: str) -> None:
        """Forward engine notifications to Textual toasts."""
        self.notify(message, severity=cast(SeverityLevel, severity), markup=False)

    def _refresh_proposals(self) -> None:
        """Update subtitle and WS disconnect banner."""
        # WS disconnect: show red banner and title bar warning
        banner = self.query_one("#ws-disconnect-banner", Static)
        ws_dead = self._engine is not None and not self._engine.ws_connected
        if ws_dead:
            banner.add_class("visible")
        else:
            banner.remove_class("visible")
        # Sub_title: WS warning takes priority, then auto-accept, then clear
        if ws_dead:
            self.sub_title = "!!! WEBSOCKET DISCONNECTED — PRICES ARE STALE !!!"
        elif self._auto_accept.active:
            self.sub_title = (
                f"AUTO-ACCEPT {self._auto_accept.remaining_str()} remaining "
                f"({self._auto_accept.accepted_count} accepted)"
            )
        else:
            self.sub_title = ""

    @work(thread=False)
    async def _auto_accept_tick(self) -> None:
        """Each second: if auto-accept is active, approve the oldest pending proposal."""
        if not self._auto_accept.active or self._engine is None:
            return

        if self._auto_accept.is_expired():
            self._stop_auto_accept()
            return

        pending = self._engine.proposal_queue.pending()
        if not pending:
            return

        proposal = pending[0]
        snapshot = self._capture_state_snapshot()
        try:
            await self._engine.approve_proposal(proposal.key)
            self._auto_accept.accepted_count += 1
            if self._auto_accept_logger:
                self._auto_accept_logger.log_accepted(proposal, snapshot, self._auto_accept)
        except Exception as e:
            logger.exception("auto_accept_error", proposal_key=str(proposal.key))
            if self._auto_accept_logger:
                self._auto_accept_logger.log_error(proposal, str(e), snapshot, self._auto_accept)

    def _capture_state_snapshot(self) -> dict[str, object]:
        """Capture full trading state for JSONL logging."""
        if self._engine is None:
            return {}

        positions: dict[str, dict[str, object]] = {}
        for summary in self._engine.position_summaries:
            positions[summary.event_ticker] = {
                "status": summary.status,
                "leg_a_filled": summary.leg_a.filled_count,
                "leg_a_resting": summary.leg_a.resting_count,
                "leg_b_filled": summary.leg_b.filled_count,
                "leg_b_resting": summary.leg_b.resting_count,
                "matched_pairs": summary.matched_pairs,
                "locked_profit_cents": summary.locked_profit_cents,
            }

        resting_orders = [
            {
                "ticker": o.ticker,
                "no_price": o.no_price,
                "remaining": o.remaining_count,
                "side": o.side,
                "status": o.status,
            }
            for o in self._engine.orders
            if o.status == "resting"
        ]

        top_of_market: dict[str, dict[str, int | None]] = {}
        tracker = self._engine.tracker
        for ticker in tracker.resting_tickers:
            top_of_market[ticker] = {
                "resting_price": tracker.resting_price(ticker),
                "book_top": tracker.book_top_price(ticker),
            }

        opportunities: list[dict[str, object]] = []
        if self._scanner:
            for opp in self._scanner.opportunities:
                opportunities.append(
                    {
                        "event_ticker": opp.event_ticker,
                        "fee_edge": opp.fee_edge,
                        "no_a": opp.no_a,
                        "no_b": opp.no_b,
                    }
                )

        return {
            "positions": positions,
            "balance_cents": self._engine.balance,
            "portfolio_value_cents": self._engine.portfolio_value,
            "resting_orders": resting_orders,
            "top_of_market": top_of_market,
            "scanner_opportunities": opportunities,
        }

    def action_show_proposals(self) -> None:
        """Show pending proposals in a popup."""
        if self._engine is None:
            return
        pending = self._engine.proposal_queue.pending()
        if not pending:
            self.notify("No pending proposals", severity="information")
            return
        lines = [f"{len(pending)} pending proposal(s):"]
        for p in pending[:10]:
            lines.append(f"  {p.summary}")
        self.notify("\n".join(lines), severity="information")

    def on_proposal_panel_approved(self, event: ProposalPanel.Approved) -> None:
        """Handle operator approving a proposal."""
        self._execute_approval(event.key)

    @work(thread=False)
    async def _execute_approval(self, key: ProposalKey) -> None:
        if self._engine is not None:
            await self._engine.approve_proposal(key)

    def on_proposal_panel_rejected(self, event: ProposalPanel.Rejected) -> None:
        """Handle operator rejecting a proposal."""
        if self._engine is not None:
            self._engine.reject_proposal(event.key)

    # ── Polling delegations ───────────────────────────────────────

    @work(thread=False)
    async def _start_feed(self) -> None:
        if self._engine is not None:
            await self._engine.start_feed()

    @work(thread=False)
    async def _poll_balance(self) -> None:
        """Fast balance poll — no WS channel for balance."""
        if self._engine is None:
            return
        await self._engine.refresh_balance()
        self.query_one(AccountPanel).update_balance(
            self._engine.balance, self._engine.portfolio_value
        )

    @work(thread=False)
    async def _poll_account(self) -> None:
        """Backup REST sync every 30s — WS handles real-time updates."""
        if self._engine is None:
            return
        if self._poll_in_progress:
            return  # Previous poll still running — skip this cycle
        self._poll_in_progress = True
        try:
            await self._engine.refresh_account()
        finally:
            self._poll_in_progress = False
        table = self.query_one(OpportunitiesTable)
        table.update_positions(self._engine.position_summaries)
        table._all_dirty = True  # position data changed, rebuild all rows
        self.query_one(OrderLog).update_orders(self._engine.order_data)

    @work(thread=False)
    async def _poll_queue(self) -> None:
        if self._engine is None:
            return
        await self._engine.refresh_queue_positions()
        self.query_one(OpportunitiesTable).update_positions(self._engine.position_summaries)

    @work(thread=False)
    async def _poll_trades(self) -> None:
        if self._engine is not None:
            await self._engine.refresh_trades()

    def _log_market_snapshots(self) -> None:
        """Log market snapshots every 10s for ML data collection."""
        if self._engine is None or not hasattr(self._engine, '_data_collector'):
            return
        dc = self._engine._data_collector
        if dc is None:
            return
        scanner = self._engine.scanner
        snapshots = []
        for opp in scanner.all_snapshots.values():
            pos = None
            for s in self._engine.position_summaries:
                if s.event_ticker == opp.event_ticker:
                    pos = s
                    break
            status = self._engine.event_statuses.get(opp.event_ticker, "")
            gs = self._engine.game_status_resolver.get(opp.event_ticker) if self._engine.game_status_resolver else None
            snapshots.append({
                "event_ticker": opp.event_ticker,
                "ticker_a": opp.ticker_a,
                "ticker_b": opp.ticker_b,
                "no_a": opp.no_a,
                "no_b": opp.no_b,
                "edge": opp.fee_edge,
                "volume_a": 0,  # from ticker feed if available
                "volume_b": 0,
                "open_interest_a": 0,
                "open_interest_b": 0,
                "game_state": gs.state if gs else "unknown",
                "status": status,
                "filled_a": pos.leg_a.filled_count if pos else 0,
                "filled_b": pos.leg_b.filled_count if pos else 0,
                "resting_a": pos.leg_a.resting_count if pos else 0,
                "resting_b": pos.leg_b.resting_count if pos else 0,
            })
        if snapshots:
            dc.log_market_snapshots(snapshots)

    @work(thread=False)
    async def _refresh_volumes(self) -> None:
        if self._engine is not None:
            await self._engine.game_manager.refresh_volumes()

    @work(thread=False)
    async def _refresh_game_status(self) -> None:
        if self._engine is not None:
            await self._engine.refresh_game_status()

    def mark_event_dirty(self, event_ticker: str) -> None:
        """Mark an event for table refresh on next cycle."""
        self.query_one(OpportunitiesTable).mark_dirty(event_ticker)

    def refresh_opportunities(self) -> None:
        """Update the opportunities table from scanner state.

        In production, dirty tracking limits which rows rebuild.
        In test mode (no engine), mark all dirty each cycle.
        """
        table = self.query_one(OpportunitiesTable)
        if self._engine is None:
            table._all_dirty = True  # test mode — no WS dirty tracking
        if self._engine is not None:
            table.update_labels(self._engine.game_manager.labels)
            table.update_volumes(self._engine.game_manager.volumes_24h)
            table.update_statuses(self._engine.event_statuses)
        tracker = self._engine.tracker if self._engine else None
        table.refresh_from_scanner(self._scanner, tracker)

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        """Forward header clicks to the opportunities table for sorting."""
        self.query_one(OpportunitiesTable).toggle_sort(event.column_index)

    # ── Actions ───────────────────────────────────────────────────

    def action_add_games(self) -> None:
        self._open_add_games()

    @work(thread=False, exclusive=True, group="add_games")
    async def _open_add_games(self) -> None:
        urls = await self.push_screen_wait(AddGamesScreen())
        if urls is not None and self._engine is not None:
            await self._engine.add_games(urls)

    def action_scan(self) -> None:
        if self._engine is not None:
            self._run_scan()

    @work(thread=False, exclusive=True, group="scan")
    async def _run_scan(self) -> None:
        if self._engine is None:
            return
        self.notify("Scanning for events...")
        try:
            events = await self._engine.game_manager.scan_events()
        except Exception as e:
            self.notify(f"Scan failed: {e}", severity="error")
            return
        if not events:
            self.notify("No new events found", severity="information")
            return

        self.notify(f"Found {len(events)} events")
        selected = await self.push_screen_wait(ScanScreen(events))
        if selected and self._engine is not None:
            await self._engine.add_games(selected)
            self.notify(f"Added {len(selected)} event(s)")

    def action_open_in_browser(self) -> None:
        """Open the highlighted event on Kalshi's website."""
        import webbrowser

        table = self.query_one(OpportunitiesTable)
        if table.cursor_row is None or table.row_count == 0:
            return
        cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        event_ticker = str(cell_key.row_key.value)
        if not event_ticker:
            return
        series = event_ticker.split("-")[0].lower()
        url = f"https://kalshi.com/markets/{series}/{event_ticker.lower()}"
        webbrowser.open(url)

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
                row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
                event_ticker = str(row_key.value)
                self._remove_game(event_ticker)
            except CellDoesNotExist:
                logger.debug("remove_game_no_selection")

    @work(thread=False)
    async def _remove_game(self, event_ticker: str) -> None:
        if self._engine is not None:
            await self._engine.remove_game(event_ticker)

    def action_approve_proposal(self) -> None:
        if self._engine is not None:
            pending = self._engine.proposal_queue.pending()
            if pending:
                self._execute_approval(pending[0].key)

    def action_reject_proposal(self) -> None:
        if self._engine is not None:
            pending = self._engine.proposal_queue.pending()
            if pending:
                self._engine.reject_proposal(pending[0].key)

    def action_set_unit_size(self) -> None:
        if self._engine is not None:
            self._open_unit_size()

    @work(thread=False, exclusive=True, group="unit_size")
    async def _open_unit_size(self) -> None:
        current = self._engine.unit_size if self._engine else 10
        result = await self.push_screen_wait(UnitSizeScreen(current))
        if result is not None and self._engine is not None:
            self._engine.set_unit_size(result)
            self.notify(f"Unit size set to {result}", severity="information", markup=False)

    def action_toggle_suggestions(self) -> None:
        if self._engine is None:
            return
        cfg = self._engine.automation_config
        cfg.enabled = not cfg.enabled
        if cfg.enabled:
            self.notify(
                f"Suggestions ON (min edge: {cfg.edge_threshold_cents:.1f}c, "
                f"stability: {cfg.stability_seconds:.0f}s)",
                severity="information",
                markup=False,
            )
        else:
            self.notify("Suggestions OFF", severity="information", markup=False)

    def action_toggle_auto_accept(self) -> None:
        if self._engine is None:
            return
        if self._auto_accept.active:
            self._stop_auto_accept()
        else:
            self._open_auto_accept()

    @work(thread=False, exclusive=True, group="auto_accept")
    async def _open_auto_accept(self) -> None:
        hours = await self.push_screen_wait(AutoAcceptScreen())
        if hours is not None and self._engine is not None:
            self._start_auto_accept(hours)

    def _start_auto_accept(self, hours: float) -> None:
        """Activate auto-accept for the given duration."""
        if self._engine is None:
            return

        from pathlib import Path

        self._auto_accept.start(hours=hours)

        log_dir = Path(__file__).resolve().parents[3] / "auto_accept_sessions"
        aa_logger = AutoAcceptLogger(log_dir)
        self._auto_accept_logger = aa_logger

        cfg = self._engine.automation_config
        config: dict[str, object] = {
            "edge_threshold_cents": cfg.edge_threshold_cents,
            "stability_seconds": cfg.stability_seconds,
            "unit_size": self._engine.unit_size,
        }
        aa_logger.log_session_start(self._auto_accept, config)

        self.notify(
            f"Auto-accept ON — {hours:.1f}h",
            severity="warning",
            markup=False,
        )
        logger.info("auto_accept_started", hours=hours)

    def _stop_auto_accept(self) -> None:
        """Deactivate auto-accept and log session end."""
        count = self._auto_accept.accepted_count
        elapsed = self._auto_accept.elapsed_str()

        if self._auto_accept_logger and self._engine:
            final_positions: dict[str, object] = {}
            for s in self._engine.position_summaries:
                final_positions[s.event_ticker] = {
                    "status": s.status,
                    "leg_a_filled": s.leg_a.filled_count,
                    "leg_b_filled": s.leg_b.filled_count,
                }
            self._auto_accept_logger.log_session_end(self._auto_accept, final_positions)

        self._auto_accept.stop()
        self._auto_accept_logger = None

        self.notify(
            f"Auto-accept OFF — {count} accepted in {elapsed}",
            severity="information",
            markup=False,
        )
        logger.info("auto_accept_stopped", accepted_count=count, elapsed=elapsed)

    def action_clear_games(self) -> None:
        if self._engine is not None:
            self._clear_all_games()

    @work(thread=False)
    async def _clear_all_games(self) -> None:
        if self._engine is not None:
            await self._engine.clear_games()
