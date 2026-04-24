"""Main Talos TUI application.

Thin UI shell — all trading logic lives in TradingEngine.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast

import structlog
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.notifications import SeverityLevel
from textual.widgets import DataTable, Footer, Header, Static
from textual.widgets._data_table import CellDoesNotExist

from talos.auto_accept import ExecutionMode
from talos.auto_accept_log import AutoAcceptLogger
from talos.automation_config import DEFAULT_UNIT_SIZE, AutomationConfig
from talos.discovery import DiscoveryService
from talos.engine import TradingEngine
from talos.errors import KalshiRateLimitError
from talos.milestones import MilestoneResolver
from talos.models.proposal import ProposalKey
from talos.models.strategy import BidConfirmation
from talos.scanner import ArbitrageScanner
from talos.tree_metadata import TreeMetadataStore
from talos.ui.event_review import EventReviewScreen
from talos.ui.proposal_panel import ProposalPanel
from talos.ui.screens import (
    AddGamesScreen,
    AutoAcceptScreen,
    BidScreen,
    MarketPickerScreen,
    ScanScreen,
    SettlementHistoryScreen,
    UnitSizeScreen,
)
from talos.ui.theme import APP_CSS
from talos.ui.widgets import (
    ActivityLog,
    OpportunitiesTable,
    OrderLog,
    PerformancePanel,
    PortfolioPanel,
)
from talos.units import ONE_CENT_BPS, ONE_CONTRACT_FP100, bps_to_cents_round

logger = structlog.get_logger()


def _event_ticker_from_row_key(raw_key: str) -> str:
    """Strip :a or :b suffix from two-row layout row keys."""
    return raw_key.rsplit(":", 1)[0] if ":" in raw_key else raw_key


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
        ("e", "toggle_exit_only", "Exit-Only"),
        ("E", "exit_all", "Exit All"),
        ("c", "scan", "Scan"),
        ("o", "open_in_browser", "Open"),
        ("r", "review_event", "Review"),
        ("h", "settlement_history", "History"),
        ("l", "copy_activity_log", "Copy Log"),
        ("b", "blacklist_ticker", "Blacklist"),
        ("B", "edit_blacklist", "Edit Blacklist"),
        ("m", "toggle_scan_mode", "Mode"),
        ("v", "toggle_view", "View"),
        ("t", "push_tree_screen", "Tree"),
        ("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        engine: TradingEngine | None = None,
        scanner: ArbitrageScanner | None = None,
        startup_execution_mode: str = "automatic",
        startup_auto_stop_hours: float | None = None,
        automation_config: AutomationConfig | None = None,
        tree_metadata_store: TreeMetadataStore | None = None,
        milestone_resolver: MilestoneResolver | None = None,
        discovery_service: DiscoveryService | None = None,
    ) -> None:
        super().__init__()
        self._engine = engine
        # Test mode: scanner-only for table tests without a full engine
        self._scanner = scanner or (engine.scanner if engine else None)
        self._startup_execution_mode = startup_execution_mode
        self._startup_auto_stop_hours = startup_auto_stop_hours
        self._execution_mode = ExecutionMode()
        self._poll_in_progress = False
        self._auto_accept_logger: AutoAcceptLogger | None = None
        self._rate_limit_until: datetime | None = None
        self._scan_mode: str = "sports"
        self.on_scan_mode_change: Callable[[str], None] | None = None
        # Tree-mode collaborators (None unless automation_config.tree_mode is on).
        self._automation_config = automation_config
        self._tree_metadata_store = tree_metadata_store
        self._milestone_resolver = milestone_resolver
        self._discovery_service = discovery_service

    def notify(self, message: object = "", *args: object, **kwargs: object) -> None:  # type: ignore[override]
        """Tee every toast notification to structlog so devs tailing the log
        file see the same messages the user sees as toasts.

        Diagnostic hook — remove (or make opt-in) after bootstrap-lag work
        is finished.
        """
        import contextlib

        with contextlib.suppress(Exception):
            logger.info(
                "notify_toast",
                message=str(message),
                severity=kwargs.get("severity", "information"),
            )
        return super().notify(message, *args, **kwargs)  # type: ignore[no-any-return]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            "WEBSOCKET DISCONNECTED — ALL PRICES ARE STALE — RESTART TALOS",
            id="ws-disconnect-banner",
        )
        yield OpportunitiesTable(id="opportunities-table")
        if self._engine is not None:
            panel = ProposalPanel(self._engine.proposal_queue, id="proposal-panel")
            panel.display = False
            yield panel
        with Horizontal(id="bottom-panels"):
            yield PortfolioPanel(id="account-panel")
            yield PerformancePanel(id="performance-panel")
            yield ActivityLog(id="activity-log")
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
            self.set_interval(3600.0, self._refresh_volumes)  # hourly — volumes change slowly
            self.set_interval(10.0, self._log_market_snapshots)
            self.set_interval(300.0, self._poll_settlements)  # every 5 minutes
            # Seed performance panel from existing cache immediately
            rows = self._engine.performance_settlement_rows()
            if rows:
                from talos.settlement_tracker import aggregate_settlements

                agg = aggregate_settlements(rows)
                self.query_one(PerformancePanel).update_performance(agg)
            # Fire first settlement poll after 30s (don't wait 5 min)
            self.set_timer(30.0, self._poll_settlements)
            self._engine.on_notification = self._on_engine_notification
            self._engine.tracker.on_change = self._engine.on_top_of_market_change
            if self._engine.game_status_resolver is not None:
                table = self.query_one(OpportunitiesTable)
                table.set_resolver(self._engine.game_status_resolver)
            # Boot into configured execution mode (startup defaults from settings.json)
            startup_mode = self._startup_execution_mode
            startup_hours = self._startup_auto_stop_hours
            if startup_mode == "automatic":
                self._enter_automatic_mode(hours=startup_hours)
            else:
                self._execution_mode.enter_manual()
            self._start_feed()
            self._start_watchdog()
            self._poll_balance()  # show cash immediately
            self._refresh_volumes()  # populate 24h volume on startup
            # Tree-mode: kick off discovery + milestone bootstrap as a
            # background worker right after engine wiring. This used to be
            # lazy (deferred until the user opened TreeScreen), but that
            # left a critical safety hole: restored pairs on a tree-mode
            # restart would trade with no milestone protection because the
            # engine readiness gate self-times out after 30s. Workers run
            # off the event loop, so the heavy 9,700-series pull doesn't
            # freeze the TUI — and the milestone-load gate in the engine
            # now actually arms before any trading cycle runs.
            self._tree_bootstrap_started = False
            if (
                self._automation_config is not None
                and self._automation_config.tree_mode
                and self._discovery_service is not None
                and self._milestone_resolver is not None
            ):
                self._tree_bootstrap_started = True
                self._bootstrap_tree_discovery()
                self._run_tree_milestone_loop()

    # ── Engine callbacks ──────────────────────────────────────────

    def _on_engine_notification(self, message: str, severity: str, toast: bool) -> None:
        """Route engine notifications to activity log or toast.

        Most automated events go to the ActivityLog panel (zero asyncio overhead).
        Only critical errors and user-initiated results use Textual toasts.
        """
        self.query_one(ActivityLog).log_activity(message, severity)
        if toast:
            self.notify(message, severity=cast(SeverityLevel, severity), markup=False)

    def _refresh_proposals(self) -> None:
        """Update subtitle with structured status bar and refresh proposal panel."""
        banner = self.query_one("#ws-disconnect-banner", Static)
        ws_dead = self._engine is not None and not self._engine.ws_connected
        if ws_dead:
            banner.add_class("visible")
        else:
            banner.remove_class("visible")

        # Structured status bar: SCAN_MODE | MODE: X | DATA: X | count
        mode_tag = "SPORTS" if self._scan_mode == "sports" else "NON-SPORTS"
        parts: list[str] = [mode_tag]

        if self._execution_mode.is_automatic:
            mode_str = "MODE: AUTO"
            remaining = self._execution_mode.remaining_str()
            if remaining:
                mode_str += f" {remaining} left"
            parts.append(mode_str)
        else:
            parts.append("MODE: MANUAL")

        parts.append("DATA: STALE" if self._is_data_stale() else "DATA: LIVE")

        if self._execution_mode.is_automatic:
            parts.append(f"{self._execution_mode.accepted_count} accepted")

        self.sub_title = " | ".join(parts)

        try:
            panel = self.query_one("#proposal-panel", ProposalPanel)
            if panel.display:
                panel.refresh_proposals()
        except Exception:
            pass

    def _is_data_stale(self) -> bool:
        """True if orderbook data is not fresh. 60s threshold — warns before
        the 120s recovery threshold in orderbook.py kicks in."""
        if self._engine is None:
            return True
        if not self._engine.ws_connected:
            return True
        return self._engine.seconds_since_last_book_update() > 60.0

    @work(thread=False)
    async def _auto_accept_tick(self) -> None:
        """Each second: if auto-accept is active, approve the oldest pending proposal."""
        if not self._execution_mode.is_automatic or self._engine is None:
            return

        if self._execution_mode.is_expired():
            self._end_automatic_session()
            return

        # Rate limit backoff — skip ticks until cooldown expires
        if self._rate_limit_until is not None:
            if datetime.now(UTC) < self._rate_limit_until:
                return
            self._rate_limit_until = None

        pending = self._engine.proposal_queue.pending()
        if not pending:
            return

        # Skip HOLDs — they're informational, not actionable. Leaving them
        # in the queue prevents reevaluate_jumps from re-creating them every
        # cycle, eliminating the "Dismissed: HOLD" notification flood.
        # HOLDs get naturally superseded when conditions change.
        actionable = [p for p in pending if p.kind != "hold"]
        if not actionable:
            return

        proposal = actionable[0]
        snapshot = self._capture_state_snapshot()
        try:
            await self._engine.approve_proposal(proposal.key)
            self._execution_mode.accepted_count += 1
            if self._auto_accept_logger:
                self._auto_accept_logger.log_accepted(proposal, snapshot, self._execution_mode)
        except KalshiRateLimitError as e:
            backoff = max(e.retry_after or 2.0, 2.0)
            self._rate_limit_until = datetime.now(UTC) + timedelta(seconds=backoff)
            logger.info("auto_accept_rate_limited", backoff_s=backoff)
        except Exception as e:
            logger.exception("auto_accept_error", proposal_key=str(proposal.key))
            if self._auto_accept_logger:
                self._auto_accept_logger.log_error(proposal, str(e), snapshot, self._execution_mode)

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
                "locked_profit_bps": summary.locked_profit_bps,
            }

        resting_orders = [
            {
                "ticker": o.ticker,
                "no_price": o.no_price_bps // ONE_CENT_BPS,
                "remaining": o.remaining_count_fp100 // ONE_CONTRACT_FP100,
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
        """Toggle the proposal panel sidebar."""
        if self._engine is None:
            return
        try:
            panel = self.query_one("#proposal-panel", ProposalPanel)
        except Exception:
            return
        panel.display = not panel.display
        if panel.display:
            panel.refresh_proposals()
            panel.focus()

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

    # ── Event loop watchdog ─────────────────────────────────────

    _FREEZE_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
    _FREEZE_TASK_DUMP_LIMIT = 50  # max tasks to dump per freeze event

    @work(thread=False)
    async def _start_watchdog(self) -> None:
        """Detect event loop blocking by measuring sleep accuracy.

        Sleeps for 0.5s in a loop. If actual elapsed > 2s, the event
        loop was blocked — log a warning with a sample of running tasks.
        Freeze log is capped at 10 MB to prevent disk exhaustion.
        """
        import asyncio
        import os
        import sys
        import time

        from talos.persistence import get_data_dir

        log_path = str(get_data_dir() / "talos_freeze.log")
        while True:
            t0 = time.monotonic()
            await asyncio.sleep(0.5)
            elapsed = time.monotonic() - t0

            if elapsed > 2.0:
                tasks = asyncio.all_tasks()
                task_count = len(tasks)
                # Only dump a limited sample to avoid massive I/O
                task_info = []
                for i, task in enumerate(tasks):
                    if i >= self._FREEZE_TASK_DUMP_LIMIT:
                        task_info.append(
                            f"  ... and {task_count - self._FREEZE_TASK_DUMP_LIMIT} more tasks"
                        )
                        break
                    frames = task.get_stack(limit=5)
                    frame_strs = [
                        f"  {f.f_code.co_filename}:{f.f_lineno} in {f.f_code.co_name}"
                        for f in frames
                    ]
                    stack = "\n".join(frame_strs) if frame_strs else "  (no stack)"
                    task_info.append(f"  Task {task.get_name()}: {task.get_coro()}\n{stack}")

                full_msg = (
                    f"EVENT LOOP BLOCKED for {elapsed:.1f}s (expected 0.5s)\n"
                    f"Active tasks ({task_count}):\n" + "\n".join(task_info)
                )

                logger.error("event_loop_blocked", elapsed=elapsed, task_count=task_count)
                try:
                    # Truncate log if it exceeds size cap
                    try:
                        if os.path.getsize(log_path) > self._FREEZE_LOG_MAX_BYTES:
                            with open(log_path, "w") as f:
                                f.write(
                                    f"[{time.strftime('%H:%M:%S')}]"
                                    " --- log rotated (exceeded 10 MB) ---\n\n"
                                )
                    except OSError:
                        pass
                    with open(log_path, "a") as f:
                        f.write(f"[{time.strftime('%H:%M:%S')}] {full_msg}\n\n")
                except Exception:
                    pass

                print(f"\n!!! FREEZE DETECTED: {elapsed:.1f}s !!!", file=sys.stderr)

    # ── Polling delegations ───────────────────────────────────────

    @work(thread=False)
    async def _start_feed(self) -> None:
        if self._engine is not None:
            await self._engine.start_feed()

    @work(thread=False)
    async def _bootstrap_tree_discovery(self) -> None:
        """Tree-mode: bootstrap discovery + initial milestone refresh, then
        signal the engine that it is ready for trading."""
        import time as _time

        if self._discovery_service is None or self._milestone_resolver is None:
            return
        t0 = _time.perf_counter()
        import structlog as _structlog

        _log = _structlog.get_logger()
        _log.info("tree_bootstrap_start")
        self.notify("Tree: loading series catalog...", severity="information")

        t_fetch = _time.perf_counter()
        await self._discovery_service.bootstrap()
        series_ms = int((_time.perf_counter() - t_fetch) * 1000)
        series_count = sum(c.series_count for c in self._discovery_service.categories.values())
        _log.info(
            "tree_bootstrap_series_done",
            elapsed_ms=series_ms,
            series_count=series_count,
        )
        self.notify(
            f"Series: {series_count} in {series_ms} ms",
            severity="information",
        )

        t_ms = _time.perf_counter()
        await self._milestone_resolver.refresh()
        ms_elapsed = int((_time.perf_counter() - t_ms) * 1000)
        _log.info(
            "tree_bootstrap_milestones_done",
            elapsed_ms=ms_elapsed,
            milestone_count=self._milestone_resolver.count,
        )
        self.notify(
            f"Milestones: {self._milestone_resolver.count} in {ms_elapsed} ms",
            severity="information",
        )

        if self._engine is not None:
            self._engine._ready_for_trading.set()

        total_ms = int((_time.perf_counter() - t0) * 1000)
        _log.info("tree_bootstrap_complete", total_ms=total_ms)
        self.notify(f"Tree: loaded in {total_ms} ms", severity="information")

    @work(thread=False)
    async def _run_tree_milestone_loop(self) -> None:
        """Tree-mode: periodic milestone refresh loop (runs until shutdown).

        Delays the first refresh until after bootstrap completes so the two
        aren't competing for network/CPU during user-visible TreeScreen mount.
        """
        import asyncio

        if (
            self._discovery_service is None
            or self._milestone_resolver is None
            or self._automation_config is None
        ):
            return
        # Wait for bootstrap to signal completion — no point refreshing
        # milestones while the series catalog is still downloading.
        import contextlib

        if self._engine is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._engine._ready_for_trading.wait(),
                    timeout=60.0,
                )
        await self._discovery_service.run_milestone_loop(
            self._milestone_resolver,
            interval_seconds=self._automation_config.milestone_refresh_seconds,
        )

    @work(thread=False)
    async def _poll_balance(self) -> None:
        """Fast balance poll — no WS channel for balance."""
        if self._engine is None:
            return
        await self._engine.refresh_balance()
        self.query_one(PortfolioPanel).update_balance(
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

    @work(thread=False, exclusive=True, group="poll_queue")
    async def _poll_queue(self) -> None:
        if self._engine is None:
            return
        await self._engine.refresh_queue_positions()
        table = self.query_one(OpportunitiesTable)
        table.update_positions(self._engine.position_summaries)
        table._all_dirty = True

    @work(thread=False, exclusive=True, group="poll_trades")
    async def _poll_trades(self) -> None:
        if self._engine is None:
            return
        await self._engine.refresh_trades()
        # CPM data changed — recompute positions so ETA columns update
        self._engine.recompute_positions()
        table = self.query_one(OpportunitiesTable)
        table.update_positions(self._engine.position_summaries)
        table._all_dirty = True

    @work(thread=False, exclusive=True, group="settlements")
    async def _poll_settlements(self) -> None:
        if self._engine is None:
            return
        try:
            settlements = await self._engine.get_all_settlements()
            from talos.settlement_tracker import reconcile_event

            # Populate cache if available
            if self._engine.has_settlement_cache:
                est_map = {
                    s.event_ticker: bps_to_cents_round(int(s.locked_profit_bps))
                    for s in self._engine.position_summaries
                }
                self._engine.cache_settlements(settlements, est_pnl_map=est_map)

            # Reconciliation: check for discrepancies
            summaries_by_event = {s.event_ticker: s for s in self._engine.position_summaries}
            for s in settlements:
                pos = summaries_by_event.get(s.event_ticker)
                if pos is None:
                    continue
                our_expected = bps_to_cents_round(int(pos.locked_profit_bps))
                disc = reconcile_event(
                    our_expected, s.revenue_bps // ONE_CENT_BPS, s.event_ticker
                )
                if disc is not None and abs(disc["difference"]) > 5:
                    self.query_one(ActivityLog).log_activity(
                        f"P&L DISCREPANCY {s.event_ticker}: "
                        f"ours=${disc['our_revenue'] / 100:.2f} "
                        f"kalshi=${disc['kalshi_revenue'] / 100:.2f} "
                        f"diff=${disc['difference'] / 100:.2f}",
                        severity="warning",
                    )
            # Update performance panel from cached settlements
            if self._engine.has_settlement_cache:
                from talos.settlement_tracker import aggregate_settlements

                all_rows = self._engine.performance_settlement_rows()
                agg = aggregate_settlements(all_rows)
                self.query_one(PerformancePanel).update_performance(agg)
        except Exception:
            pass  # Non-critical — don't crash for P&L display

    def _log_market_snapshots(self) -> None:
        """Log market snapshots every 10s for ML data collection.

        Collects snapshot data on the main thread (reads engine state),
        then writes to SQLite in a worker thread to avoid blocking the UI.
        """
        if self._engine is None:
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
            gs = (
                self._engine.game_status_resolver.get(opp.event_ticker)
                if self._engine.game_status_resolver
                else None
            )
            snapshots.append(
                {
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
                }
            )
        if snapshots:
            self._engine.log_market_snapshots(snapshots)

    @work(thread=False)
    async def _refresh_volumes(self) -> None:
        if self._engine is not None:
            await self._engine.refresh_volumes()

    @work(thread=False)
    async def _refresh_game_status(self) -> None:
        if self._engine is not None:
            await self._engine.refresh_game_status()

    def mark_event_dirty(self, event_ticker: str) -> None:
        """Mark an event for table refresh on next cycle."""
        self.query_one(OpportunitiesTable).mark_dirty(event_ticker)

    def _update_freshness(self) -> None:
        """Compute orderbook age per market and push to table."""
        if self._engine is None:
            return
        table = self.query_one(OpportunitiesTable)
        table.update_freshness(self._engine.orderbook_ages(now=time.time()))

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
            table.update_leg_labels(self._engine.game_manager.leg_labels)
            table.update_volumes(self._engine.game_manager.volumes_24h)
            table.update_statuses(self._engine.event_statuses)
            # Push portfolio summaries
            panel = self.query_one(PortfolioPanel)
            summaries = self._engine.position_summaries
            total_matched_units = 0
            total_partial_events = 0
            total_locked_bps: float = 0.0
            total_exposure_bps = 0
            with_positions = 0
            bidding = 0

            for s in summaries:
                filled = s.leg_a.filled_count + s.leg_b.filled_count
                resting = s.leg_a.resting_count + s.leg_b.resting_count
                matched = s.matched_pairs

                total_matched_units += matched // s.unit_size if s.unit_size > 0 else 0
                total_locked_bps += s.locked_profit_bps
                total_exposure_bps += s.exposure_bps

                if filled > 0:
                    with_positions += 1
                    if not (
                        matched > 0
                        and matched % s.unit_size == 0
                        and s.leg_a.filled_count == s.leg_b.filled_count
                    ):
                        total_partial_events += 1
                elif resting > 0:
                    bidding += 1

            total_events = len(self._scanner.pairs) if self._scanner else 0
            unentered = total_events - with_positions - bidding

            # PortfolioPanel._locked / _exposure are cents-scale (same unit as
            # _cash); convert bps totals back at this display-layer boundary.
            total_locked = float(bps_to_cents_round(int(total_locked_bps)))
            total_exposure = bps_to_cents_round(total_exposure_bps)
            panel.update_account(
                total_matched_units, total_partial_events, total_locked, total_exposure
            )
            panel.update_coverage(total_events, with_positions, bidding, unentered)
        tracker = self._engine.tracker if self._engine else None
        table.refresh_from_scanner(self._scanner, tracker)
        self._update_freshness()

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        """Forward header clicks to the opportunities table for sorting."""
        self.query_one(OpportunitiesTable).toggle_sort(event.column_index)

    # ── Actions ───────────────────────────────────────────────────

    def action_add_games(self) -> None:
        self._open_add_games()

    @work(thread=False, exclusive=True, group="add_games")
    async def _open_add_games(self) -> None:
        from talos.game_manager import MarketPickerNeeded

        urls = await self.push_screen_wait(AddGamesScreen())
        if urls is not None and self._engine is not None:
            try:
                await self._engine.add_games(urls, source="manual")
            except MarketPickerNeeded as e:
                await self._show_market_picker(e)

    async def _show_market_picker(self, e: object) -> None:
        """Show market picker for multi-market non-sports events."""
        from talos.game_manager import MarketPickerNeeded

        if not isinstance(e, MarketPickerNeeded) or self._engine is None:
            return
        selected = await self.push_screen_wait(
            MarketPickerScreen(e.markets, event_title=e.event.title or e.event.event_ticker)
        )
        if selected:
            pairs = await self._engine.add_market_pairs(e.event, selected)
            if not pairs:
                self.notify(
                    f"Failed to add markets (0/{len(selected)} succeeded)",
                    severity="error",
                )
                return
            # Push fresh labels, volumes + refresh table so new rows appear immediately
            table = self.query_one(OpportunitiesTable)
            gm = self._engine.game_manager
            table.update_labels(gm.labels)
            table.update_leg_labels(gm.leg_labels)
            table.update_volumes(gm.volumes_24h)
            tracker = self._engine.tracker if self._engine else None
            table.refresh_from_scanner(self._scanner, tracker)

    def action_scan(self) -> None:
        if self._engine is not None:
            self._run_scan()

    @work(thread=False, exclusive=True, group="scan")
    async def _run_scan(self) -> None:
        if self._engine is None:
            return
        mode_label = "non-sports" if self._scan_mode == "nonsports" else "sports"
        self.notify(f"Scanning {mode_label} events...", timeout=15)
        import time as _scan_time

        _scan_t0 = _scan_time.monotonic()
        try:
            events = await self._engine.scan_events(scan_mode=self._scan_mode)
        except Exception as e:
            self.notify(f"Scan failed: {e}", severity="error")
            return
        _duration = round((_scan_time.monotonic() - _scan_t0) * 1000)
        if not events:
            self.notify(f"No {mode_label} events found ({_duration}ms)")
            return

        self.notify(f"Found {len(events)} {mode_label} events")
        selected = await self.push_screen_wait(ScanScreen(events))
        selected_tickers = set(selected) if selected else set()

        # Log scan to data collector
        from talos.game_manager import DEFAULT_NONSPORTS_CATEGORIES, SCAN_SERIES

        scan_events = []
        for ev in events:
            prefix = ev.series_ticker or ev.event_ticker.split("-")[0]
            from talos.ui.widgets import _CATEGORY_SHORT, _SPORT_LEAGUE

            sport_league = _SPORT_LEAGUE.get(prefix)
            if sport_league:
                sport, league = sport_league
            else:
                sport = _CATEGORY_SHORT.get(ev.category, ev.category[:4])
                league = prefix.removeprefix("KX")[:5]
            active_mkts = [m for m in ev.markets if m.status == "active"]
            scan_events.append(
                {
                    "event_ticker": ev.event_ticker,
                    "series_ticker": ev.series_ticker,
                    "sport": sport,
                    "league": league,
                    "title": ev.title,
                    "sub_title": ev.sub_title,
                    "volume_a": (
                        (active_mkts[0].volume_24h_fp100 or 0) // ONE_CONTRACT_FP100
                        if len(active_mkts) > 0
                        else 0
                    ),
                    "volume_b": (
                        (active_mkts[1].volume_24h_fp100 or 0) // ONE_CONTRACT_FP100
                        if len(active_mkts) > 1
                        else 0
                    ),
                    "no_bid_a": (
                        (active_mkts[0].no_bid_bps or 0) // ONE_CENT_BPS
                        if len(active_mkts) > 0
                        else 0
                    ),
                    "no_ask_a": (
                        (active_mkts[0].no_ask_bps or 0) // ONE_CENT_BPS
                        if len(active_mkts) > 0
                        else 0
                    ),
                    "no_bid_b": (
                        (active_mkts[1].no_bid_bps or 0) // ONE_CENT_BPS
                        if len(active_mkts) > 1
                        else 0
                    ),
                    "no_ask_b": (
                        (active_mkts[1].no_ask_bps or 0) // ONE_CENT_BPS
                        if len(active_mkts) > 1
                        else 0
                    ),
                    "edge": 0.0,
                    "selected": 1 if ev.event_ticker in selected_tickers else 0,
                }
            )
        self._engine.log_scan(
            events_found=len(events),
            events_eligible=len(events),
            events_selected=len(selected_tickers),
            series_scanned=len(SCAN_SERIES) + (1 if DEFAULT_NONSPORTS_CATEGORIES else 0),
            duration_ms=_duration,
            events=scan_events,
        )

        if selected and self._engine is not None:
            pairs = await self._engine.add_games(selected)
            skipped = len(selected) - len(pairs)
            msg = f"Added {len(pairs)} event(s)"
            if skipped:
                msg += f" ({skipped} skipped — multi-market or failed)"
            self.notify(msg)
            # Refresh table immediately so new rows appear
            table = self.query_one(OpportunitiesTable)
            gm = self._engine.game_manager
            table.update_labels(gm.labels)
            table.update_leg_labels(gm.leg_labels)
            table.update_volumes(gm.volumes_24h)
            tracker = self._engine.tracker if self._engine else None
            table.refresh_from_scanner(self._scanner, tracker)

    def action_review_event(self) -> None:
        """Open the event review panel for the selected row."""
        if self._engine is None:
            return
        table = self.query_one(OpportunitiesTable)
        if table.cursor_row is None or table.row_count == 0:
            return
        cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        event_ticker = _event_ticker_from_row_key(str(cell_key.row_key.value))
        if not event_ticker:
            return
        from talos.persistence import get_data_dir

        base = get_data_dir()
        db_path = base / "talos_data.db"
        log_path = base / "suggestions.log"
        self.push_screen(EventReviewScreen(event_ticker, self._engine, db_path, log_path))

    def action_open_in_browser(self) -> None:
        """Open the highlighted event on Kalshi's website."""
        import webbrowser

        table = self.query_one(OpportunitiesTable)
        if table.cursor_row is None or table.row_count == 0:
            return
        cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        event_ticker = _event_ticker_from_row_key(str(cell_key.row_key.value))
        if not event_ticker:
            return
        # For YES/NO pairs, use the real Kalshi event ticker for the URL
        url_ticker = event_ticker
        if self._engine:
            pair = self._engine.find_pair(event_ticker)
            if pair and pair.api_event_ticker != event_ticker:
                url_ticker = pair.api_event_ticker
        series = url_ticker.split("-")[0].lower()
        url = f"https://kalshi.com/markets/{series}/{url_ticker.lower()}"
        webbrowser.open(url)

    def action_settlement_history(self) -> None:
        if self._engine is not None:
            self._open_settlement_history()

    @work(thread=False, exclusive=True, group="settlement_history")
    async def _open_settlement_history(self) -> None:
        if self._engine is None:
            return
        if self._engine.has_settlement_cache:
            try:
                new_settlements = await self._engine.get_settlements(limit=200)
            except Exception as e:
                self.notify(f"Failed to fetch settlements: {e}", severity="error")
                new_settlements = []
            if new_settlements:
                # Build est_pnl map from live positions
                est_map = {
                    s.event_ticker: bps_to_cents_round(int(s.locked_profit_bps))
                    for s in self._engine.position_summaries
                }
                self._engine.cache_settlements(new_settlements, est_pnl_map=est_map)
            # Read everything from cache
            cached = self._engine.cached_settlement_models()
            settlements = [s for s, _, _ in cached]
            est_pnl_map: dict[str, int] = {}
            subtitles: dict[str, str] = {}
            for s, est, sub in cached:
                if est is not None:
                    est_pnl_map[s.event_ticker] = est
                if sub:
                    subtitles[s.event_ticker] = sub
        else:
            # No cache — fetch directly
            try:
                settlements = await self._engine.get_settlements(limit=200)
            except Exception as e:
                self.notify(f"Failed to fetch settlements: {e}", severity="error")
                return
            est_pnl_map = {}
            subtitles = dict(self._engine.game_manager.subtitles)

        if not settlements:
            self.notify("No settlements found", severity="information")
            return
        await self.push_screen_wait(
            SettlementHistoryScreen(
                settlements,
                position_summaries=self._engine.position_summaries,
                subtitles=subtitles,
                est_pnl_map=est_pnl_map,
            )
        )

    def action_copy_activity_log(self) -> None:
        """Copy activity log contents to clipboard."""
        log = self.query_one(ActivityLog)
        text = log.get_plain_text()
        if text:
            self.copy_to_clipboard(text)
            self.notify("Activity log copied to clipboard")
        else:
            self.notify("Activity log is empty", severity="warning")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self._scanner is None:
            return
        event_ticker = _event_ticker_from_row_key(str(event.row_key.value))
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
                event_ticker = _event_ticker_from_row_key(str(row_key.value))
                self._remove_game(event_ticker)
            except CellDoesNotExist:
                logger.debug("remove_game_no_selection")

    @work(thread=False)
    async def _remove_game(self, event_ticker: str) -> None:
        if self._engine is not None:
            await self._engine.remove_game(event_ticker)

    def action_blacklist_ticker(self) -> None:
        if self._engine is None:
            return
        table = self.query_one(OpportunitiesTable)
        if table.cursor_row is not None:
            try:
                row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
                event_ticker = _event_ticker_from_row_key(str(row_key.value))
                # Blacklist by series prefix (blocks entire series)
                series = self._engine.get_series_for_event(event_ticker)
                entry = series if series else event_ticker.split("-")[0]
                self._blacklist_ticker(entry)
            except CellDoesNotExist:
                logger.debug("blacklist_no_selection")

    @work(thread=False)
    async def _blacklist_ticker(self, entry: str) -> None:
        if self._engine is not None:
            removed = await self._engine.blacklist_ticker(entry)
            self.notify(f"Blacklisted {entry} — removed {len(removed)} game(s)")
            if removed:
                table = self.query_one(OpportunitiesTable)
                tracker = self._engine.tracker if self._engine else None
                table.refresh_from_scanner(self._scanner, tracker)

    def action_edit_blacklist(self) -> None:
        if self._engine is None:
            return

        current = self._engine.game_manager.ticker_blacklist
        self._show_blacklist_editor(current)

    def set_scan_mode(self, mode: str) -> None:
        """Set scan mode (subtitle updated by _refresh_status loop)."""
        self._scan_mode = mode

    def action_toggle_scan_mode(self) -> None:
        """Toggle between sports and non-sports scan mode."""
        new_mode = "nonsports" if self._scan_mode == "sports" else "sports"
        self.set_scan_mode(new_mode)
        if self.on_scan_mode_change:
            self.on_scan_mode_change(new_mode)
        mode_label = "SPORTS" if new_mode == "sports" else "NON-SPORTS"
        self.notify(f"Scan mode: {mode_label}")

    def action_toggle_view(self) -> None:
        """Toggle between full and compact table columns."""
        table = self.query_one(OpportunitiesTable)
        new_compact = not table._compact
        table.set_compact(new_compact)
        mode_label = "compact" if new_compact else "full"
        self.notify(f"View: {mode_label}")

    def action_push_tree_screen(self) -> None:
        """Push the TreeScreen (tree-mode selection surface).

        Tree mode is always on in production. Collaborators are only
        absent in test harnesses that construct the app without a full
        engine; in that case we bail with a warning rather than crash.
        """
        from talos.ui.tree_screen import TreeScreen

        if (
            self._automation_config is None
            or self._discovery_service is None
            or self._milestone_resolver is None
            or self._tree_metadata_store is None
        ):
            self.notify(
                "Tree mode collaborators not initialized.",
                severity="warning",
            )
            return
        # Lazy bootstrap: only start discovery + milestone loop the first
        # time the user opens the tree screen. Prevents startup event-loop
        # block from the 9,700-series /series pull.
        if not getattr(self, "_tree_bootstrap_started", False):
            self._tree_bootstrap_started = True
            self._bootstrap_tree_discovery()
            self._run_tree_milestone_loop()

        screen = TreeScreen(
            discovery=self._discovery_service,
            milestones=self._milestone_resolver,
            metadata=self._tree_metadata_store,
            engine=self._engine,
        )
        # Listener registration moved into TreeScreen.on_mount (round-7
        # plan Step 12a) so it happens AFTER _app_loop is captured.
        # Pre-mount delivery would otherwise hit the defensive log-and-
        # drop path and lose real signals.
        self.push_screen(screen)

    @work(thread=False)
    async def _show_blacklist_editor(self, current: list[str]) -> None:
        from talos.ui.screens import BlacklistScreen

        result = await self.push_screen_wait(BlacklistScreen(current))
        if result is None or self._engine is None:
            return
        removed = await self._engine.replace_blacklist(result)
        msg = f"Blacklist updated — {len(result)} entries"
        if removed:
            msg += f", removed {len(removed)} game(s)"
        self.notify(msg)
        if removed:
            table = self.query_one(OpportunitiesTable)
            tracker = self._engine.tracker if self._engine else None
            table.refresh_from_scanner(self._scanner, tracker)

    def action_approve_proposal(self) -> None:
        if self._engine is None:
            return
        # If proposal panel is open, approve its selected item
        try:
            panel = self.query_one("#proposal-panel", ProposalPanel)
            if panel.display:
                panel.approve_selected()
                return
        except Exception:
            pass
        # Fallback: approve first pending
        pending = self._engine.proposal_queue.pending()
        if pending:
            self._execute_approval(pending[0].key)

    def action_reject_proposal(self) -> None:
        if self._engine is None:
            return
        # If proposal panel is open, reject its selected item
        try:
            panel = self.query_one("#proposal-panel", ProposalPanel)
            if panel.display:
                panel.reject_selected()
                return
        except Exception:
            pass
        # Fallback: reject first pending
        pending = self._engine.proposal_queue.pending()
        if pending:
            self._engine.reject_proposal(pending[0].key)

    def action_set_unit_size(self) -> None:
        if self._engine is not None:
            self._open_unit_size()

    @work(thread=False, exclusive=True, group="unit_size")
    async def _open_unit_size(self) -> None:
        current = self._engine.unit_size if self._engine else DEFAULT_UNIT_SIZE
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
        if self._execution_mode.is_automatic:
            self._end_automatic_session()
        else:
            self._open_auto_accept()

    @work(thread=False, exclusive=True, group="auto_accept")
    async def _open_auto_accept(self) -> None:
        result = await self.push_screen_wait(AutoAcceptScreen())
        if self._engine is None or result is None:
            return
        if result == "indefinite":
            self._enter_automatic_mode(hours=None)
        else:
            self._enter_automatic_mode(hours=result)

    def _enter_automatic_mode(self, hours: float | None = None) -> None:
        """Enter automatic execution mode. hours=None means indefinite."""
        if self._engine is None:
            return

        from talos.persistence import get_data_dir

        self._execution_mode.enter_automatic(hours=hours)

        log_dir = get_data_dir() / "auto_accept_sessions"
        aa_logger = AutoAcceptLogger(log_dir)
        self._auto_accept_logger = aa_logger

        cfg = self._engine.automation_config
        config: dict[str, object] = {
            "edge_threshold_cents": cfg.edge_threshold_cents,
            "stability_seconds": cfg.stability_seconds,
            "unit_size": self._engine.unit_size,
        }
        aa_logger.log_session_start(self._execution_mode, config)

        label = "Automatic mode ON" + (f" — {hours:.1f}h" if hours else " — indefinite")
        self.notify(label, severity="warning", markup=False)
        logger.info("execution_mode_automatic", hours=hours)

    def _end_automatic_session(self) -> None:
        """End automatic session: log final state, switch to manual."""
        count = self._execution_mode.accepted_count
        elapsed = self._execution_mode.elapsed_str()

        if self._auto_accept_logger and self._engine:
            final_positions: dict[str, object] = {}
            for s in self._engine.position_summaries:
                final_positions[s.event_ticker] = {
                    "status": s.status,
                    "leg_a_filled": s.leg_a.filled_count,
                    "leg_b_filled": s.leg_b.filled_count,
                }
            self._auto_accept_logger.log_session_end(self._execution_mode, final_positions)

        self._execution_mode.enter_manual()
        self._auto_accept_logger = None

        self.notify(
            f"Manual mode — {count} accepted in {elapsed}",
            severity="information",
            markup=False,
        )
        logger.info("execution_mode_manual", accepted_count=count, elapsed=elapsed)

    def action_toggle_exit_only(self) -> None:
        """Toggle exit-only mode on the highlighted event."""
        if self._engine is None:
            return
        table = self.query_one(OpportunitiesTable)
        if table.cursor_row is None or table.row_count == 0:
            return
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            event_ticker = _event_ticker_from_row_key(str(cell_key.row_key.value))
        except CellDoesNotExist:
            return
        if not event_ticker:
            return
        self._toggle_exit_only(event_ticker)

    @work(thread=False)
    async def _toggle_exit_only(self, event_ticker: str) -> None:
        if self._engine is not None:
            is_on = self._engine.toggle_exit_only(event_ticker)
            if is_on:
                await self._engine.enforce_exit_only(event_ticker)

    def action_exit_all(self) -> None:
        """Put ALL monitored games into exit-only mode."""
        if self._engine is None:
            return
        self._exit_all_games()

    @work(thread=False)
    async def _exit_all_games(self) -> None:
        if self._engine is not None:
            await self._engine.exit_all()

    def action_clear_games(self) -> None:
        if self._engine is not None:
            self._clear_all_games()

    @work(thread=False)
    async def _clear_all_games(self) -> None:
        if self._engine is not None:
            await self._engine.clear_games()
