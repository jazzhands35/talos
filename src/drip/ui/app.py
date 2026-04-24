"""DripApp — Textual TUI for a single Drip arbitrage run (v2: WS-first).

Architecture:
- WS drives fills, order lifecycle, and orderbook jumps in real time.
- REST is used for startup hydration, reconnect recovery, periodic
  30s reconcile, and actual place/cancel execution.
- A serialized action queue prevents overlapping REST calls.
- RuntimeState tracks sync state (HYDRATING / LIVE / RECONNECTING)
  and gates the executor so no trading happens when disconnected.
"""

from __future__ import annotations

import asyncio

import structlog
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header

from drip.config import DripConfig
from drip.controller import Action, CancelOrder, DripController, NoOp, PlaceOrder
from drip.runtime_state import RuntimeState, SyncState
from drip.ui.theme import APP_CSS
from drip.ui.widgets import ActionLog, BalancePanel, SidePanel
from drip.ws_runtime import DripWSRuntime
from talos.auth import KalshiAuth
from talos.config import KalshiConfig
from talos.models.ws import (
    FillMessage,
    OrderBookDelta,
    OrderBookSnapshot,
    UserOrderMessage,
)
from talos.rest_client import KalshiRESTClient

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_UI_REFRESH_INTERVAL = 1.0  # seconds between UI refreshes
_RECONCILE_INTERVAL = 30.0  # seconds between full reconcile (repair path)


