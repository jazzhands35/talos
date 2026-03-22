"""TradingEngine — central orchestrator for trading logic.

Owns all subsystem dependencies, mutable caches, and polling/action methods.
The TUI delegates to this engine rather than managing trading state directly.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from talos.data_collector import DataCollector
    from talos.settlement_tracker import SettlementCache

from talos.automation_config import AutomationConfig
from talos.bid_adjuster import BidAdjuster
from talos.cpm import CPMTracker
from talos.errors import KalshiAPIError, KalshiRateLimitError
from talos.fees import MAKER_FEE_RATE
from talos.game_manager import GameManager
from talos.game_status import GameStatusResolver
from talos.lifecycle_feed import LifecycleFeed
from talos.market_feed import MarketFeed
from talos.models.order import Order
from talos.models.portfolio import EventPosition, Position
from talos.models.position import EventPositionSummary
from talos.models.proposal import Proposal, ProposalKey
from talos.models.ws import FillMessage, TickerMessage, UserOrderMessage
from talos.opportunity_proposer import OpportunityProposer
from talos.portfolio_feed import PortfolioFeed
from talos.position_feed import PositionFeed
from talos.position_ledger import PositionLedger, Side, compute_display_positions
from talos.proposal_queue import ProposalQueue
from talos.rebalance import (
    _create_order_group,
    compute_overcommit_reduction,
    compute_rebalance_proposal,
    compute_topup_needs,
)
from talos.rebalance import execute_rebalance as _execute_rebalance
from talos.rest_client import KalshiRESTClient
from talos.scanner import ArbitrageScanner
from talos.ticker_feed import TickerFeed
from talos.top_of_market import TopOfMarketTracker

if TYPE_CHECKING:
    from talos.models.strategy import ArbPair, BidConfirmation

logger = structlog.get_logger()


def _merge_queue(existing: int | None, incoming: int) -> int:
    """Conservative queue position merge — keep smallest non-negative value.

    Queue position 0 = front of queue (0 preceding shares). Negative values
    are defensive guards only — the API should never return them.
    """
    if existing is None:
        return incoming
    if incoming < 0:
        return existing
    if existing < 0:
        return incoming
    return min(existing, incoming)


class TradingEngine:
    """Central orchestrator for all trading logic.

    Owns subsystem references, caches, and polling/action methods.
    Proposals flow through ProposalQueue for operator approval.
    """

    def __init__(
        self,
        *,
        scanner: ArbitrageScanner,
        game_manager: GameManager,
        rest_client: KalshiRESTClient,
        market_feed: MarketFeed,
        tracker: TopOfMarketTracker,
        adjuster: BidAdjuster,
        initial_games: list[str] | None = None,
        initial_games_full: list[dict] | None = None,
        proposal_queue: ProposalQueue | None = None,
        automation_config: AutomationConfig | None = None,
        portfolio_feed: PortfolioFeed | None = None,
        ticker_feed: TickerFeed | None = None,
        lifecycle_feed: LifecycleFeed | None = None,
        position_feed: PositionFeed | None = None,
        game_status_resolver: GameStatusResolver | None = None,
        data_collector: DataCollector | None = None,
        settlement_cache: SettlementCache | None = None,
    ) -> None:
        self._scanner = scanner
        self._game_manager = game_manager
        self._rest = rest_client
        self._feed = market_feed
        self._tracker = tracker
        self._adjuster = adjuster
        self._initial_games = list(initial_games or [])
        self._initial_games_full = initial_games_full
        self._proposal_queue = proposal_queue or ProposalQueue()
        self._auto_config = automation_config or AutomationConfig()
        self._portfolio_feed = portfolio_feed
        self._ticker_feed = ticker_feed
        self._lifecycle_feed = lifecycle_feed
        self._game_status_resolver = game_status_resolver
        self._data_collector = data_collector
        self._settlement_cache = settlement_cache
        self._position_feed = position_feed
        self._proposer = OpportunityProposer(self._auto_config)

        # Mutable caches
        self._queue_cache: dict[str, int] = {}
        self._orders_cache: list[Order] = []
        self._cpm = CPMTracker()
        self._balance: int = 0
        self._portfolio_value: int = 0
        self._position_summaries: list[EventPositionSummary] = []
        self._order_data: list[dict[str, object]] = []
        self._event_positions: dict[str, EventPosition] = {}
        self._paused_markets: set[str] = set()
        self._ws_connected: bool = False
        self._settled_markets: dict[str, dict[str, str]] = {}  # event_ticker -> {ticker: result}
        self._order_placed_at: dict[str, float] = {}  # order_id -> monotonic timestamp
        self._exit_only_events: set[str] = set()  # events in exit-only mode
        self._game_started_events: set[str] = set()  # events where game is live/final
        self._last_jump_eval: dict[str, tuple[int, int]] = {}  # ticker -> (book_top, resting)
        self._stale_candidates: set[str] = set()  # two-strike stale position cleanup
        self._initial_sync_done: bool = False  # gate bids until first refresh_account
        self._pair_index: dict[str, ArbPair] = {}  # rebuilt in _recompute_positions
        self._ticker_to_event: dict[str, str] = {}  # rebuilt in _recompute_positions
        self._pending_kinds_cache: dict[str, set[str]] = {}  # rebuilt in _recompute_positions

        # Wire portfolio feed callbacks
        if self._portfolio_feed is not None:
            self._portfolio_feed.on_order_update = self._on_order_update
            self._portfolio_feed.on_fill = self._on_fill

        # Wire lifecycle feed callbacks
        if self._lifecycle_feed is not None:
            self._lifecycle_feed.on_determined = self._on_market_determined
            self._lifecycle_feed.on_settled = self._on_market_settled
            self._lifecycle_feed.on_paused = self._on_market_paused

        # Callbacks for UI communication and persistence
        self.on_notification: Callable[[str, str, bool], None] | None = None
        self.on_unit_size_change: Callable[[int], None] | None = None

    # ── Read-only properties ─────────────────────────────────────────

    @property
    def scanner(self) -> ArbitrageScanner:
        return self._scanner

    @property
    def tracker(self) -> TopOfMarketTracker:
        return self._tracker

    @property
    def adjuster(self) -> BidAdjuster:
        return self._adjuster

    @property
    def game_manager(self) -> GameManager:
        return self._game_manager

    @property
    def ws_connected(self) -> bool:
        return self._ws_connected

    @property
    def game_status_resolver(self) -> GameStatusResolver | None:
        return self._game_status_resolver

    @property
    def proposal_queue(self) -> ProposalQueue:
        return self._proposal_queue

    @property
    def automation_config(self) -> AutomationConfig:
        return self._auto_config

    @property
    def orders(self) -> list[Order]:
        return self._orders_cache

    @property
    def order_data(self) -> list[dict[str, object]]:
        return self._order_data

    @property
    def position_summaries(self) -> list[EventPositionSummary]:
        return self._position_summaries

    @property
    def event_statuses(self) -> dict[str, str]:
        """Event ticker -> status string for ALL monitored events."""
        return getattr(self, "_event_statuses", {})

    @property
    def balance(self) -> int:
        return self._balance

    @property
    def portfolio_value(self) -> int:
        return self._portfolio_value

    def _display_name(self, event_ticker: str) -> str:
        """Resolve event ticker to short human-readable label (e.g. 'Gorgodze-Kalinina')."""
        return self._game_manager.labels.get(event_ticker, event_ticker)

    @property
    def event_positions(self) -> dict[str, EventPosition]:
        """Rich event-level position data from Kalshi."""
        return self._event_positions

    @property
    def unit_size(self) -> int:
        return self._adjuster._unit_size

    def set_unit_size(self, size: int) -> None:
        """Update unit size across adjuster and all existing ledgers."""
        self._adjuster.set_unit_size(size)
        logger.info("unit_size_changed", unit_size=size)
        if self.on_unit_size_change is not None:
            self.on_unit_size_change(size)

    # ── Exit-only mode ────────────────────────────────────────────

    def is_exit_only(self, event_ticker: str) -> bool:
        return event_ticker in self._exit_only_events

    def toggle_exit_only(self, event_ticker: str) -> bool:
        """Toggle exit-only mode for an event. Returns new state."""
        if event_ticker in self._exit_only_events:
            self._exit_only_events.discard(event_ticker)
            name = self._display_name(event_ticker)
            self._notify(f"Exit-only OFF: {name}")
            logger.info("exit_only_off", event_ticker=event_ticker)
            return False
        else:
            self._exit_only_events.add(event_ticker)
            name = self._display_name(event_ticker)
            self._notify(f"Exit-only ON: {name}", "warning")
            logger.info("exit_only_on", event_ticker=event_ticker)
            self._enforce_exit_only_sync(event_ticker)
            return True

    async def exit_all(self) -> int:
        """Put ALL monitored games into exit-only mode. Returns count changed."""
        count = 0
        for pair in list(self._scanner.pairs):
            if pair.event_ticker not in self._exit_only_events:
                # Set directly instead of toggle_exit_only to avoid per-game toasts
                self._exit_only_events.add(pair.event_ticker)
                self._enforce_exit_only_sync(pair.event_ticker)
                await self._enforce_exit_only(pair.event_ticker)
                count += 1
        if count > 0:
            logger.info("exit_all", count=count)
            self._notify(f"Exit-only ON for {count} game(s)", "warning", toast=True)
        else:
            self._notify("All games already in exit-only mode", toast=True)
        return count

    def _enforce_exit_only_sync(self, event_ticker: str) -> None:
        """Synchronous part of exit-only enforcement — expire proposals."""
        # Expire any pending bid proposals for this event
        for proposal in list(self._proposal_queue.pending()):
            if proposal.key.event_ticker == event_ticker and proposal.kind == "bid":
                self._proposal_queue.reject(proposal.key)

    async def _enforce_exit_only(self, event_ticker: str) -> None:
        """Cancel resting orders per exit-only rules.

        Balanced → cancel all resting on both sides.
        Imbalanced → cancel ahead side resting, reduce behind side
        resting so it can only fill up to match ahead's fill count.
        """
        try:
            ledger = self._adjuster.get_ledger(event_ticker)
        except KeyError:
            return

        filled_a = ledger.filled_count(Side.A)
        filled_b = ledger.filled_count(Side.B)
        game_started = event_ticker in self._game_started_events

        if filled_a == filled_b:
            # Balanced — cancel everything on both sides
            reason = "game_started" if game_started else "balanced"
            for side in (Side.A, Side.B):
                order_id = ledger.resting_order_id(side)
                if order_id is not None:
                    try:
                        await self._rest.cancel_order(order_id)
                        logger.info(
                            "exit_only_cancel",
                            event_ticker=event_ticker,
                            side=side.value,
                            order_id=order_id,
                            reason=reason,
                        )
                    except Exception:
                        logger.warning(
                            "exit_only_cancel_failed",
                            event_ticker=event_ticker,
                            side=side.value,
                            exc_info=True,
                        )
        else:
            # Imbalanced — cancel the ahead side, reduce behind side
            ahead = Side.A if filled_a > filled_b else Side.B
            behind = ahead.other
            order_id = ledger.resting_order_id(ahead)
            if order_id is not None:
                try:
                    await self._rest.cancel_order(order_id)
                    logger.info(
                        "exit_only_cancel",
                        event_ticker=event_ticker,
                        side=ahead.value,
                        order_id=order_id,
                        reason="ahead_side",
                    )
                except Exception:
                    logger.warning(
                        "exit_only_cancel_failed",
                        event_ticker=event_ticker,
                        side=ahead.value,
                        exc_info=True,
                    )

            # Reduce behind side resting so it can't overshoot ahead's fills
            behind_order_id = ledger.resting_order_id(behind)
            if behind_order_id is not None:
                ahead_filled = ledger.filled_count(ahead)
                behind_filled = ledger.filled_count(behind)
                target_behind_resting = ahead_filled - behind_filled
                current_behind_resting = ledger.resting_count(behind)
                if target_behind_resting <= 0:
                    # Behind already has more fills — cancel all resting
                    try:
                        await self._rest.cancel_order(behind_order_id)
                        logger.info(
                            "exit_only_cancel",
                            event_ticker=event_ticker,
                            side=behind.value,
                            order_id=behind_order_id,
                            reason="behind_overshoot",
                        )
                    except Exception:
                        logger.warning(
                            "exit_only_cancel_failed",
                            event_ticker=event_ticker,
                            side=behind.value,
                            exc_info=True,
                        )
                elif current_behind_resting > target_behind_resting:
                    try:
                        await self._rest.decrease_order(
                            behind_order_id,
                            reduce_to=target_behind_resting,
                        )
                        logger.info(
                            "exit_only_reduce_behind",
                            event_ticker=event_ticker,
                            side=behind.value,
                            order_id=behind_order_id,
                            from_resting=current_behind_resting,
                            to_resting=target_behind_resting,
                        )
                    except Exception:
                        logger.warning(
                            "exit_only_reduce_behind_failed",
                            event_ticker=event_ticker,
                            side=behind.value,
                            exc_info=True,
                        )

        await self._verify_after_action(event_ticker)

    def _check_exit_only(self) -> None:
        """Auto-trigger exit-only based on game status, auto-remove when done.

        Called from _recompute_positions (runs every refresh cycle).
        """
        if self._game_status_resolver is None:
            return

        exit_minutes = self._auto_config.exit_only_minutes
        now = datetime.now(UTC)

        for pair in self._scanner.pairs:
            event_ticker = pair.event_ticker

            if event_ticker not in self._exit_only_events:
                # Check if we should auto-activate
                gs = self._game_status_resolver.get(event_ticker)
                if gs is None:
                    continue

                if gs.state == "live":
                    self._exit_only_events.add(event_ticker)
                    self._game_started_events.add(event_ticker)
                    self._enforce_exit_only_sync(event_ticker)
                    name = self._display_name(event_ticker)
                    self._notify(
                        f"GAME LIVE: {name} — cancelling all resting",
                        "error",
                        toast=True,
                    )
                    logger.info(
                        "exit_only_auto_trigger",
                        event_ticker=event_ticker,
                        reason="live",
                        cancel_all=True,
                    )
                elif gs.state == "post":
                    self._exit_only_events.add(event_ticker)
                    self._game_started_events.add(event_ticker)
                    self._enforce_exit_only_sync(event_ticker)
                    name = self._display_name(event_ticker)
                    self._notify(
                        f"GAME FINAL: {name} — cancelling all resting",
                        "error",
                        toast=True,
                    )
                    logger.info(
                        "exit_only_auto_trigger",
                        event_ticker=event_ticker,
                        reason="final",
                        cancel_all=True,
                    )
                elif (
                    gs.state == "pre"
                    and gs.scheduled_start is not None
                    and (gs.scheduled_start - now).total_seconds() < exit_minutes * 60
                ):
                    self._exit_only_events.add(event_ticker)
                    self._enforce_exit_only_sync(event_ticker)
                    name = self._display_name(event_ticker)
                    mins = (gs.scheduled_start - now).total_seconds() / 60
                    self._notify(
                        f"Exit-only AUTO: {name} ({mins:.0f}m to start)",
                        "warning",
                    )
                    logger.info(
                        "exit_only_auto_trigger",
                        event_ticker=event_ticker,
                        reason="approaching_start",
                        minutes_to_start=mins,
                    )

    async def _enforce_all_exit_only(self) -> None:
        """Enforce exit-only rules on all flagged events. Called from refresh cycle."""
        for event_ticker in list(self._exit_only_events):
            # Check if balanced + no resting → auto-remove
            try:
                ledger = self._adjuster.get_ledger(event_ticker)
            except KeyError:
                self._exit_only_events.discard(event_ticker)
                continue

            filled_a = ledger.filled_count(Side.A)
            filled_b = ledger.filled_count(Side.B)
            resting_a = ledger.resting_count(Side.A)
            resting_b = ledger.resting_count(Side.B)

            if filled_a == filled_b and resting_a == 0 and resting_b == 0:
                # Balanced and no resting → auto-remove game
                name = self._display_name(event_ticker)
                self._notify(f"Exit-only DONE: {name} — removing")
                logger.info(
                    "exit_only_auto_remove",
                    event_ticker=event_ticker,
                    filled=filled_a,
                )
                self._exit_only_events.discard(event_ticker)
                self._game_started_events.discard(event_ticker)
                await self.remove_game(event_ticker)
                continue

            # Still has resting on wrong side — enforce
            await self._enforce_exit_only(event_ticker)

    def get_ticker_data(self, ticker: str) -> TickerMessage | None:
        """Return the latest WS ticker data for a market, or None."""
        if self._ticker_feed is None:
            return None
        return self._ticker_feed.get_ticker(ticker)

    # ── Polling methods ─────────────────────────────────────────────

    async def start_feed(self) -> None:
        """Connect WebSocket, restore saved games, and listen.

        Reconnects automatically on disconnect with a non-recursive loop.
        """
        first_connect = True
        while True:
            try:
                await self._feed.connect()
                self._ws_connected = True
                self._notify("WebSocket connected")

                if first_connect:
                    await self._setup_initial_games()
                    first_connect = False

                # Subscribe to portfolio events globally (all markets)
                if self._portfolio_feed is not None:
                    await self._portfolio_feed.subscribe()
                if self._lifecycle_feed is not None:
                    await self._lifecycle_feed.subscribe()
                if self._position_feed is not None:
                    await self._position_feed.subscribe()

                # Subscribe to ticker updates for all active markets
                if self._ticker_feed is not None:
                    market_tickers = self._active_market_tickers()
                    if market_tickers:
                        await self._ticker_feed.subscribe(market_tickers)

                # Resubscribe orderbook channels on reconnect
                if not first_connect:
                    tickers = self._active_market_tickers()
                    if tickers:
                        await self._feed.subscribe_bulk(tickers)

                await self._feed.start()
                # If we reach here, the WS exited cleanly
                self._ws_connected = False
                self._notify("WEBSOCKET DISCONNECTED — prices are stale!", "error", toast=True)
                logger.error("ws_connection_lost", reason="listen loop exited cleanly")
            except Exception as e:
                self._ws_connected = False
                self._notify(f"WEBSOCKET DISCONNECTED: {e}", "error", toast=True)
                logger.error("ws_connection_lost", reason=str(e), error_type=type(e).__name__)

            # Wait and retry — loop instead of recursion
            logger.info("ws_reconnecting")
            self._notify("Reconnecting WebSocket in 5s...", "warning", toast=True)
            await asyncio.sleep(5)

    async def _setup_initial_games(self) -> None:
        """One-time game setup on first WS connect."""
        # Auto-discover events with positions or resting orders
        discovered = await self._discover_active_events()

        # Merge with saved games (union, deduplicate)
        all_tickers = list(dict.fromkeys(discovered + self._initial_games))

        # Fast restore from cached data (no REST calls)
        if self._initial_games_full:
            cached_tickers = set()
            pairs = []
            for data in self._initial_games_full:
                try:
                    pair = self._game_manager.restore_game(data)
                    if pair is None:
                        continue
                    self._adjuster.add_event(pair)
                    pairs.append(pair)
                    cached_tickers.add(pair.event_ticker)
                except Exception:
                    logger.warning("restore_game_failed", event=data.get("event_ticker"))
            # Bulk subscribe all market tickers at once
            tickers = [t for p in pairs for t in (p.ticker_a, p.ticker_b)]
            if tickers:
                await self._feed.subscribe_bulk(tickers)
            if pairs:
                self._notify(f"Loaded {len(pairs)} game(s)")
            # Log startup restores
            if self._data_collector is not None:
                for pair in pairs:
                    prefix = pair.event_ticker.split("-")[0]
                    from talos.ui.widgets import _SPORT_LEAGUE

                    sport, league = _SPORT_LEAGUE.get(prefix, ("", ""))
                    self._data_collector.log_game_add(
                        event_ticker=pair.event_ticker,
                        series_ticker=prefix,
                        sport=sport,
                        league=league,
                        source="startup",
                        ticker_a=pair.ticker_a,
                        ticker_b=pair.ticker_b,
                        fee_type=pair.fee_type,
                        fee_rate=pair.fee_rate,
                    )
            # Backfill expected_expiration_time for games from old cache
            needs_backfill = [p for p in pairs if p.expected_expiration_time is None]
            if needs_backfill:
                await self._backfill_expiration(needs_backfill)

            self._initial_games_full = None
            # Only REST-fetch discovered events not already in cache
            all_tickers = [t for t in all_tickers if t not in cached_tickers]

        if all_tickers:
            pairs = await self._game_manager.add_games(all_tickers)
            for pair in pairs:
                self._adjuster.add_event(pair)
            if pairs:
                self._notify(f"Loaded {len(pairs)} game(s)")
            self._initial_games.clear()

        # Resolve game status — run in background, don't block startup
        if self._game_status_resolver is not None:
            for p in self._game_manager.active_games:
                self._game_status_resolver.set_expiration(
                    p.event_ticker, p.expected_expiration_time
                )
            batch = [
                (p.event_ticker, self._game_manager.subtitles.get(p.event_ticker, ""))
                for p in self._game_manager.active_games
            ]
            if batch:
                asyncio.create_task(self._game_status_resolver.resolve_batch(batch))

    async def _backfill_expiration(self, pairs: list[ArbPair]) -> None:
        """Fetch expected_expiration_time for pairs restored from old cache."""
        sem = asyncio.Semaphore(10)

        async def _fetch(pair: ArbPair) -> None:
            async with sem:
                try:
                    event = await self._rest.get_event(pair.event_ticker, with_nested_markets=True)
                    for m in event.markets:
                        if m.expected_expiration_time:
                            pair.expected_expiration_time = m.expected_expiration_time
                            break
                except Exception:
                    logger.debug(
                        "backfill_expiration_failed",
                        event_ticker=pair.event_ticker,
                    )

        await asyncio.gather(*(_fetch(p) for p in pairs))
        count = sum(1 for p in pairs if p.expected_expiration_time)
        missed = len(pairs) - count
        logger.info(
            "backfill_expiration",
            filled=count,
            missed=missed,
        )
        # Trigger re-persist so next startup has the data
        if count and self._game_manager.on_change:
            self._game_manager.on_change()

    async def refresh_balance(self) -> None:
        """Fetch balance only — fast, independent of order/position sync."""
        try:
            balance = await self._rest.get_balance()
            self._balance = balance.balance
            self._portfolio_value = balance.portfolio_value
        except Exception:
            logger.debug("balance_fetch_failed")

    async def refresh_account(self) -> None:
        """Backup REST sync for orders + positions. WS is primary data source.

        Runs every 30s as a safety net — catches anything WS missed.
        """
        await self._recover_stale_books()

        # Bump sync generation so optimistic placements from this cycle
        # are protected against stale-data overwrites.
        for pair in self._scanner.pairs:
            with contextlib.suppress(KeyError):
                self._adjuster.get_ledger(pair.event_ticker).bump_sync_gen()

        try:
            # Only fetch resting orders — fill data comes from positions API.
            orders = await self._rest.get_all_orders(status="resting")
            self._orders_cache = orders

            # Update top-of-market tracker with current orders
            self._tracker.update_orders(orders, self._scanner.pairs)

            # Re-check all tracked tickers against the live book so the
            # _at_top cache reflects current state, not stale WS events.
            # Suppress callback to avoid toast flood — reevaluate_jumps()
            # handles proposals silently later in this cycle.
            saved_cb = self._tracker.on_change
            self._tracker.on_change = None
            for ticker, side in self._tracker.resting_keys:
                self._tracker.check(ticker, side=side)
            self._tracker.on_change = saved_cb

            # Fetch queue positions and merge into cache
            try:
                tickers = self._active_market_tickers()
                if tickers:
                    new_qp = await self._rest.get_queue_positions(market_tickers=tickers)
                    for oid, qp in new_qp.items():
                        if qp >= 0:
                            self._queue_cache[oid] = _merge_queue(self._queue_cache.get(oid), qp)
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

            # Mark stale / purge proposals whose orders have vanished
            self._proposal_queue.tick(active_order_ids=active_ids)

            # Sync position ledgers from orders (Principle 15)
            for pair in self._scanner.pairs:
                try:
                    ledger = self._adjuster.get_ledger(pair.event_ticker)
                    ledger.sync_from_orders(orders, ticker_a=pair.ticker_a, ticker_b=pair.ticker_b)
                    for side in (Side.A, Side.B):
                        if ledger.is_unit_complete(side):
                            self._adjuster.on_side_complete(pair.event_ticker, side)
                except KeyError:
                    pass  # Pair not registered with adjuster yet

            # Augment fills from positions API (P7/P15 — Kalshi is source
            # of truth, always). GET /portfolio/orders archives old orders,
            # but GET /portfolio/positions never does. This catches fills
            # invisible to sync_from_orders due to order archival.
            pos_map: dict[str, Position] | None = None
            try:
                market_positions = await self._rest.get_positions(limit=200)
                pos_map = {p.ticker: p for p in market_positions}
                for pair in self._scanner.pairs:
                    pos_a = pos_map.get(pair.ticker_a)
                    pos_b = pos_map.get(pair.ticker_b)
                    if pos_a is None and pos_b is None:
                        continue
                    try:
                        ledger = self._adjuster.get_ledger(pair.event_ticker)
                    except KeyError:
                        continue
                    fills = {
                        Side.A: abs(pos_a.position) if pos_a else 0,
                        Side.B: abs(pos_b.position) if pos_b else 0,
                    }
                    costs = {
                        Side.A: pos_a.total_traded if pos_a else 0,
                        Side.B: pos_b.total_traded if pos_b else 0,
                    }
                    fees = {
                        Side.A: pos_a.fees_paid if pos_a else 0,
                        Side.B: pos_b.fees_paid if pos_b else 0,
                    }
                    ledger.sync_from_positions(fills, costs, fees)

            except Exception:
                logger.warning("positions_sync_failed", exc_info=True)

            self._reconcile_stale_positions(pos_map)

            self._recompute_positions()

            # Build enriched order dicts for the order log
            self._order_data = [
                {
                    "ticker": o.ticker,
                    "side": o.side,
                    "price": o.no_price if o.side == "no" else o.yes_price,
                    "filled": o.fill_count,
                    "total": o.initial_count,
                    "remaining": o.remaining_count,
                    "status": o.status,
                    "time": (o.created_time[11:16] if len(o.created_time) > 16 else o.created_time),
                    "queue_pos": o.queue_position,
                }
                for o in orders
            ]

            # Full ledger reconciliation against Kalshi API data
            self._reconcile_with_kalshi(orders, pos_map or {})

            # Re-evaluate jumped tickers that have no pending proposal (P20)
            self.reevaluate_jumps()

            # Check for position imbalances (P16)
            await self.check_imbalances()

            # Evaluate scanner opportunities for automated bid proposals
            self.evaluate_opportunities()

            # Check exit-only triggers and enforce cancellations
            self._check_exit_only()
            await self._enforce_all_exit_only()

            if not self._initial_sync_done:
                self._initial_sync_done = True
                logger.info("initial_sync_complete")
        except Exception:
            logger.exception("refresh_account_error")

    async def refresh_queue_positions(self) -> None:
        """Fast-cadence queue poll with conservative merge."""
        try:
            tickers = self._active_market_tickers()
            if not tickers:
                return
            new_qp = await self._rest.get_queue_positions(market_tickers=tickers)
            for oid, qp in new_qp.items():
                if qp >= 0:
                    self._queue_cache[oid] = _merge_queue(self._queue_cache.get(oid), qp)
        except Exception:
            logger.debug("queue_poll_failed")
            return

        if not self._orders_cache:
            return

        self._recompute_positions()

    async def refresh_trades(self) -> None:
        """Fetch recent trades for CPM tracking.

        Limits concurrency to 5 parallel requests and caps the entire
        batch at 30s to prevent task storms when the API is slow.
        """
        tickers = self._active_market_tickers()
        if not tickers:
            return

        sem = asyncio.Semaphore(5)

        async def _fetch(ticker: str) -> tuple[str, list] | None:
            async with sem:
                try:
                    trades = await self._rest.get_trades(ticker, limit=50)
                    return (ticker, trades)
                except Exception:
                    logger.warning("trade_fetch_failed", ticker=ticker, exc_info=True)
                    return None

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*[_fetch(t) for t in tickers]),
                timeout=30.0,
            )
        except TimeoutError:
            logger.warning("refresh_trades_timeout", ticker_count=len(tickers))
            return

        for result in results:
            if result is not None:
                ticker, trades = result
                self._cpm.ingest(ticker, trades)
                logger.debug("trades_ingested", ticker=ticker, count=len(trades))
        self._cpm.prune()

    # ── WS real-time handlers ──────────────────────────────────────

    def _on_order_update(self, msg: UserOrderMessage) -> None:
        """Handle a real-time order update from the user_orders WS channel.

        Updates the orders cache with monotonic fills and triggers ledger
        re-sync for the affected pair. Notifies on new fills.
        """
        for order in self._orders_cache:
            if order.order_id == msg.order_id:
                old_fill_count = order.fill_count

                # Monotonic update — WS can never decrease fills
                order.fill_count = max(order.fill_count, msg.fill_count)
                order.remaining_count = msg.remaining_count
                order.status = msg.status
                order.maker_fill_cost = max(order.maker_fill_cost, msg.maker_fill_cost)
                order.taker_fill_cost = max(order.taker_fill_cost, msg.taker_fill_cost)
                order.maker_fees = max(order.maker_fees, msg.maker_fees)

                new_fills = order.fill_count - old_fill_count
                if new_fills > 0:
                    price = msg.no_price if msg.side == "no" else msg.yes_price
                    self._notify(
                        f"WS fill: {new_fills} @ {price}¢ on {msg.ticker}",
                    )
                    logger.info(
                        "ws_order_fill",
                        order_id=msg.order_id,
                        ticker=msg.ticker,
                        new_fills=new_fills,
                        total_fills=order.fill_count,
                    )

                # Re-sync the affected pair's ledger
                for pair in self._scanner.pairs:
                    if msg.ticker in (pair.ticker_a, pair.ticker_b):
                        try:
                            ledger = self._adjuster.get_ledger(pair.event_ticker)
                            ledger.sync_from_orders(
                                self._orders_cache,
                                ticker_a=pair.ticker_a,
                                ticker_b=pair.ticker_b,
                            )
                        except KeyError:
                            pass

                self._tracker.update_orders(self._orders_cache, self._scanner.pairs)
                self._recompute_positions()
                # Log order state change
                if self._data_collector is not None:
                    event_ticker = ""
                    for pair in self._scanner.pairs:
                        if msg.ticker in (pair.ticker_a, pair.ticker_b):
                            event_ticker = pair.event_ticker
                            break
                    ws_price = msg.no_price if msg.side == "no" else msg.yes_price
                    self._data_collector.log_order(
                        event_ticker=event_ticker,
                        order_id=msg.order_id,
                        ticker=msg.ticker,
                        side=msg.side,
                        status=msg.status,
                        price=ws_price,
                        initial_count=msg.fill_count + msg.remaining_count,
                        fill_count=msg.fill_count,
                        remaining_count=msg.remaining_count,
                        maker_fill_cost=msg.maker_fill_cost,
                        maker_fees=msg.maker_fees,
                        source="ws_update",
                    )
                return

        # Order not in cache — add it so WS is self-sufficient
        # Check if this order's side matches one of our pair's expected sides
        evt = self._ticker_to_event.get(msg.ticker)
        ws_pair = self._find_pair(evt) if evt else None
        expected = ws_pair is not None and msg.side in {ws_pair.side_a, ws_pair.side_b}
        if expected and msg.status in ("resting", "executed"):
            new_order = Order(
                order_id=msg.order_id,
                ticker=msg.ticker,
                action="buy",
                side=msg.side,
                status=msg.status,
                no_price=msg.no_price,
                yes_price=msg.yes_price,
                fill_count=msg.fill_count,
                remaining_count=msg.remaining_count,
                initial_count=msg.fill_count + msg.remaining_count,
                maker_fill_cost=msg.maker_fill_cost,
                taker_fill_cost=msg.taker_fill_cost,
                maker_fees=msg.maker_fees,
            )
            self._orders_cache.append(new_order)
            logger.info(
                "ws_order_added_to_cache",
                order_id=msg.order_id,
                ticker=msg.ticker,
                status=msg.status,
            )
            # Sync the affected pair
            for pair in self._scanner.pairs:
                if msg.ticker in (pair.ticker_a, pair.ticker_b):
                    try:
                        ledger = self._adjuster.get_ledger(pair.event_ticker)
                        ledger.sync_from_orders(
                            self._orders_cache,
                            ticker_a=pair.ticker_a,
                            ticker_b=pair.ticker_b,
                        )
                    except KeyError:
                        pass
            self._tracker.update_orders(self._orders_cache, self._scanner.pairs)
            self._recompute_positions()

    def _on_fill(self, msg: FillMessage) -> None:
        """Handle a real-time fill from the fill WS channel.

        Supplementary to _on_order_update — provides per-trade detail and
        Kalshi's authoritative post_position for cross-checking.
        """
        logger.info(
            "ws_fill_detail",
            trade_id=msg.trade_id,
            order_id=msg.order_id,
            ticker=msg.market_ticker,
            side=msg.side,
            count=msg.count,
            price=msg.yes_price,
            is_taker=msg.is_taker,
            post_position=msg.post_position,
        )
        if self._data_collector is not None:
            # Find event ticker for this market
            event_ticker = ""
            for pair in self._scanner.pairs:
                if msg.market_ticker in (pair.ticker_a, pair.ticker_b):
                    event_ticker = pair.event_ticker
                    break
            import time as _ft

            placed_at = self._order_placed_at.get(msg.order_id)
            time_since = _ft.monotonic() - placed_at if placed_at else None
            qp = self._queue_cache.get(msg.order_id)
            self._data_collector.log_fill(
                event_ticker=event_ticker,
                trade_id=msg.trade_id,
                order_id=msg.order_id,
                ticker=msg.market_ticker,
                side=msg.side,
                price=msg.yes_price,
                count=msg.count,
                fee_cost=msg.fee_cost if hasattr(msg, "fee_cost") else 0,
                is_taker=msg.is_taker,
                post_position=msg.post_position,
                queue_position=qp,
                time_since_order=time_since,
            )

    # ── Lifecycle event handlers ────────────────────────────────

    def _is_our_market(self, ticker: str) -> bool:
        """Check if a ticker belongs to a market we're actively tracking."""
        return ticker in self._active_market_tickers()

    def _on_market_determined(self, ticker: str, result: str, settlement_value: int) -> None:
        """Handle market determination (result known, not yet settled)."""
        logger.info(
            "lifecycle_determined",
            ticker=ticker,
            result=result,
            settlement_value=settlement_value,
        )
        if self._is_our_market(ticker):
            self._notify(f"Market determined: {ticker} → {result}")
            # Find the event this market belongs to
            event_ticker = ""
            pair = None
            for p in self._scanner.pairs:
                if ticker in (p.ticker_a, p.ticker_b):
                    event_ticker = p.event_ticker
                    pair = p
                    break

            if self._data_collector is not None:
                self._data_collector.log_settlement(
                    event_ticker=event_ticker,
                    ticker=ticker,
                    event_type="determined",
                    result=result,
                    settlement_value=settlement_value,
                )

            # Track which markets have been determined for event_outcome
            if event_ticker and pair:
                if event_ticker not in self._settled_markets:
                    self._settled_markets[event_ticker] = {}
                self._settled_markets[event_ticker][ticker] = result

                # Check if both legs are now determined
                both_done = (
                    pair.ticker_a in self._settled_markets[event_ticker]
                    and pair.ticker_b in self._settled_markets[event_ticker]
                )
                if both_done:
                    if self._data_collector is not None:
                        self._log_event_outcome(event_ticker, pair)
                    # Auto-remove settled game to free WS subscription slots
                    asyncio.create_task(self.remove_game(event_ticker))

    def _on_market_settled(self, ticker: str) -> None:
        """Handle market settlement (cash distributed)."""
        logger.info("lifecycle_settled", ticker=ticker)
        if self._is_our_market(ticker):
            self._notify(f"Market settled: {ticker}")
            asyncio.create_task(self._fetch_settlement(ticker))
            if self._data_collector is not None:
                event_ticker = ""
                for pair in self._scanner.pairs:
                    if ticker in (pair.ticker_a, pair.ticker_b):
                        event_ticker = pair.event_ticker
                        break
                self._data_collector.log_settlement(
                    event_ticker=event_ticker,
                    ticker=ticker,
                    event_type="settled",
                )

    async def _fetch_settlement(self, ticker: str) -> None:
        """Fetch settlement details from Kalshi after a market settles."""
        try:
            settlements = await self._rest.get_settlements(ticker=ticker)
            if not settlements:
                logger.info("settlement_not_found", ticker=ticker)
                return
            s = settlements[0]
            net = s.revenue - s.fee_cost
            self._notify(
                f"Settlement {ticker}: "
                f"{'won' if s.market_result == 'yes' else 'lost'} "
                f"rev ${s.revenue / 100:.2f} fee ${s.fee_cost / 100:.2f} "
                f"net ${net / 100:.2f}"
            )
            logger.info(
                "settlement_fetched",
                ticker=ticker,
                result=s.market_result,
                revenue=s.revenue,
                fee_cost=s.fee_cost,
                no_count=s.no_count,
                yes_count=s.yes_count,
            )
            # Cache settlement with our estimated P&L (still available at this point)
            if self._settlement_cache is not None:
                est_pnl: int | None = None
                sub = ""
                for ps in self._position_summaries:
                    if ps.event_ticker == s.event_ticker:
                        est_pnl = int(ps.locked_profit_cents)
                        break
                sub = self._game_manager.subtitles.get(s.event_ticker, "")
                self._settlement_cache.upsert(s, est_pnl_cents=est_pnl, sub_title=sub)
        except Exception:
            logger.warning("settlement_fetch_failed", ticker=ticker, exc_info=True)

    def _log_event_outcome(self, event_ticker: str, pair: ArbPair) -> None:
        """Log the final outcome of an event with trap analysis."""
        if self._data_collector is None:
            return
        try:
            ledger = self._adjuster.get_ledger(event_ticker)
        except KeyError:
            return

        filled_a = ledger.filled_count(Side.A)
        filled_b = ledger.filled_count(Side.B)
        results = self._settled_markets.get(event_ticker, {})
        result_a = results.get(pair.ticker_a, "")
        result_b = results.get(pair.ticker_b, "")

        prefix = event_ticker.split("-")[0]
        from talos.ui.widgets import _SPORT_LEAGUE

        sport, league = _SPORT_LEAGUE.get(prefix, ("", ""))

        total_cost_a = ledger.filled_total_cost(Side.A)
        total_cost_b = ledger.filled_total_cost(Side.B)
        total_fees_a = ledger.filled_fees(Side.A)
        total_fees_b = ledger.filled_fees(Side.B)

        # Compute revenue: our side wins → payout = count * 100 cents
        side_a = pair.side_a if pair else "no"
        side_b = pair.side_b if pair else "no"
        revenue = 0
        if result_a == side_a:
            revenue += filled_a * 100
        if result_b == side_b:
            revenue += filled_b * 100

        total_pnl = revenue - total_cost_a - total_cost_b - total_fees_a - total_fees_b

        avg_a = total_cost_a / filled_a if filled_a > 0 else 0.0
        avg_b = total_cost_b / filled_b if filled_b > 0 else 0.0

        gs = self._game_status_resolver.get(event_ticker) if self._game_status_resolver else None

        self._data_collector.log_event_outcome(
            event_ticker=event_ticker,
            sport=sport,
            league=league,
            filled_a=filled_a,
            filled_b=filled_b,
            avg_price_a=avg_a,
            avg_price_b=avg_b,
            total_cost_a=total_cost_a,
            total_cost_b=total_cost_b,
            total_fees_a=total_fees_a,
            total_fees_b=total_fees_b,
            result_a=result_a,
            result_b=result_b,
            revenue=revenue,
            total_pnl=total_pnl,
            game_state_at_fill=gs.state if gs else "",
        )
        logger.info(
            "event_outcome_logged",
            event_ticker=event_ticker,
            filled_a=filled_a,
            filled_b=filled_b,
            total_pnl=total_pnl,
            trapped=filled_a != filled_b,
        )

    def _on_market_paused(self, ticker: str, is_deactivated: bool) -> None:
        """Handle market pause/unpause."""
        if is_deactivated:
            self._paused_markets.add(ticker)
            if self._is_our_market(ticker):
                self._notify(f"Market paused: {ticker}", "warning")
        else:
            self._paused_markets.discard(ticker)
            if self._is_our_market(ticker):
                self._notify(f"Market unpaused: {ticker}")
        logger.info(
            "lifecycle_paused",
            ticker=ticker,
            is_deactivated=is_deactivated,
            paused_count=len(self._paused_markets),
        )

    @property
    def paused_markets(self) -> set[str]:
        """Currently paused markets."""
        return set(self._paused_markets)

    # ── Integrity checks ────────────────────────────────────────

    def _reconcile_stale_positions(self, pos_map: dict[str, Position] | None) -> None:
        """Two-strike cleanup: remove pairs whose positions settled but
        lifecycle event was missed (WS disconnect, Talos wasn't running).

        Flag on first detection, remove on second consecutive detection
        to avoid false positives from transient API gaps.
        """
        if pos_map is None:
            return  # API failed — don't flag anything

        current_stale: set[str] = set()
        for pair in self._scanner.pairs:
            pos_a = pos_map.get(pair.ticker_a)
            pos_b = pos_map.get(pair.ticker_b)
            both_zero = (
                (pos_a is None or pos_a.position == 0)
                and (pos_b is None or pos_b.position == 0)
            )
            if not both_zero:
                continue
            try:
                ledger = self._adjuster.get_ledger(pair.event_ticker)
            except KeyError:
                continue
            if ledger.filled_count(Side.A) > 0 or ledger.filled_count(Side.B) > 0:
                current_stale.add(pair.event_ticker)

        to_remove = current_stale & self._stale_candidates
        for event_ticker in to_remove:
            logger.info("stale_position_cleanup", event_ticker=event_ticker)
            self._notify(
                f"Auto-removed settled: {self._display_name(event_ticker)}",
                toast=True,
            )
            asyncio.create_task(self.remove_game(event_ticker))
        self._stale_candidates = current_stale

    def _reconcile_with_kalshi(
        self,
        orders: list[Order],
        pos_map: dict[str, Position],
    ) -> None:
        """Full reconciliation: compute ground truth from Kalshi API data
        and compare against ledger state.

        Checks:
        1. Unit overcommit (filled-in-unit + resting > unit_size)
        2. Multiple resting orders per side (double-bid indicator)
        3. Fill count: orders vs positions API
        4. Ledger resting divergence from Kalshi
        5. Ledger fill divergence from Kalshi

        Runs every poll cycle after sync_from_orders + sync_from_positions.
        """
        for pair in self._scanner.pairs:
            try:
                ledger = self._adjuster.get_ledger(pair.event_ticker)
            except KeyError:
                continue

            ticker_to_side = {pair.ticker_a: Side.A, pair.ticker_b: Side.B}
            name = self._display_name(pair.event_ticker)

            # For same-ticker pairs, disambiguate by order side (yes/no)
            if pair.is_same_ticker:
                order_side_map = {pair.side_a: Side.A, pair.side_b: Side.B}
            else:
                order_side_map = None

            # ── Compute ground truth from Kalshi orders ──
            kalshi_fills: dict[Side, int] = {Side.A: 0, Side.B: 0}
            kalshi_resting: dict[Side, int] = {Side.A: 0, Side.B: 0}
            kalshi_resting_order_count: dict[Side, int] = {Side.A: 0, Side.B: 0}

            for order in orders:
                if order.action != "buy":
                    continue
                if pair.is_same_ticker:
                    if order.ticker != pair.ticker_a:
                        continue
                    if order_side_map is None or order.side not in order_side_map:
                        continue
                    side = order_side_map[order.side]
                else:
                    if order.side not in {pair.side_a, pair.side_b}:
                        continue
                    side = ticker_to_side.get(order.ticker)
                    if side is None:
                        continue
                if order.fill_count > 0:
                    kalshi_fills[side] += order.fill_count
                if order.remaining_count > 0 and order.status in ("resting", "executed"):
                    kalshi_resting[side] += order.remaining_count
                    kalshi_resting_order_count[side] += 1

            # ── Augment fills from positions API ──
            pos_fills: dict[Side, int] = {Side.A: 0, Side.B: 0}
            for side, ticker in ((Side.A, pair.ticker_a), (Side.B, pair.ticker_b)):
                pos = pos_map.get(ticker)
                if pos is not None:
                    pos_fills[side] = abs(pos.position)
            # Authoritative fill count = max of both sources
            auth_fills = {s: max(kalshi_fills[s], pos_fills[s]) for s in (Side.A, Side.B)}

            for side in (Side.A, Side.B):
                sl = side.value  # "A" or "B"

                # Check 1: Unit overcommit (hard invariant P16)
                filled_in_unit = auth_fills[side] % ledger.unit_size
                if filled_in_unit + kalshi_resting[side] > ledger.unit_size:
                    msg = (
                        f"OVERCOMMIT {name} {sl}: "
                        f"{filled_in_unit} filled + {kalshi_resting[side]} resting "
                        f"= {filled_in_unit + kalshi_resting[side]} > unit {ledger.unit_size}"
                    )
                    logger.error(
                        "reconcile_overcommit",
                        event_ticker=pair.event_ticker,
                        side=sl,
                        filled_in_unit=filled_in_unit,
                        kalshi_resting=kalshi_resting[side],
                        unit_size=ledger.unit_size,
                    )
                    self._notify(msg, "error", toast=True)

                # Check 2: Multiple resting orders (double-bid indicator)
                if kalshi_resting_order_count[side] > 1:
                    logger.warning(
                        "reconcile_multiple_resting",
                        event_ticker=pair.event_ticker,
                        side=sl,
                        order_count=kalshi_resting_order_count[side],
                        total_resting=kalshi_resting[side],
                    )

                # Check 3: Fill consistency between orders and positions APIs
                if (
                    pos_fills[side] > 0
                    and kalshi_fills[side] > 0
                    and pos_fills[side] != kalshi_fills[side]
                ):
                    logger.info(
                            "reconcile_fill_source_gap",
                            event_ticker=pair.event_ticker,
                            side=sl,
                            orders_fills=kalshi_fills[side],
                            positions_fills=pos_fills[side],
                        )

                # Check 4: Ledger resting vs Kalshi resting
                # Skip during optimistic placement (stale-sync guard active)
                ledger_resting = ledger.resting_count(side)
                if (
                    ledger._sides[side]._placed_at_gen is None
                    and ledger_resting != kalshi_resting[side]
                ):
                    logger.warning(
                            "reconcile_resting_mismatch",
                            event_ticker=pair.event_ticker,
                            side=sl,
                            ledger=ledger_resting,
                            kalshi=kalshi_resting[side],
                        )

                # Check 5: Ledger fills vs authoritative fills
                ledger_fills = ledger.filled_count(side)
                if ledger_fills != auth_fills[side]:
                    logger.warning(
                        "reconcile_fill_mismatch",
                        event_ticker=pair.event_ticker,
                        side=sl,
                        ledger=ledger_fills,
                        kalshi=auth_fills[side],
                    )

    # ── Event handlers ───────────────────────────────────────────

    def on_top_of_market_change(self, ticker: str, side: str, at_top: bool) -> None:
        """Handle top-of-market state transition — evaluate adjustment.

        Logs to structlog only (no toast) to prevent toast accumulation
        from freezing the event loop. The Status column already shows
        jumped state, and proposals handle the response.
        """
        # Invalidate jump-eval cache — real WS transition, force re-evaluation
        self._last_jump_eval.pop(ticker, None)

        resting = self._tracker.resting_price(ticker, side=side)
        evt = self._adjuster.resolve_event(ticker)
        label = self._display_name(evt) if evt else ticker
        if at_top:
            logger.info("back_at_top", ticker=ticker, side=side, label=label, resting=resting)
        else:
            top_price = self._tracker.book_top_price(ticker, side=side) or 0
            logger.info(
                "jumped",
                ticker=ticker,
                side=side,
                label=label,
                resting=resting,
                top_price=top_price,
            )

        self._generate_jump_proposal(ticker, side=side, at_top=at_top)

    def _generate_jump_proposal(
        self, ticker: str, *, side: str = "no", at_top: bool = False
    ) -> None:
        """Evaluate a jump and enqueue a proposal if appropriate.

        Shared by on_top_of_market_change (with toast) and reevaluate_jumps
        (silent). The notification is the caller's responsibility.
        """
        evt_ticker = self._adjuster.resolve_event(ticker)
        exit_only = self.is_exit_only(evt_ticker) if evt_ticker else False
        proposal = self._adjuster.evaluate_jump(ticker, at_top, exit_only=exit_only, side=side)
        if proposal is not None:
            evt = proposal.event_ticker
            name = self._display_name(evt)

            if proposal.action == "withdraw":
                logger.info(
                    "adjustment_withdraw",
                    event_ticker=evt,
                    reason=proposal.reason,
                )
                kind = "withdraw"
                summary = f"WITHDRAW {name} — cancel both sides"
            elif proposal.action == "hold":
                logger.info(
                    "adjustment_hold",
                    event_ticker=evt,
                    side=proposal.side,
                    reason=proposal.reason,
                )
                kind = "hold"
                summary = f"HOLD {name} side {proposal.side}"
            else:
                logger.info(
                    "adjustment_proposed",
                    event_ticker=evt,
                    side=proposal.side,
                    old_price=proposal.cancel_price,
                    new_price=proposal.new_price,
                    reason=proposal.reason,
                )
                kind = "adjustment"
                summary = (
                    f"MOVE {name} side {proposal.side}"
                    f"\n  {proposal.cancel_price}c \u2192 {proposal.new_price}c"
                )
            key = ProposalKey(
                event_ticker=proposal.event_ticker,
                side="" if kind == "withdraw" else proposal.side,
                kind=kind,
            )
            envelope = Proposal(
                key=key,
                kind=kind,
                summary=summary,
                detail=proposal.reason,
                created_at=datetime.now(UTC),
                adjustment=proposal if proposal.action == "follow_jump" else None,
            )
            self._proposal_queue.add(envelope)

    def reevaluate_jumps(self) -> None:
        """Re-check all jumped tickers and generate proposals if missing.

        Catches jumps missed due to startup ordering or lost events (P20).
        Unlike on_top_of_market_change, this does NOT fire toast notifications —
        it silently ensures a proposal exists for every jumped ticker.
        """
        pending_keys = {p.key for p in self._proposal_queue.pending()}
        for pair in self._scanner.pairs:
            # Skip if a withdraw proposal already covers this event
            has_withdraw = any(
                k.event_ticker == pair.event_ticker and k.kind == "withdraw" for k in pending_keys
            )
            if has_withdraw:
                continue
            for ticker, side_label, pair_side in [
                (pair.ticker_a, "A", pair.side_a),
                (pair.ticker_b, "B", pair.side_b),
            ]:
                at_top = self._tracker.is_at_top(ticker, side=pair_side)
                if at_top is not None and not at_top:
                    # Jumped — check if there's already a proposal for this side
                    has_proposal = any(
                        k.event_ticker == pair.event_ticker
                        and k.side == side_label
                        and k.kind in ("adjustment", "hold")
                        for k in pending_keys
                    )
                    if not has_proposal:
                        # Skip if book top and resting price haven't changed
                        # since the last evaluation — same HOLD would result.
                        book_top = self._tracker.book_top_price(ticker, side=pair_side) or 0
                        resting = self._tracker.resting_price(ticker, side=pair_side) or 0
                        eval_key = (book_top, resting)
                        if eval_key == self._last_jump_eval.get(ticker):
                            continue
                        self._last_jump_eval[ticker] = eval_key
                        self._generate_jump_proposal(ticker, side=pair_side)

    async def check_imbalances(self) -> None:
        """Detect and auto-execute rebalance catch-ups (P16).

        Delegates to compute_rebalance_proposal() for pure detection,
        then auto-executes via execute_rebalance() without operator approval.
        Rebalance is risk-reducing (closing exposure), so it bypasses the
        ProposalQueue (P2 progression: supervised -> autonomous for catch-up).

        NOTE: The old pending_keys/ProposalQueue check is intentionally removed.
        Auto-execution replaces manual proposal approval — the executed_this_cycle
        set prevents double-firing within a single call.
        """
        executed_this_cycle: set[str] = set()
        for pair in self._scanner.pairs:
            if pair.event_ticker in executed_this_cycle:
                continue

            try:
                ledger = self._adjuster.get_ledger(pair.event_ticker)
            except KeyError:
                continue

            snapshot = self._scanner.all_snapshots.get(pair.event_ticker)
            proposal = compute_rebalance_proposal(
                pair.event_ticker,
                ledger,
                pair,
                snapshot,
                self._display_name(pair.event_ticker),
                self._feed.book_manager,
            )
            if proposal is None or proposal.rebalance is None:
                # No cross-side imbalance — check for single-side overcommit
                # (balanced committed counts but unit capacity violated)
                overcommit = compute_overcommit_reduction(
                    pair.event_ticker,
                    ledger,
                    pair,
                    self._display_name(pair.event_ticker),
                )
                if overcommit is not None:
                    await _execute_rebalance(
                        overcommit,
                        rest_client=self._rest,
                        adjuster=self._adjuster,
                        scanner=self._scanner,
                        notify=self._notify,
                    )
                    executed_this_cycle.add(pair.event_ticker)
                    continue

                # No overcommit — check for mid-unit top-up
                if not self.is_exit_only(pair.event_ticker):
                    topup_needs = compute_topup_needs(ledger, pair, snapshot)
                    for side, (qty, price) in topup_needs.items():
                        ok, reason = ledger.is_placement_safe(side, qty, price, rate=pair.fee_rate)
                        if not ok:
                            self._notify(
                                f"Top-up BLOCKED ({side.value}): {reason}",
                                "warning",
                            )
                            continue
                        ticker = pair.ticker_a if side == Side.A else pair.ticker_b
                        group = await _create_order_group(
                            self._rest, pair.event_ticker, side.value, qty
                        )
                        pair_side = pair.side_a if side == Side.A else pair.side_b
                        try:
                            await self._rest.create_order(
                                ticker=ticker,
                                action="buy",
                                side=pair_side,
                                yes_price=price if pair_side == "yes" else None,
                                no_price=price if pair_side == "no" else None,
                                count=qty,
                                order_group_id=group,
                            )
                            self._notify(
                                f"Top-up {pair.event_ticker} {side.value}: {qty} @ {price}c",
                                "information",
                            )
                            logger.info(
                                "topup_placed",
                                event_ticker=pair.event_ticker,
                                side=side.value,
                                qty=qty,
                                price=price,
                            )
                        except Exception as e:
                            self._notify(
                                f"Top-up FAILED ({side.value}): {type(e).__name__}: {e}",
                                "error",
                            )
                            logger.exception(
                                "topup_error",
                                event_ticker=pair.event_ticker,
                                side=side.value,
                            )
                continue

            # Auto-execute catch-up — no ProposalQueue
            await _execute_rebalance(
                proposal.rebalance,
                rest_client=self._rest,
                adjuster=self._adjuster,
                scanner=self._scanner,
                notify=self._notify,
            )
            executed_this_cycle.add(pair.event_ticker)

    def evaluate_opportunities(self) -> None:
        """Run OpportunityProposer against all scanner pairs.

        Only active when automation is enabled.
        """
        if not self._auto_config.enabled:
            return
        pending_keys = {p.key for p in self._proposal_queue.pending()}
        for pair in self._scanner.pairs:
            opp = self._scanner.get_opportunity(pair.event_ticker)
            if opp is None:
                continue
            try:
                ledger = self._adjuster.get_ledger(pair.event_ticker)
            except KeyError:
                continue
            proposal = self._proposer.evaluate(
                pair,
                opp,
                ledger,
                pending_keys,
                display_name=self._display_name(pair.event_ticker),
                exit_only=self.is_exit_only(pair.event_ticker),
            )
            if proposal is not None:
                self._proposal_queue.add(proposal)
                pending_keys.add(proposal.key)

    # ── Action methods ──────────────────────────────────────────────

    async def place_bids(self, bid: BidConfirmation) -> None:
        """Place orders on both legs (side-aware: YES or NO per pair config).

        Safety: checks is_placement_safe() on both sides before sending orders.
        After placement, optimistically updates the ledger via record_placement()
        (with generation-based stale-sync guard) and appends to _orders_cache.
        """
        # Block until first refresh_account has synced ledger data from Kalshi.
        # Without this, the ledger may be empty after a restart, causing P18
        # to trivially pass because it sees no position on the other side.
        if not self._initial_sync_done:
            self._notify("Bid BLOCKED: waiting for initial sync", "warning", toast=True)
            logger.warning("bid_blocked_no_sync", ticker_a=bid.ticker_a, ticker_b=bid.ticker_b)
            return

        # Block on exit-only events
        evt_for_bid = self._adjuster.resolve_event(bid.ticker_a)
        if evt_for_bid and self.is_exit_only(evt_for_bid):
            label = self._display_name(evt_for_bid)
            self._notify(
                f"Bid BLOCKED {label}: exit-only mode (press E to disable)", "error", toast=True
            )
            logger.error("bid_blocked_exit_only", event_ticker=evt_for_bid)
            return

        # Block on paused markets
        for ticker in (bid.ticker_a, bid.ticker_b):
            if ticker in self._paused_markets:
                evt = self._adjuster.resolve_event(ticker)
                label = self._display_name(evt) if evt else ticker
                self._notify(f"Bid BLOCKED {label}: {ticker} is paused", "error", toast=True)
                logger.error("bid_blocked_market_paused", ticker=ticker)
                return

        # Look up ledger for safety gate
        ledger = self._find_ledger_for_bid(bid)

        # Hard safety gate (P16, P18) — blocks if unit exceeded or arb unprofitable
        if ledger is not None:
            pair = self._find_pair(ledger.event_ticker)
            fee_rate = pair.fee_rate if pair is not None else MAKER_FEE_RATE
            for side, price in [(Side.A, bid.no_a), (Side.B, bid.no_b)]:
                ok, reason = ledger.is_placement_safe(side, bid.qty, price, rate=fee_rate)
                if not ok:
                    name = self._display_name(ledger.event_ticker)
                    self._notify(
                        f"Bid BLOCKED {name} (side {side.value}): {reason}", "error", toast=True
                    )
                    logger.error(
                        "bid_blocked_safety_gate",
                        event_ticker=ledger.event_ticker,
                        side=side.value,
                        reason=reason,
                        qty=bid.qty,
                        price=price,
                    )
                    return

        # Resolve pair for side-aware order placement
        event_ticker = self._adjuster.resolve_event(bid.ticker_a)
        pair = self._find_pair(event_ticker) if event_ticker else None
        if pair is None:
            logger.error("place_bids_no_pair", ticker_a=bid.ticker_a)
            self._notify(f"BLOCKED: no pair found for {bid.ticker_a}", "error")
            return

        side_a = pair.side_a
        side_b = pair.side_b

        try:
            order_a = await self._rest.create_order(
                ticker=bid.ticker_a,
                action="buy",
                side=side_a,
                yes_price=bid.no_a if side_a == "yes" else None,
                no_price=bid.no_a if side_a == "no" else None,
                count=bid.qty,
            )
            logger.info("order_placed", ticker=bid.ticker_a, order_id=order_a.order_id)
            order_b = await self._rest.create_order(
                ticker=bid.ticker_b,
                action="buy",
                side=side_b,
                yes_price=bid.no_b if side_b == "yes" else None,
                no_price=bid.no_b if side_b == "no" else None,
                count=bid.qty,
            )
            logger.info("order_placed", ticker=bid.ticker_b, order_id=order_b.order_id)
            self._notify(
                f"Orders placed: {bid.ticker_a} @ {bid.no_a}c, {bid.ticker_b} @ {bid.no_b}c",
            )

            # Optimistic ledger update — prevents duplicate proposals when a
            # concurrent refresh_account has stale data (the orders weren't in
            # the API response it fetched before placement). The generation
            # guard in sync_from_orders prevents stale syncs from clearing this.
            if ledger is not None:
                ledger.record_placement(
                    Side.A,
                    order_a.order_id,
                    order_a.remaining_count,
                    bid.no_a,
                )
                ledger.record_placement(
                    Side.B,
                    order_b.order_id,
                    order_b.remaining_count,
                    bid.no_b,
                )
            # Track placement time for fill latency calculation
            import time as _pt

            _now = _pt.monotonic()
            self._order_placed_at[order_a.order_id] = _now
            self._order_placed_at[order_b.order_id] = _now
            # Add to orders cache so WS handler can match future updates
            self._orders_cache.extend([order_a, order_b])
            # Log to data collector
            if self._data_collector is not None:
                for order in (order_a, order_b):
                    price = order.no_price if order.side == "no" else order.yes_price
                    self._data_collector.log_order(
                        event_ticker=pair.api_event_ticker,
                        order_id=order.order_id,
                        ticker=order.ticker,
                        side=order.side,
                        status=order.status,
                        price=price,
                        initial_count=order.initial_count,
                        fill_count=order.fill_count,
                        remaining_count=order.remaining_count,
                        source="auto_accept" if self._auto_config.enabled else "manual",
                    )
        except KalshiRateLimitError:
            raise  # Let auto-accept back off
        except Exception as e:
            event_ticker = bid.ticker_a.rsplit("-", 1)[0] if "-" in bid.ticker_a else ""
            label = self._display_name(event_ticker) if event_ticker else bid.ticker_a
            self._notify(f"Order error ({label}): {type(e).__name__}: {e}", "error", toast=True)
            logger.exception(
                "place_bids_error",
                event_ticker=event_ticker,
                ticker_a=bid.ticker_a,
                ticker_b=bid.ticker_b,
            )
            # Record failure cooldown to prevent the proposer from endlessly
            # re-proposing the same bid (e.g. post-only cross where the
            # orderbook condition persists).
            if event_ticker:
                self._proposer.record_placement_failure(event_ticker)
            # "post only cross" means our local book is stale — the price we
            # tried would immediately match on Kalshi's real book. Resubscribe
            # to get a fresh snapshot and correct our local state.
            is_cross = (
                isinstance(e, KalshiAPIError)
                and "post only cross" in str(e).lower()
            )
            if is_cross:
                for ticker in (bid.ticker_a, bid.ticker_b):
                    await self._feed.unsubscribe(ticker)
                    await self._feed.subscribe(ticker)
                logger.info(
                    "orderbook_resync_after_cross",
                    ticker_a=bid.ticker_a,
                    ticker_b=bid.ticker_b,
                )

    async def add_games(self, urls: list[str], source: str = "scan") -> None:
        """Add games by URL."""
        try:
            pairs = await self._game_manager.add_games(urls)
            for pair in pairs:
                self._adjuster.add_event(pair)
            if self._game_status_resolver is not None:
                for p in pairs:
                    self._game_status_resolver.set_expiration(
                        p.event_ticker, p.expected_expiration_time
                    )
                batch = [
                    (p.event_ticker, self._game_manager.subtitles.get(p.event_ticker, ""))
                    for p in pairs
                ]
                if batch:
                    await self._game_status_resolver.resolve_batch(batch)
            # Log game adds to data collector
            if self._data_collector is not None:
                for pair in pairs:
                    prefix = pair.event_ticker.split("-")[0]
                    from talos.ui.widgets import _SPORT_LEAGUE

                    sport, league = _SPORT_LEAGUE.get(prefix, ("", ""))
                    gs = (
                        self._game_status_resolver.get(pair.event_ticker)
                        if self._game_status_resolver
                        else None
                    )
                    self._data_collector.log_game_add(
                        event_ticker=pair.event_ticker,
                        series_ticker=prefix,
                        sport=sport,
                        league=league,
                        source=source,
                        ticker_a=pair.ticker_a,
                        ticker_b=pair.ticker_b,
                        volume_a=self._game_manager.volumes_24h.get(pair.ticker_a, 0),
                        volume_b=self._game_manager.volumes_24h.get(pair.ticker_b, 0),
                        fee_type=pair.fee_type,
                        fee_rate=pair.fee_rate,
                        scheduled_start=gs.scheduled_start.isoformat()
                        if gs and gs.scheduled_start
                        else None,
                    )
            self._notify(f"Added {len(urls)} game(s)", toast=True)
        except Exception as e:
            from talos.game_manager import MarketPickerNeeded

            if isinstance(e, MarketPickerNeeded):
                raise  # Propagate to UI for market picker
            self._notify(f"Error: {e}", "error", toast=True)
            logger.exception("add_games_error")

    async def add_market_pairs(
        self, event: object, markets: list[object],
    ) -> list[ArbPair]:
        """Add YES/NO arb pairs for selected markets from a non-sports event.

        Called from the UI after the market picker selects markets.
        Handles full engine wiring: adjuster, game status, persistence.
        """
        from talos.models.market import Event as EventModel
        from talos.models.market import Market as MarketModel

        if not isinstance(event, EventModel):
            return []
        pairs: list[ArbPair] = []
        for market in markets:
            if not isinstance(market, MarketModel):
                continue
            try:
                pair = await self._game_manager.add_market_as_pair(event, market)
                self._adjuster.add_event(pair)
                if self._game_status_resolver is not None:
                    self._game_status_resolver.set_expiration(
                        pair.event_ticker, pair.expected_expiration_time,
                    )
                pairs.append(pair)
            except Exception:
                logger.warning(
                    "add_market_pair_failed",
                    market_ticker=getattr(market, "ticker", "?"),
                    exc_info=True,
                )
        if pairs:
            self._notify(f"Added {len(pairs)} market pair(s)", toast=True)
        return pairs

    async def remove_game(self, event_ticker: str) -> None:
        """Remove a game from monitoring."""
        try:
            self._exit_only_events.discard(event_ticker)
            self._stale_candidates.discard(event_ticker)
            if self._game_status_resolver is not None:
                self._game_status_resolver.remove(event_ticker)
            self._adjuster.remove_event(event_ticker)
            await self._game_manager.remove_game(event_ticker)
            self._notify(f"Removed {event_ticker}", toast=True)
        except Exception as e:
            self._notify(f"Error: {e}", "error", toast=True)

    async def clear_games(self) -> None:
        """Clear all monitored games."""
        try:
            pairs = list(self._game_manager.active_games)
            for pair in pairs:
                self._exit_only_events.discard(pair.event_ticker)
                self._stale_candidates.discard(pair.event_ticker)
                self._game_started_events.discard(pair.event_ticker)
                if self._game_status_resolver is not None:
                    self._game_status_resolver.remove(pair.event_ticker)
                self._adjuster.remove_event(pair.event_ticker)
            await self._game_manager.clear_all_games()
            self._notify(f"Cleared {len(pairs)} game(s)", toast=True)
        except Exception as e:
            self._notify(f"Error: {e}", "error", toast=True)

    async def refresh_game_status(self) -> None:
        """Hourly: re-fetch game status for all active events."""
        if self._game_status_resolver is not None:
            await self._game_status_resolver.refresh_all()

    async def approve_proposal(self, key: ProposalKey) -> None:
        """Approve and execute a queued proposal."""
        try:
            envelope = self._proposal_queue.approve(key)
        except KeyError:
            self._notify("No pending proposal to approve", "warning", toast=True)
            return

        if envelope.kind == "hold":
            self._notify(f"Dismissed: {envelope.summary}")
            return

        if envelope.kind == "withdraw":
            await self._execute_withdrawal(envelope.key.event_ticker)
            return

        if envelope.kind == "rebalance":
            if envelope.rebalance is not None:
                await _execute_rebalance(
                    envelope.rebalance,
                    rest_client=self._rest,
                    adjuster=self._adjuster,
                    scanner=self._scanner,
                    notify=self._notify,
                )
                await self._verify_after_action(envelope.rebalance.event_ticker)
            else:
                self._notify(f"Acknowledged: {envelope.summary} (manual action needed)")
            return

        if envelope.kind == "adjustment" and envelope.adjustment is not None:
            try:
                await self._adjuster.execute(envelope.adjustment, self._rest)
                # Invalidate jump-eval cache — resting price changed
                adj_pair = self._find_pair(envelope.adjustment.event_ticker)
                if adj_pair is not None:
                    adj_ticker = (
                        adj_pair.ticker_a if envelope.adjustment.side == "A" else adj_pair.ticker_b
                    )
                    self._last_jump_eval.pop(adj_ticker, None)
                adj_name = self._display_name(envelope.adjustment.event_ticker)
                self._notify(
                    f"Adjusted: {adj_name}"
                    f" {envelope.adjustment.side}"
                    f" \u2192 {envelope.adjustment.new_price}c",
                )
            except KalshiRateLimitError:
                raise  # Let auto-accept back off
            except Exception as e:
                self._notify(f"Adjustment FAILED: {type(e).__name__}: {e}", "error", toast=True)
                logger.exception(
                    "adjustment_execute_error",
                    event_ticker=envelope.adjustment.event_ticker,
                )
            await self._verify_after_action(envelope.adjustment.event_ticker)
        elif envelope.kind == "bid" and envelope.bid is not None:
            bid = envelope.bid
            from talos.models.strategy import BidConfirmation

            # Reset stability timer — prevents re-proposing in the sync gap
            self._proposer.record_approval(bid.event_ticker)

            confirmation = BidConfirmation(
                ticker_a=bid.ticker_a,
                ticker_b=bid.ticker_b,
                no_a=bid.no_a,
                no_b=bid.no_b,
                qty=bid.qty,
            )
            await self.place_bids(confirmation)
            await self._verify_after_action(bid.event_ticker)

    def reject_proposal(self, key: ProposalKey) -> None:
        """Reject and remove a queued proposal."""
        self._proposal_queue.reject(key)
        if key.kind == "adjustment" and key.side:
            self._adjuster.clear_proposal(key.event_ticker, Side(key.side))
        elif key.kind == "bid":
            self._proposer.record_rejection(key.event_ticker)
        self._notify(f"Rejected: {self._display_name(key.event_ticker)} {key.kind}")

    async def _execute_withdrawal(self, event_ticker: str) -> None:
        """Cancel both sides' resting orders when arb is unprofitable with no fills.

        Looks up resting order IDs fresh from the ledger at execution time (P7).
        """
        try:
            ledger = self._adjuster.get_ledger(event_ticker)
        except KeyError:
            self._notify(f"Withdraw FAILED: no ledger for {event_ticker}", "error", toast=True)
            return

        name = self._display_name(event_ticker)
        cancelled = 0
        for side in (Side.A, Side.B):
            order_id = ledger.resting_order_id(side)
            if order_id is not None:
                try:
                    await self._rest.cancel_order(order_id)
                    cancelled += 1
                    logger.info(
                        "withdrawal_cancelled",
                        event_ticker=event_ticker,
                        side=side.value,
                        order_id=order_id,
                    )
                except Exception as e:
                    self._notify(
                        f"Withdraw cancel FAILED ({side.value}): {e}",
                        "error",
                        toast=True,
                    )
                    logger.exception(
                        "withdrawal_cancel_error",
                        event_ticker=event_ticker,
                        side=side.value,
                        order_id=order_id,
                    )

        if cancelled > 0:
            self._notify(f"Withdrew {name} — cancelled {cancelled} order(s)")
        else:
            self._notify(f"Withdrew {name} — no resting orders to cancel")

        await self._verify_after_action(event_ticker)

    async def _verify_after_action(self, event_ticker: str) -> None:
        """Re-sync from Kalshi after any order action to verify outcome."""
        pair = self._find_pair(event_ticker)
        if pair is None:
            return
        try:
            api_evt = pair.api_event_ticker
            orders = await self._rest.get_all_orders(event_ticker=api_evt)
            ledger = self._adjuster.get_ledger(event_ticker)
            ledger.sync_from_orders(orders, ticker_a=pair.ticker_a, ticker_b=pair.ticker_b)
            # For same-ticker pairs, Kalshi reports net position = 0 (YES+NO
            # cancel). Position-based verification is meaningless — skip it.
            if not pair.is_same_ticker:
                positions = await self._rest.get_positions(
                    event_ticker=api_evt,
                    limit=200,
                )
                pos_map = {p.ticker: p for p in positions}
                pos_a = pos_map.get(pair.ticker_a)
                pos_b = pos_map.get(pair.ticker_b)
                fills = {
                    Side.A: abs(pos_a.position) if pos_a else 0,
                    Side.B: abs(pos_b.position) if pos_b else 0,
                }
                costs = {
                    Side.A: pos_a.total_traded if pos_a else 0,
                    Side.B: pos_b.total_traded if pos_b else 0,
                }
                fees = {
                    Side.A: pos_a.fees_paid if pos_a else 0,
                    Side.B: pos_b.fees_paid if pos_b else 0,
                }
                ledger.sync_from_positions(fills, costs, fees)
            logger.info(
                "post_action_verify",
                event_ticker=event_ticker,
                committed_a=ledger.total_committed(Side.A),
                committed_b=ledger.total_committed(Side.B),
                delta=ledger.current_delta(),
            )
        except KalshiRateLimitError:
            # Verify is non-critical — the action already succeeded and the
            # 30s polling cycle will sync. Don't alarm the operator.
            logger.debug(
                "post_action_verify_rate_limited",
                event_ticker=event_ticker,
            )
        except Exception as e:
            logger.warning(
                "post_action_verify_failed",
                event_ticker=event_ticker,
                exc_info=True,
            )
            name = self._display_name(event_ticker)
            self._notify(
                f"Verify FAILED for {name} ({type(e).__name__}) — position data may be stale",
                "warning",
                toast=True,
            )

    # ── Internal helpers ─────────────────────────────────────────────

    async def _discover_active_events(self) -> list[str]:
        """Query Kalshi for events with positions or resting orders.

        Also stores rich EventPosition data for UI access (realized_pnl, etc.).
        """
        try:
            event_positions = await self._rest.get_event_positions()
            self._event_positions = {ep.event_ticker: ep for ep in event_positions}
            tickers = [ep.event_ticker for ep in event_positions]
            if tickers:
                logger.info("discovered_active_events", count=len(tickers), tickers=tickers)
            return tickers
        except Exception:
            logger.warning("event_discovery_failed", exc_info=True)
            return []

    def _compute_event_status(self, event_ticker: str) -> str:
        """Compute a human-readable status for an event's position.

        Shows why Talos is or isn't acting — makes inaction visible (P20).
        """
        try:
            ledger = self._adjuster.get_ledger(event_ticker)
        except KeyError:
            return ""

        filled_a = ledger.filled_count(Side.A)
        filled_b = ledger.filled_count(Side.B)
        resting_a = ledger.resting_count(Side.A)
        resting_b = ledger.resting_count(Side.B)
        total_a = filled_a + resting_a
        total_b = filled_b + resting_b

        # Exit-only status takes priority
        if self.is_exit_only(event_ticker):
            if filled_a == filled_b and resting_a == 0 and resting_b == 0:
                return "EXIT"
            if resting_a > 0 or resting_b > 0:
                if filled_a != filled_b:
                    diff = abs(filled_a - filled_b)
                    behind = "B" if filled_a > filled_b else "A"
                    return f"EXIT -{diff} {behind}"
                return "EXITING"
            # Imbalanced, no resting — waiting for behind side to fill
            diff = abs(filled_a - filled_b)
            behind = "B" if filled_a > filled_b else "A"
            return f"EXIT -{diff} {behind}"

        if total_a == 0 and total_b == 0:
            # No position — show why proposer isn't suggesting (fall through)
            return self._compute_proposer_status(event_ticker)

        # Settled — no resting orders and either balanced fills or markets closed
        if resting_a == 0 and resting_b == 0 and (filled_a > 0 or filled_b > 0):
            if filled_a == filled_b:
                return "Settled"
            # Unbalanced fills but markets closed → settled with loss
            pair = self._find_pair(event_ticker)
            if pair is not None:
                books = self._feed.book_manager
                no_ask_a = not books.best_ask(pair.ticker_a, side=pair.side_a)
                no_ask_b = not books.best_ask(pair.ticker_b, side=pair.side_b)
                if no_ask_a and no_ask_b:
                    return "Settled"

        # Jumped — resting orders not at top of market
        if resting_a > 0 or resting_b > 0:
            pair = self._find_pair(event_ticker)
            if pair is not None:
                jumped_a = (
                    resting_a > 0
                    and self._tracker.is_at_top(pair.ticker_a, side=pair.side_a) is False
                )
                jumped_b = (
                    resting_b > 0
                    and self._tracker.is_at_top(pair.ticker_b, side=pair.side_b) is False
                )
                if jumped_a or jumped_b:
                    sides = ""
                    if jumped_a:
                        sides += "A"
                    if jumped_b:
                        sides += "B"
                    return f"Jumped {sides}"

        # Check if pending proposal exists — O(1) via pre-built cache
        pending_kinds = getattr(self, "_pending_kinds_cache", {}).get(event_ticker, set())

        if "rebalance" in pending_kinds:
            return "Imbalanced"

        # Both sides at unit boundary with equal fills → pair complete
        if ledger.both_sides_complete() and filled_a == filled_b:
            if resting_a > 0 and resting_b > 0:
                return "Bidding"  # Next pair already deployed

            if "bid" in pending_kinds:
                return "Proposed"

            return self._compute_proposer_status(event_ticker)

        # Check for fill imbalance
        if filled_a != filled_b:
            if filled_a > filled_b:
                behind = "B"
                diff = filled_a - filled_b
            else:
                behind = "A"
                diff = filled_b - filled_a
            if resting_a > 0 or resting_b > 0:
                return f"Filling ({behind} -{diff})"
            return f"Waiting {behind} (-{diff})"

        # Equal fills, work in progress
        if resting_a > 0 and resting_b > 0:
            return "Bidding"
        if resting_a > 0:
            return "Need bid B"
        if resting_b > 0:
            return "Need bid A"

        return ""

    def _find_pair(self, event_ticker: str) -> ArbPair | None:
        """Look up scanner pair by event ticker — O(1) via index."""
        result = self._pair_index.get(event_ticker)
        if result is None and self._scanner.pairs:
            # Lazy rebuild if index is stale (e.g., pairs added since last recompute)
            self._pair_index = {p.event_ticker: p for p in self._scanner.pairs}
            result = self._pair_index.get(event_ticker)
        return result

    def _find_ledger_for_bid(self, bid: BidConfirmation) -> PositionLedger | None:
        """Look up the position ledger for a bid's event — O(1) via ticker index."""
        # Build ticker→event_ticker index lazily (keyed by ticker_a)
        if not hasattr(self, "_ticker_to_event") or not self._ticker_to_event:
            self._ticker_to_event = {p.ticker_a: p.event_ticker for p in self._scanner.pairs}
        event_ticker = self._ticker_to_event.get(bid.ticker_a)
        if event_ticker is None:
            return None
        try:
            return self._adjuster.get_ledger(event_ticker)
        except KeyError:
            return None

    def _compute_proposer_status(self, event_ticker: str) -> str:
        """Diagnose why the proposer isn't suggesting for this event."""
        if self.is_exit_only(event_ticker):
            return "EXIT"

        if not self._auto_config.enabled:
            return "Sug. off"

        opp = self._scanner.get_opportunity(event_ticker)
        if opp is None or opp.fee_edge < self._auto_config.edge_threshold_cents:
            return "Low edge"

        stability = self._proposer.stability_elapsed(event_ticker)
        if (
            stability is not None
            and self._auto_config.stability_seconds > 0
            and stability < self._auto_config.stability_seconds
        ):
            remaining = self._auto_config.stability_seconds - stability
            return f"Stable {remaining:.0f}s"

        cooldown = self._proposer.cooldown_elapsed(event_ticker)
        if cooldown is not None and cooldown < self._auto_config.rejection_cooldown_seconds:
            remaining = self._auto_config.rejection_cooldown_seconds - cooldown
            return f"Cooldown {remaining:.0f}s"

        return "Ready"

    def _recompute_positions(self) -> None:
        """Recompute position summaries from ledger state and enrich with status."""
        # Rebuild lookup indices — O(N) once, enables O(1) lookups throughout
        self._pair_index = {p.event_ticker: p for p in self._scanner.pairs}
        self._ticker_to_event = {p.ticker_a: p.event_ticker for p in self._scanner.pairs}

        self._position_summaries = compute_display_positions(
            self._adjuster.ledgers,
            self._scanner.pairs,
            self._queue_cache,
            self._cpm,
        )

        # Pre-build pending proposal kinds by event — O(P) once vs O(P×E) per event
        pending_by_event: dict[str, set[str]] = {}
        for p in self._proposal_queue.pending():
            pending_by_event.setdefault(p.key.event_ticker, set()).add(p.kind)
        self._pending_kinds_cache = pending_by_event

        for summary in self._position_summaries:
            summary.status = self._compute_event_status(summary.event_ticker)
            ep = self._event_positions.get(summary.event_ticker)
            if ep is not None:
                summary.kalshi_pnl = ep.realized_pnl

        # Compute status for ALL pairs — use dict instead of O(N²) scan
        summary_index = {s.event_ticker: s for s in self._position_summaries}
        self._event_statuses: dict[str, str] = {}
        for pair in self._scanner.pairs:
            existing = summary_index.get(pair.event_ticker)
            if existing is not None:
                self._event_statuses[pair.event_ticker] = existing.status
            else:
                self._event_statuses[pair.event_ticker] = self._compute_proposer_status(
                    pair.event_ticker
                )

    async def _recover_stale_books(self) -> None:
        """Resubscribe to any tickers with stale orderbooks.

        Books are considered stale if no snapshot or delta has been received
        within the staleness threshold (120s). Unsubscribe+resubscribe triggers
        a fresh snapshot from Kalshi, resetting the update timestamp.
        """
        stale = self._feed.book_manager.stale_tickers()
        if not stale:
            return
        active = set(self._active_market_tickers())
        for ticker in stale:
            if ticker not in active:
                continue
            try:
                await self._feed.unsubscribe(ticker)
                await self._feed.subscribe(ticker)
                logger.info("stale_book_recovered", ticker=ticker)
            except Exception:
                logger.warning("stale_book_recovery_failed", ticker=ticker, exc_info=True)

    def _active_market_tickers(self) -> list[str]:
        """Collect market tickers from all active scanner pairs."""
        tickers: list[str] = []
        for pair in self._scanner.pairs:
            tickers.append(pair.ticker_a)
            tickers.append(pair.ticker_b)
        return tickers

    def _notify(self, message: str, severity: str = "information", *, toast: bool = False) -> None:
        """Emit a notification to the UI if callback is set.

        By default, notifications go to the ActivityLog panel (zero asyncio
        overhead). Pass ``toast=True`` for critical errors or user-initiated
        action results that need an interruptive Textual toast.
        """
        if self.on_notification:
            self.on_notification(message, severity, toast)
