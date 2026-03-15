"""TradingEngine — central orchestrator for trading logic.

Owns all subsystem dependencies, mutable caches, and polling/action methods.
The TUI delegates to this engine rather than managing trading state directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from talos.automation_config import AutomationConfig
from talos.bid_adjuster import BidAdjuster
from talos.cpm import CPMTracker
from talos.fees import MAKER_FEE_RATE
from talos.game_manager import GameManager
from talos.game_status import GameStatusResolver
from talos.lifecycle_feed import LifecycleFeed
from talos.market_feed import MarketFeed
from talos.models.order import Order
from talos.models.portfolio import EventPosition, Position
from talos.models.position import EventPositionSummary
from talos.models.proposal import Proposal, ProposalKey
from talos.rebalance import compute_rebalance_proposal
from talos.rebalance import execute_rebalance as _execute_rebalance
from talos.models.ws import FillMessage, TickerMessage, UserOrderMessage
from talos.opportunity_proposer import OpportunityProposer
from talos.portfolio_feed import PortfolioFeed
from talos.position_feed import PositionFeed
from talos.position_ledger import PositionLedger, Side, compute_display_positions
from talos.proposal_queue import ProposalQueue
from talos.rest_client import KalshiRESTClient
from talos.scanner import ArbitrageScanner
from talos.ticker_feed import TickerFeed
from talos.top_of_market import TopOfMarketTracker

if TYPE_CHECKING:
    from talos.models.strategy import ArbPair, BidConfirmation

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
        data_collector: object | None = None,
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
        self.on_notification: Callable[[str, str], None] | None = None
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

    def get_ticker_data(self, ticker: str) -> TickerMessage | None:
        """Return the latest WS ticker data for a market, or None."""
        if self._ticker_feed is None:
            return None
        return self._ticker_feed.get_ticker(ticker)

    # ── Polling methods ─────────────────────────────────────────────

    async def start_feed(self) -> None:
        """Connect WebSocket, restore saved games, and listen."""
        try:
            await self._feed.connect()
            self._ws_connected = True
            self._notify("WebSocket connected")

            # Auto-discover events with positions or resting orders
            discovered = await self._discover_active_events()

            # Merge with saved games (union, deduplicate)
            all_tickers = list(dict.fromkeys(discovered + self._initial_games))

            from pathlib import Path as _PP
            import time as _st
            _perf = _PP("talos_perf.log").open("a")

            _s0 = _st.monotonic()

            # Fast restore from cached data (no REST calls)
            if self._initial_games_full:
                _perf.write(f"STARTUP: fast-restoring {len(self._initial_games_full)} games from cache...\n")
                _perf.flush()
                cached_tickers = set()
                pairs = []
                for data in self._initial_games_full:
                    try:
                        pair = self._game_manager.restore_game(data)
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
                self._initial_games_full = None
                # Only REST-fetch discovered events not already in cache
                all_tickers = [t for t in all_tickers if t not in cached_tickers]

            if all_tickers:
                _perf.write(f"STARTUP: adding {len(all_tickers)} games via REST...\n")
                _perf.flush()
                pairs = await self._game_manager.add_games(all_tickers)
                for pair in pairs:
                    self._adjuster.add_event(pair)
                if pairs:
                    self._notify(f"Loaded {len(pairs)} game(s)")
                self._initial_games.clear()

            _s1 = _st.monotonic()
            _perf.write(f"STARTUP: add_games done in {round((_s1 - _s0) * 1000)}ms\n")
            _perf.flush()

            # Resolve game status — run in background, don't block startup
            if self._game_status_resolver is not None:
                batch = [
                    (p.event_ticker, self._game_manager.subtitles.get(p.event_ticker, ""))
                    for p in self._game_manager.active_games
                ]
                if batch:
                    # Don't await — let it run while WS starts listening
                    import asyncio as _aio
                    _aio.create_task(self._game_status_resolver.resolve_batch(batch))

            _perf.write(f"STARTUP: game_status fired as background task\n")
            _perf.flush()

            # Subscribe to portfolio events globally (all markets)
            if self._portfolio_feed is not None:
                await self._portfolio_feed.subscribe()
            if self._lifecycle_feed is not None:
                await self._lifecycle_feed.subscribe()
            if self._position_feed is not None:
                await self._position_feed.subscribe()

            _s3 = _st.monotonic()
            _perf.write(f"STARTUP: ws_subs done in {round((_s3 - _s1) * 1000)}ms\n")
            _perf.flush()

            # Subscribe to ticker updates for all active markets
            if self._ticker_feed is not None:
                market_tickers = self._active_market_tickers()
                _perf.write(f"STARTUP: subscribing to {len(market_tickers)} ticker markets...\n")
                _perf.flush()
                if market_tickers:
                    await self._ticker_feed.subscribe(market_tickers)

            _s4 = _st.monotonic()
            _perf.write(f"STARTUP: ticker_sub done in {round((_s4 - _s3) * 1000)}ms\n")
            _perf.write(f"STARTUP: total={round((_s4 - _s0) * 1000)}ms pairs={len(self._game_manager.active_games)}\n")
            _perf.write(f"STARTUP: entering WS listen loop\n")
            _perf.flush()

            await self._feed.start()
            # If we reach here without exception, the WS exited cleanly
            self._ws_connected = False
            self._notify("WEBSOCKET DISCONNECTED — prices are stale!", "error")
            from pathlib import Path
            Path("talos_perf.log").open("a").write(
                f"WS_CLEAN_EXIT: listen loop ended without exception\n"
            )
        except Exception as e:
            self._ws_connected = False
            self._notify(f"WEBSOCKET DISCONNECTED: {e}", "error")
            from pathlib import Path
            Path("talos_perf.log").open("a").write(
                f"WS_EXCEPTION: {type(e).__name__}: {e}\n"
            )

        # Auto-reconnect after disconnect (wait 5s then retry)
        if not self._ws_connected:
            import asyncio as _aio
            from pathlib import Path as _P
            _P("talos_perf.log").open("a").write("WS_RECONNECTING: waiting 5s...\n")
            self._notify("Reconnecting WebSocket in 5s...", "warning")
            await _aio.sleep(5)
            await self.start_feed()

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
        import time as _time
        _t0 = _time.monotonic()

        await self._recover_stale_books()

        # Bump sync generation so optimistic placements from this cycle
        # are protected against stale-data overwrites.
        for pair in self._scanner.pairs:
            try:
                self._adjuster.get_ledger(pair.event_ticker).bump_sync_gen()
            except KeyError:
                pass

        try:
            _t2 = _time.monotonic()
            # Only fetch resting orders — fill data comes from positions API.
            # "executed" (1500+) and "canceled" (250+) are historical noise.
            orders = await self._rest.get_all_orders(status="resting")
            _t3 = _time.monotonic()
            self._orders_cache = orders

            # Update top-of-market tracker with current orders
            self._tracker.update_orders(orders, self._scanner.pairs)

            # Re-check all tracked tickers against the live book so the
            # _at_top cache reflects current state, not stale WS events.
            for ticker in self._tracker.resting_tickers:
                self._tracker.check(ticker)

            # Fetch queue positions and merge into cache
            try:
                tickers = self._active_market_tickers()
                if tickers:
                    new_qp = await self._rest.get_queue_positions(market_tickers=tickers)
                    for oid, qp in new_qp.items():
                        if qp > 0:
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

            _t4 = _time.monotonic()
            # Augment fills from positions API (P7/P15 — Kalshi is source
            # of truth, always). GET /portfolio/orders archives old orders,
            # but GET /portfolio/positions never does. This catches fills
            # invisible to sync_from_orders due to order archival.
            pos_map: dict[str, Position] = {}
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
                    ledger.sync_from_positions(fills, costs)
            except Exception:
                logger.warning("positions_sync_failed", exc_info=True)

            _t5 = _time.monotonic()
            self._recompute_positions()
            _t6 = _time.monotonic()

            _timing = (
                f"refresh_account: total={round((_t6 - _t0) * 1000)}ms "
                f"orders={round((_t3 - _t2) * 1000)}ms({len(orders)}) "
                f"sync={round((_t4 - _t3) * 1000)}ms "
                f"positions={round((_t5 - _t4) * 1000)}ms "
                f"recompute={round((_t6 - _t5) * 1000)}ms "
                f"pairs={len(self._scanner.pairs)}\n"
            )
            from pathlib import Path
            with Path("talos_perf.log").open("a") as _f:
                _f.write(_timing)

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
            self._reconcile_with_kalshi(orders, pos_map)

            # Re-evaluate jumped tickers that have no pending proposal (P20)
            self.reevaluate_jumps()

            # Check for position imbalances (P16)
            self.check_imbalances()

            # Evaluate scanner opportunities for automated bid proposals
            self.evaluate_opportunities()
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
                if qp > 0:
                    self._queue_cache[oid] = _merge_queue(self._queue_cache.get(oid), qp)
        except Exception:
            logger.debug("queue_poll_failed")
            return

        if not self._orders_cache:
            return

        self._recompute_positions()

    async def refresh_trades(self) -> None:
        """Fetch recent trades for CPM tracking."""
        tickers = self._active_market_tickers()
        if not tickers:
            return

        async def _fetch(ticker: str) -> tuple[str, list] | None:
            try:
                trades = await self._rest.get_trades(ticker, limit=50)
                return (ticker, trades)
            except Exception:
                logger.warning("trade_fetch_failed", ticker=ticker, exc_info=True)
                return None

        results = await asyncio.gather(*[_fetch(t) for t in tickers])
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
                    self._data_collector.log_order(
                        event_ticker=event_ticker,
                        order_id=msg.order_id,
                        ticker=msg.ticker,
                        side=msg.side,
                        status=msg.status,
                        price=msg.no_price,
                        initial_count=msg.fill_count + msg.remaining_count,
                        fill_count=msg.fill_count,
                        remaining_count=msg.remaining_count,
                        maker_fill_cost=msg.maker_fill_cost,
                        maker_fees=msg.maker_fees,
                        source="ws_update",
                    )
                return

        # Order not in cache — add it so WS is self-sufficient
        if msg.side == "no" and msg.status in ("resting", "executed"):
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
                if both_done and self._data_collector is not None:
                    self._log_event_outcome(event_ticker, pair)

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
        except Exception:
            logger.warning("settlement_fetch_failed", ticker=ticker, exc_info=True)

    def _log_event_outcome(self, event_ticker: str, pair: ArbPair) -> None:
        """Log the final outcome of an event with trap analysis."""
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

        # Compute revenue: NO side wins → payout = count * 100 cents
        revenue = 0
        if result_a == "no":
            revenue += filled_a * 100
        if result_b == "no":
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

            # ── Compute ground truth from Kalshi orders ──
            kalshi_fills: dict[Side, int] = {Side.A: 0, Side.B: 0}
            kalshi_resting: dict[Side, int] = {Side.A: 0, Side.B: 0}
            kalshi_resting_order_count: dict[Side, int] = {Side.A: 0, Side.B: 0}

            for order in orders:
                if order.side != "no" or order.action != "buy":
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
            auth_fills = {
                s: max(kalshi_fills[s], pos_fills[s]) for s in (Side.A, Side.B)
            }

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
                    self._notify(msg, "error")

                # Check 2: Multiple resting orders (double-bid indicator)
                if kalshi_resting_order_count[side] > 1:
                    msg = (
                        f"MULTI-ORDER {name} {sl}: "
                        f"{kalshi_resting_order_count[side]} resting orders "
                        f"({kalshi_resting[side]} total contracts)"
                    )
                    logger.warning(
                        "reconcile_multiple_resting",
                        event_ticker=pair.event_ticker,
                        side=sl,
                        order_count=kalshi_resting_order_count[side],
                        total_resting=kalshi_resting[side],
                    )
                    self._notify(msg, "warning")

                # Check 3: Fill consistency between orders and positions APIs
                if pos_fills[side] > 0 and kalshi_fills[side] > 0:
                    if pos_fills[side] != kalshi_fills[side]:
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
                if ledger._sides[side]._placed_at_gen is None:
                    if ledger_resting != kalshi_resting[side]:
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

    def on_top_of_market_change(self, ticker: str, at_top: bool) -> None:
        """Handle top-of-market state transition — evaluate adjustment."""
        resting = self._tracker.resting_price(ticker)
        evt = self._adjuster.resolve_event(ticker)
        label = self._display_name(evt) if evt else ticker
        if at_top:
            self._notify(f"Back at top: {label} ({resting}c)")
        else:
            top_price = self._tracker.book_top_price(ticker) or "?"
            self._notify(
                f"Jumped: {label} (you: {resting}c, top: {top_price}c)",
                "warning",
            )

        self._generate_jump_proposal(ticker, at_top=at_top)

    def _generate_jump_proposal(self, ticker: str, *, at_top: bool = False) -> None:
        """Evaluate a jump and enqueue a proposal if appropriate.

        Shared by on_top_of_market_change (with toast) and reevaluate_jumps
        (silent). The notification is the caller's responsibility.
        """
        proposal = self._adjuster.evaluate_jump(ticker, at_top)
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
                k.event_ticker == pair.event_ticker and k.kind == "withdraw"
                for k in pending_keys
            )
            if has_withdraw:
                continue
            for ticker, side_label in [
                (pair.ticker_a, "A"),
                (pair.ticker_b, "B"),
            ]:
                at_top = self._tracker.is_at_top(ticker)
                if at_top is not None and not at_top:
                    # Jumped — check if there's already a proposal for this side
                    has_proposal = any(
                        k.event_ticker == pair.event_ticker
                        and k.side == side_label
                        and k.kind in ("adjustment", "hold")
                        for k in pending_keys
                    )
                    if not has_proposal:
                        self._generate_jump_proposal(ticker)

    def check_imbalances(self) -> None:
        """Detect position imbalances and propose rebalancing (P16).

        Delegates to compute_rebalance_proposal() for pure detection logic.
        """
        pending_keys = {p.key for p in self._proposal_queue.pending()}
        for pair in self._scanner.pairs:
            try:
                ledger = self._adjuster.get_ledger(pair.event_ticker)
            except KeyError:
                continue

            # Skip if a rebalance proposal already covers this event
            has_rebalance = any(
                k.event_ticker == pair.event_ticker and k.kind == "rebalance"
                for k in pending_keys
            )
            if has_rebalance:
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
            if proposal is not None:
                self._proposal_queue.add(proposal)
                pending_keys.add(proposal.key)

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
            )
            if proposal is not None:
                self._proposal_queue.add(proposal)
                pending_keys.add(proposal.key)

    # ── Action methods ──────────────────────────────────────────────

    async def place_bids(self, bid: BidConfirmation) -> None:
        """Place NO orders on both legs.

        Safety: checks is_placement_safe() on both sides before sending orders.
        After placement, optimistically updates the ledger via record_placement()
        (with generation-based stale-sync guard) and appends to _orders_cache.
        """
        # Block on paused markets
        for ticker in (bid.ticker_a, bid.ticker_b):
            if ticker in self._paused_markets:
                evt = self._adjuster.resolve_event(ticker)
                label = self._display_name(evt) if evt else ticker
                self._notify(f"Bid BLOCKED {label}: {ticker} is paused", "error")
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
                        f"Bid BLOCKED {name} (side {side.value}): {reason}", "error"
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
            self._notify(
                f"Orders placed: {bid.ticker_a} @ {bid.no_a}c, {bid.ticker_b} @ {bid.no_b}c",
            )

            # Optimistic ledger update — prevents duplicate proposals when a
            # concurrent refresh_account has stale data (the orders weren't in
            # the API response it fetched before placement). The generation
            # guard in sync_from_orders prevents stale syncs from clearing this.
            if ledger is not None:
                ledger.record_placement(
                    Side.A, order_a.order_id, order_a.remaining_count, bid.no_a,
                )
                ledger.record_placement(
                    Side.B, order_b.order_id, order_b.remaining_count, bid.no_b,
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
                    self._data_collector.log_order(
                        event_ticker=bid.ticker_a.rsplit("-", 1)[0] if "-" in bid.ticker_a else "",
                        order_id=order.order_id,
                        ticker=order.ticker,
                        side=order.side,
                        status=order.status,
                        price=order.no_price,
                        initial_count=order.initial_count,
                        fill_count=order.fill_count,
                        remaining_count=order.remaining_count,
                        source="auto_accept" if self._auto_config.enabled else "manual",
                    )
        except Exception as e:
            self._notify(f"Order error: {type(e).__name__}: {e}", "error")
            logger.exception("place_bids_error")

    async def add_games(self, urls: list[str], source: str = "scan") -> None:
        """Add games by URL."""
        try:
            pairs = await self._game_manager.add_games(urls)
            for pair in pairs:
                self._adjuster.add_event(pair)
            if self._game_status_resolver is not None:
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
                    gs = self._game_status_resolver.get(pair.event_ticker) if self._game_status_resolver else None
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
                        scheduled_start=gs.scheduled_start.isoformat() if gs and gs.scheduled_start else None,
                    )
            self._notify(f"Added {len(urls)} game(s)")
        except Exception as e:
            self._notify(f"Error: {e}", "error")
            logger.exception("add_games_error")

    async def remove_game(self, event_ticker: str) -> None:
        """Remove a game from monitoring."""
        try:
            if self._game_status_resolver is not None:
                self._game_status_resolver.remove(event_ticker)
            await self._game_manager.remove_game(event_ticker)
            self._notify(f"Removed {event_ticker}")
        except Exception as e:
            self._notify(f"Error: {e}", "error")

    async def clear_games(self) -> None:
        """Clear all monitored games."""
        try:
            count = len(self._game_manager.active_games)
            if self._game_status_resolver is not None:
                for pair in self._game_manager.active_games:
                    self._game_status_resolver.remove(pair.event_ticker)
            await self._game_manager.clear_all_games()
            self._notify(f"Cleared {count} game(s)")
        except Exception as e:
            self._notify(f"Error: {e}", "error")

    async def refresh_game_status(self) -> None:
        """Hourly: re-fetch game status for all active events."""
        if self._game_status_resolver is not None:
            await self._game_status_resolver.refresh_all()

    async def approve_proposal(self, key: ProposalKey) -> None:
        """Approve and execute a queued proposal."""
        try:
            envelope = self._proposal_queue.approve(key)
        except KeyError:
            self._notify("No pending proposal to approve", "warning")
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
                adj_name = self._display_name(envelope.adjustment.event_ticker)
                self._notify(
                    f"Adjusted: {adj_name}"
                    f" {envelope.adjustment.side}"
                    f" \u2192 {envelope.adjustment.new_price}c",
                )
            except Exception as e:
                self._notify(f"Adjustment FAILED: {type(e).__name__}: {e}", "error")
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
            self._notify(f"Withdraw FAILED: no ledger for {event_ticker}", "error")
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
            orders_a = await self._rest.get_orders(ticker=pair.ticker_a, limit=200)
            orders_b = await self._rest.get_orders(ticker=pair.ticker_b, limit=200)
            orders = orders_a + orders_b
            ledger = self._adjuster.get_ledger(event_ticker)
            ledger.sync_from_orders(orders, ticker_a=pair.ticker_a, ticker_b=pair.ticker_b)
            positions = await self._rest.get_positions(limit=200)
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
            ledger.sync_from_positions(fills, costs)
            logger.info(
                "post_action_verify",
                event_ticker=event_ticker,
                committed_a=ledger.total_committed(Side.A),
                committed_b=ledger.total_committed(Side.B),
                delta=ledger.current_delta(),
            )
        except Exception:
            logger.warning(
                "post_action_verify_failed",
                event_ticker=event_ticker,
                exc_info=True,
            )
            name = self._display_name(event_ticker)
            self._notify(
                f"Verify FAILED for {name} — position data may be stale",
                "warning",
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
                if not books.best_ask(pair.ticker_a) and not books.best_ask(pair.ticker_b):
                    return "Settled"

        # Jumped — resting orders not at top of market
        if resting_a > 0 or resting_b > 0:
            pair = self._find_pair(event_ticker)
            if pair is not None:
                jumped_a = resting_a > 0 and self._tracker.is_at_top(pair.ticker_a) is False
                jumped_b = resting_b > 0 and self._tracker.is_at_top(pair.ticker_b) is False
                if jumped_a or jumped_b:
                    sides = ""
                    if jumped_a:
                        sides += "A"
                    if jumped_b:
                        sides += "B"
                    return f"Jumped {sides}"

        # Check if pending proposal exists
        pending_kinds = set()
        for p in self._proposal_queue.pending():
            if p.key.event_ticker == event_ticker:
                pending_kinds.add(p.kind)

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
        """Look up scanner pair by event ticker."""
        for pair in self._scanner.pairs:
            if pair.event_ticker == event_ticker:
                return pair
        return None


    def _find_ledger_for_bid(self, bid: BidConfirmation) -> PositionLedger | None:
        """Look up the position ledger for a bid's event."""
        for pair in self._scanner.pairs:
            if pair.ticker_a == bid.ticker_a:
                try:
                    return self._adjuster.get_ledger(pair.event_ticker)
                except KeyError:
                    return None
        return None

    def _compute_proposer_status(self, event_ticker: str) -> str:
        """Diagnose why the proposer isn't suggesting for this event."""
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
        self._position_summaries = compute_display_positions(
            self._adjuster.ledgers,
            self._scanner.pairs,
            self._queue_cache,
            self._cpm,
        )
        for summary in self._position_summaries:
            summary.status = self._compute_event_status(summary.event_ticker)
            ep = self._event_positions.get(summary.event_ticker)
            if ep is not None:
                summary.kalshi_pnl = ep.realized_pnl

        # Compute status for ALL pairs (including those without positions)
        self._event_statuses: dict[str, str] = {}
        for pair in self._scanner.pairs:
            # Events with position summaries already have status set above
            existing = next(
                (s for s in self._position_summaries if s.event_ticker == pair.event_ticker),
                None,
            )
            if existing is not None:
                self._event_statuses[pair.event_ticker] = existing.status
            else:
                self._event_statuses[pair.event_ticker] = self._compute_proposer_status(
                    pair.event_ticker
                )

    async def _recover_stale_books(self) -> None:
        """Resubscribe to any tickers with stale orderbooks.

        A single dropped WS message marks a book stale permanently.
        Unsubscribe+resubscribe triggers a fresh snapshot from Kalshi,
        which resets the stale flag and restores the live delta stream.
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

    def _notify(self, message: str, severity: str = "information") -> None:
        """Emit a notification to the UI if callback is set."""
        if self.on_notification:
            self.on_notification(message, severity)