class DripApp(App):
    """Single-event Drip arbitrage manager — WebSocket-first architecture."""

    CSS = APP_CSS
    TITLE = "DRIP"
    BINDINGS = [
        ("w", "wind_down", "Wind Down"),
        ("l", "copy_log", "Copy Log"),
        ("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        config: DripConfig,
        rest_client: KalshiRESTClient,
        auth: KalshiAuth,
        kalshi_config: KalshiConfig,
    ) -> None:
        super().__init__()
        self._config = config
        self._rest = rest_client
        self._auth = auth
        self._kalshi_config = kalshi_config
        self._controller = DripController(config)
        self._runtime = RuntimeState()
        self._action_queue: asyncio.Queue[Action] = asyncio.Queue()
        self._winding_down = False
        self._ws_runtime: DripWSRuntime | None = None

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="top-panels"):
            yield SidePanel("Side A", id="side-a-panel")
            yield SidePanel("Side B", id="side-b-panel")
            yield BalancePanel(id="balance-panel")
        yield ActionLog(id="action-log")
        yield Footer()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        """Start the boot sequence."""
        self.title = f"DRIP \u2014 {self._config.event_ticker}"
        self._log("Starting Drip for " + self._config.event_ticker)
        self._log(
            f"A={self._config.ticker_a} @ {self._config.price_a}\u00a2, "
            f"B={self._config.ticker_b} @ {self._config.price_b}\u00a2"
        )
        self._boot()

    @work(thread=False)
    async def _boot(self) -> None:
        """Startup: hydrate from REST, create WS runtime, start workers."""
        try:
            await self._hydrate_from_kalshi()
        except Exception as exc:
            self._log(f"Hydration failed: {exc}", severity="error")
            return

        self._ws_runtime = DripWSRuntime(
            auth=self._auth,
            config=self._kalshi_config,
            tickers=[self._config.ticker_a, self._config.ticker_b],
            on_fill=self._on_fill,
            on_user_order=self._on_user_order,
            on_orderbook_snapshot=self._on_orderbook_snapshot,
            on_orderbook_delta=self._on_orderbook_delta,
            on_connect=self._on_ws_connect,
            on_disconnect=self._on_ws_disconnect,
        )

        # Start long-running background workers
        self._run_ws()
        self._run_executor()

        # Start periodic timers
        self.set_interval(_UI_REFRESH_INTERVAL, self._refresh_ui)
        self.set_interval(_RECONCILE_INTERVAL, self._poll_reconcile)
        self.set_interval(self._config.stagger_delay, self._deploy_tick)

    # ------------------------------------------------------------------
    # Hydration / reconciliation
    # ------------------------------------------------------------------

    async def _hydrate_from_kalshi(self) -> None:
        """Fetch all event orders and rebuild controller state from Kalshi truth."""
        self._log("Hydrating from Kalshi\u2026")
        all_orders = await self._rest.get_all_orders(
            event_ticker=self._config.event_ticker,
        )

        resting_a: list[tuple[str, int]] = []
        resting_b: list[tuple[str, int]] = []
        filled_a = 0
        filled_b = 0

        for order in all_orders:
            if order.side != "no" or order.action != "buy":
                continue
            if order.ticker == self._config.ticker_a:
                if order.status == "resting":
                    resting_a.append((order.order_id, order.no_price_bps // 100))
                filled_a += order.fill_count_fp100 // 100
            elif order.ticker == self._config.ticker_b:
                if order.status == "resting":
                    resting_b.append((order.order_id, order.no_price_bps // 100))
                filled_b += order.fill_count_fp100 // 100

        # Log pending state that reconcile will discard
        stale_places = len(self._runtime.side_a.pending_placements) + len(
            self._runtime.side_b.pending_placements
        )
        stale_cancels = len(self._runtime.side_a.pending_cancel_ids) + len(
            self._runtime.side_b.pending_cancel_ids
        )

        self._controller.reconcile(resting_a, resting_b, filled_a, filled_b)

        # Reconcile is authoritative — clear all pending state
        for side_rt in (self._runtime.side_a, self._runtime.side_b):
            side_rt.pending_placements.clear()
            side_rt.pending_cancel_ids.clear()

        msg = f"Hydrated: A={filled_a}f/{len(resting_a)}r, B={filled_b}f/{len(resting_b)}r"
        if stale_places or stale_cancels:
            msg += f" (cleared {stale_places}p/{stale_cancels}c pending)"
        self._log(msg)
        logger.info(
            "hydration_complete",
            filled_a=filled_a,
            filled_b=filled_b,
            resting_a=len(resting_a),
            resting_b=len(resting_b),
            stale_pending_placements=stale_places,
            stale_pending_cancels=stale_cancels,
        )

    # ------------------------------------------------------------------
    # WS lifecycle callbacks
    # ------------------------------------------------------------------

    async def _on_ws_connect(self) -> None:
        """Called after WS connects (including reconnects).

        Rehydrates from REST before the WS runtime subscribes to channels.
        This gives a sane baseline but does NOT guarantee zero event gaps —
        events between the REST snapshot and subscription start can be missed.
        The 30s periodic reconcile is what actually closes any remaining gap.
        """
        try:
            await self._hydrate_from_kalshi()
        except Exception as exc:
            self._log(f"Reconnect hydration failed: {exc}", severity="error")
        self._runtime.sync_state = SyncState.LIVE
        self._log("WS connected \u2014 LIVE")

    async def _on_ws_disconnect(self) -> None:
        """Called when WS disconnects. Gates executor until reconnect."""
        pending_places = len(self._runtime.side_a.pending_placements) + len(
            self._runtime.side_b.pending_placements
        )
        pending_cancels = len(self._runtime.side_a.pending_cancel_ids) + len(
            self._runtime.side_b.pending_cancel_ids
        )
        self._runtime.sync_state = SyncState.RECONNECTING
        msg = "WS disconnected \u2014 RECONNECTING"
        if pending_places or pending_cancels:
            msg += f" ({pending_places}p/{pending_cancels}c in flight)"
        self._log(msg, severity="warning")

    # ------------------------------------------------------------------
    # WS event handlers
    # ------------------------------------------------------------------

    async def _on_fill(self, msg: FillMessage) -> None:
        """Fill event from WS — drive controller imbalance logic."""
        side = self._ticker_to_side(msg.market_ticker)
        if side is None:
            return
        self._runtime.touch_ws()
        self._log(f"Fill {side}: {msg.order_id[:8]}\u2026 @ {msg.no_price}\u00a2")
        actions = self._controller.on_fill(side, msg.order_id)
        for action in actions:
            await self._action_queue.put(action)

    async def _on_user_order(self, msg: UserOrderMessage) -> None:
        """Order lifecycle from WS — the acknowledged-state confirmation path.

        This is where controller state actually changes:
        - "resting": placement confirmed → add to controller
        - "canceled": cancel confirmed → remove from controller
        - "executed": fully filled → clear pending (fill handler drove state)
        """
        side = self._ticker_to_side(msg.ticker)
        if side is None:
            return
        self._runtime.touch_ws()
        rt = self._runtime.get_side(side)

        if msg.status == "resting":
            # Placement confirmed by exchange — now safe to add to controller
            price = rt.pending_placements.pop(msg.order_id, None)
            if price is not None:
                self._controller._side(side).add_order(msg.order_id, price)
                logger.debug(
                    "placement_confirmed",
                    side=side,
                    order_id=msg.order_id,
                    price=price,
                )
        elif msg.status == "canceled":
            # Cancel confirmed by exchange — now safe to remove from controller
            rt.pending_cancel_ids.discard(msg.order_id)
            rt.pending_placements.pop(msg.order_id, None)
            self._controller._side(side).remove_order(msg.order_id)
            logger.debug(
                "cancel_confirmed",
                side=side,
                order_id=msg.order_id,
            )
        elif msg.status == "executed":
            # Fully filled — fill channel already drove on_fill / record_fill.
            # Just clean up any pending state.
            rt.pending_cancel_ids.discard(msg.order_id)
            rt.pending_placements.pop(msg.order_id, None)
            logger.debug(
                "execution_confirmed",
                side=side,
                order_id=msg.order_id,
            )
        else:
            logger.debug(
                "user_order_ws",
                side=side,
                order_id=msg.order_id,
                status=msg.status,
            )

    async def _on_orderbook_snapshot(self, msg: OrderBookSnapshot) -> None:
        """Orderbook snapshot from WS — initialize local book for a ticker."""
        side = self._ticker_to_side(msg.market_ticker)
        if side is None:
            return
        self._runtime.touch_ws()
        rt = self._runtime.get_side(side)
        rt.book.apply_snapshot(msg.no)
        await self._check_price_jump(side)

    async def _on_orderbook_delta(self, msg: OrderBookDelta) -> None:
        """Orderbook delta from WS — update local book, detect price jumps."""
        side = self._ticker_to_side(msg.market_ticker)
        if side is None or msg.side != "no":
            return
        self._runtime.touch_ws()
        rt = self._runtime.get_side(side)
        rt.book.apply_delta(msg.price, msg.delta)
        await self._check_price_jump(side)

    async def _check_price_jump(self, side: str) -> None:
        """Compare best NO price against last known; fire on_jump if changed."""
        if self._winding_down:
            return
        rt = self._runtime.get_side(side)
        best = rt.book.best_price
        if best is not None and rt.last_best_no is not None and best != rt.last_best_no:
            self._log(
                f"Jump {side}: {rt.last_best_no}\u00a2 \u2192 {best}\u00a2",
                severity="warning",
            )
            actions = self._controller.on_jump(side, best)
            for action in actions:
                await self._action_queue.put(action)
        rt.last_best_no = best

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ticker_to_side(self, ticker: str) -> str | None:
        """Map a market ticker to its Drip side label."""
        if ticker == self._config.ticker_a:
            return "A"
        if ticker == self._config.ticker_b:
            return "B"
        return None

    def _log(self, message: str, severity: str = "information") -> None:
        """Log to both the UI action log and structlog."""
        try:
            log_widget = self.query_one("#action-log", ActionLog)
            log_widget.log_action(message, severity)
        except Exception:
            pass  # Widget not yet mounted
        logger.info("drip_action", message=message, severity=severity)

    # ------------------------------------------------------------------
    # Serialized action executor
    # ------------------------------------------------------------------

    @work(thread=False)
    async def _run_executor(self) -> None:
        """Drain the action queue and execute REST calls serially.

        Gated by sync state: actions are dropped when not LIVE,
        except wind-down cancels which always execute.
        """
        while True:
            action = await self._action_queue.get()
            try:
                if isinstance(action, NoOp):
                    logger.debug("noop", reason=action.reason)
                    continue
                # Gate: only execute when LIVE (wind-down cancels bypass)
                if self._runtime.sync_state != SyncState.LIVE and not (
                    self._winding_down and isinstance(action, CancelOrder)
                ):
                    logger.debug(
                        "action_skipped",
                        action=type(action).__name__,
                        sync_state=self._runtime.sync_state.value,
                    )
                    continue
                await self._execute_single(action)
            finally:
                self._action_queue.task_done()

    async def _execute_single(self, action: Action) -> None:
        """Execute one PlaceOrder or CancelOrder against the Kalshi REST API.

        Controller state is NOT mutated here.  REST success only creates
        pending state in RuntimeState.  The actual controller mutation
        happens when user_orders WS confirms the lifecycle transition.
        """
        if isinstance(action, PlaceOrder):
            ticker = self._config.ticker_a if action.side == "A" else self._config.ticker_b
            try:
                order = await self._rest.create_order(
                    ticker=ticker,
                    action="buy",
                    side="no",
                    no_price=action.price,
                    count=1,
                )
                # Track as pending — controller.add_order waits for user_orders
                rt = self._runtime.get_side(action.side)
                rt.pending_placements[order.order_id] = action.price
                self._log(f"Placed {action.side} @ {action.price}\u00a2 (pending)")
            except Exception as exc:
                self._log(
                    f"FAILED place {action.side} @ {action.price}\u00a2: {exc}",
                    severity="error",
                )
        elif isinstance(action, CancelOrder):
            rt = self._runtime.get_side(action.side)
            if action.order_id in rt.pending_cancel_ids:
                logger.debug("cancel_already_pending", order_id=action.order_id)
                return
            rt.pending_cancel_ids.add(action.order_id)
            try:
                await self._rest.cancel_order(action.order_id)
                # Track as pending — controller.remove_order waits for user_orders
                self._log(f"Cancel {action.side} ({action.reason}) (pending)")
            except Exception as exc:
                # Cancel REST failed — allow retry
                rt.pending_cancel_ids.discard(action.order_id)
                self._log(
                    f"FAILED cancel {action.side} {action.order_id}: {exc}",
                    severity="error",
                )

    # ------------------------------------------------------------------
    # Deployment tick
    # ------------------------------------------------------------------

    @work(thread=False, exclusive=True, group="deploy")
    async def _deploy_tick(self) -> None:
        """Deploy the next contract during the stagger phase."""
        if self._winding_down:
            return
        if self._runtime.sync_state != SyncState.LIVE:
            return
        if not self._controller.side_a.deploying and not self._controller.side_b.deploying:
            return
        actions = self._controller.deploy_next()
        for action in actions:
            await self._action_queue.put(action)

    # ------------------------------------------------------------------
    # Periodic reconciliation (REST, every 30s — repair path only)
    # ------------------------------------------------------------------

    @work(thread=False, exclusive=True, group="reconcile")
    async def _poll_reconcile(self) -> None:
        """Full state reconciliation from Kalshi (repair path).

        Runs only when LIVE.  Fixes any drift from missed WS events.
        """
        if self._runtime.sync_state != SyncState.LIVE:
            return
        try:
            await self._hydrate_from_kalshi()
        except Exception as exc:
            self._log(f"Reconcile error: {exc}", severity="error")

    # ------------------------------------------------------------------
    # WS background worker
    # ------------------------------------------------------------------

    @work(thread=False)
    async def _run_ws(self) -> None:
        """Run the WS runtime (blocks until stopped)."""
        if self._ws_runtime:
            await self._ws_runtime.start()

    # ------------------------------------------------------------------
    # Key bindings
    # ------------------------------------------------------------------

    async def action_wind_down(self) -> None:
        """Cancel all resting orders and stop deploying."""
        if self._winding_down:
            self._log("Already winding down", severity="warning")
            return
        self._winding_down = True
        self._log(
            "WIND DOWN initiated \u2014 cancelling all resting orders",
            severity="warning",
        )
        actions = self._controller.on_wind_down()
        for action in actions:
            await self._action_queue.put(action)

    def action_copy_log(self) -> None:
        """Copy the action log to clipboard."""
        log_widget = self.query_one("#action-log", ActionLog)
        text = log_widget.get_plain_text()
        if text:
            self.copy_to_clipboard(text)
            self.notify("Action log copied to clipboard")
        else:
            self.notify("Nothing to copy", severity="warning")

    # ------------------------------------------------------------------
    # UI refresh (every 1s)
    # ------------------------------------------------------------------

    def _refresh_ui(self) -> None:
        """Update all UI widgets from controller state + sync state."""
        # Show sync state in title bar
        state = self._runtime.sync_state.value.upper()
        self.title = f"DRIP \u2014 {self._config.event_ticker} [{state}]"

        try:
            side_a_panel = self.query_one("#side-a-panel", SidePanel)
            side_a_panel.update_from_side("Side A", self._controller.side_a)
        except Exception:
            pass

        try:
            side_b_panel = self.query_one("#side-b-panel", SidePanel)
            side_b_panel.update_from_side("Side B", self._controller.side_b)
        except Exception:
            pass

        try:
            balance_panel = self.query_one("#balance-panel", BalancePanel)
            balance_panel.update_from_controller(self._controller)
        except Exception:
            pass
