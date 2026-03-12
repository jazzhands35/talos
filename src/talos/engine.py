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
from talos.errors import KalshiAPIError
from talos.game_manager import GameManager
from talos.lifecycle_feed import LifecycleFeed
from talos.market_feed import MarketFeed
from talos.models.order import Order
from talos.models.portfolio import EventPosition
from talos.models.position import EventPositionSummary
from talos.models.proposal import Proposal, ProposalKey, ProposedRebalance
from talos.models.ws import FillMessage, TickerMessage, UserOrderMessage
from talos.opportunity_proposer import OpportunityProposer
from talos.portfolio_feed import PortfolioFeed
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


def _is_no_op(err: KalshiAPIError) -> bool:
    """Check if the API error is a no-op amend (desired state already reached)."""
    if isinstance(err.body, dict):
        inner = err.body.get("error", {})
        if isinstance(inner, dict):
            return inner.get("code") == "AMEND_ORDER_NO_OP"
    return False


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
        proposal_queue: ProposalQueue | None = None,
        automation_config: AutomationConfig | None = None,
        portfolio_feed: PortfolioFeed | None = None,
        ticker_feed: TickerFeed | None = None,
        lifecycle_feed: LifecycleFeed | None = None,
    ) -> None:
        self._scanner = scanner
        self._game_manager = game_manager
        self._rest = rest_client
        self._feed = market_feed
        self._tracker = tracker
        self._adjuster = adjuster
        self._initial_games = list(initial_games or [])
        self._proposal_queue = proposal_queue or ProposalQueue()
        self._auto_config = automation_config or AutomationConfig()
        self._portfolio_feed = portfolio_feed
        self._ticker_feed = ticker_feed
        self._lifecycle_feed = lifecycle_feed
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

        # Wire portfolio feed callbacks
        if self._portfolio_feed is not None:
            self._portfolio_feed.on_order_update = self._on_order_update
            self._portfolio_feed.on_fill = self._on_fill

        # Wire lifecycle feed callbacks
        if self._lifecycle_feed is not None:
            self._lifecycle_feed.on_determined = self._on_market_determined
            self._lifecycle_feed.on_settled = self._on_market_settled
            self._lifecycle_feed.on_paused = self._on_market_paused

        # Callbacks for UI communication
        self.on_notification: Callable[[str, str], None] | None = None

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
            self._notify("WebSocket connected")

            # Auto-discover events with positions or resting orders
            discovered = await self._discover_active_events()

            # Merge with saved games (union, deduplicate)
            all_tickers = list(dict.fromkeys(discovered + self._initial_games))

            if all_tickers:
                restored = 0
                for ticker in all_tickers:
                    try:
                        pair = await self._game_manager.add_game(ticker)
                        self._adjuster.add_event(pair)
                        restored += 1
                    except Exception:
                        logger.warning("restore_game_failed", game=ticker)
                if restored:
                    self._notify(f"Loaded {restored} game(s)")
                self._initial_games.clear()
            # Subscribe to portfolio events globally (all markets)
            if self._portfolio_feed is not None:
                await self._portfolio_feed.subscribe()

            # Subscribe to lifecycle events globally
            if self._lifecycle_feed is not None:
                await self._lifecycle_feed.subscribe()

            # Subscribe to ticker updates for all active markets
            if self._ticker_feed is not None:
                market_tickers = self._active_market_tickers()
                if market_tickers:
                    await self._ticker_feed.subscribe(market_tickers)

            await self._feed.start()
        except Exception as e:
            logger.exception("feed_connection_error")
            self._notify(f"WebSocket error: {type(e).__name__}: {e}", "error")

    async def refresh_account(self) -> None:
        """Fetch balance + orders, sync ledgers, compute positions."""
        await self._recover_stale_books()
        try:
            balance = await self._rest.get_balance()
            self._balance = balance.balance
            self._portfolio_value = balance.portfolio_value

            orders = await self._rest.get_orders(limit=200)
            self._orders_cache = orders

            # Update top-of-market tracker with current orders
            self._tracker.update_orders(orders, self._scanner.pairs)

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

            # Augment fills from positions API (P7/P15 — Kalshi is source
            # of truth, always). GET /portfolio/orders archives old orders,
            # but GET /portfolio/positions never does. This catches fills
            # invisible to sync_from_orders due to order archival.
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
                return

        # Order not in cache — will be picked up by next REST poll
        logger.debug(
            "ws_order_update_unknown",
            order_id=msg.order_id,
            ticker=msg.ticker,
            status=msg.status,
        )

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

    # ── Lifecycle event handlers ────────────────────────────────

    def _on_market_determined(self, ticker: str, result: str, settlement_value: int) -> None:
        """Handle market determination (result known, not yet settled)."""
        logger.info(
            "lifecycle_determined",
            ticker=ticker,
            result=result,
            settlement_value=settlement_value,
        )
        self._notify(f"Market determined: {ticker} → {result}")

    def _on_market_settled(self, ticker: str) -> None:
        """Handle market settlement (cash distributed)."""
        logger.info("lifecycle_settled", ticker=ticker)
        self._notify(f"Market settled: {ticker}")

    def _on_market_paused(self, ticker: str, is_deactivated: bool) -> None:
        """Handle market pause/unpause."""
        if is_deactivated:
            self._paused_markets.add(ticker)
            self._notify(f"Market paused: {ticker}", "warning")
        else:
            self._paused_markets.discard(ticker)
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

    # ── Event handlers ───────────────────────────────────────────

    def on_top_of_market_change(self, ticker: str, at_top: bool) -> None:
        """Handle top-of-market state transition — evaluate adjustment."""
        resting = self._tracker.resting_price(ticker)
        if at_top:
            self._notify(f"Back at top: {ticker} ({resting}c)")
        else:
            top_price = self._tracker.book_top_price(ticker) or "?"
            self._notify(
                f"Jumped: {ticker} (you: {resting}c, top: {top_price}c)",
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

            if proposal.action == "hold":
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
                side=proposal.side,
                kind=kind,
            )
            envelope = Proposal(
                key=key,
                kind=kind,
                summary=summary,
                detail=proposal.reason,
                created_at=datetime.now(UTC),
                adjustment=proposal if proposal.action != "hold" else None,
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

        Two-step rebalance plan to maintain delta neutrality at every step:
        1. Reduce over-side resting (cancel/amend) — shrinks the larger side
        2. Catch-up bid on under-side — grows the smaller side
        Step 1 always executes before step 2 (delta-neutral at each point).
        """
        pending_keys = {p.key for p in self._proposal_queue.pending()}
        for pair in self._scanner.pairs:
            try:
                ledger = self._adjuster.get_ledger(pair.event_ticker)
            except KeyError:
                continue

            committed_a = ledger.total_committed(Side.A)
            committed_b = ledger.total_committed(Side.B)

            if committed_a == 0 and committed_b == 0:
                continue

            # No resting orders + fills balanced → event settled, nothing actionable
            if (
                ledger.resting_count(Side.A) == 0
                and ledger.resting_count(Side.B) == 0
                and ledger.filled_count(Side.A) == ledger.filled_count(Side.B)
            ):
                continue

            # No resting + markets closed → settled with imbalance, nothing actionable
            if ledger.resting_count(Side.A) == 0 and ledger.resting_count(Side.B) == 0:
                books = self._feed.book_manager
                if not books.best_ask(pair.ticker_a) and not books.best_ask(pair.ticker_b):
                    continue

            delta = committed_a - committed_b
            if abs(delta) < ledger.unit_size:
                continue  # Less than one unit — normal

            # Determine over-extended side
            if delta > 0:
                over, under = Side.A, Side.B
                over_committed, under_committed = committed_a, committed_b
            else:
                over, under = Side.B, Side.A
                over_committed, under_committed = committed_b, committed_a

            # Skip if we already have a rebalance proposal for this side
            has_proposal = any(
                k.event_ticker == pair.event_ticker
                and k.side == over.value
                and k.kind == "rebalance"
                for k in pending_keys
            )
            if has_proposal:
                continue

            over_resting = ledger.resting_count(over)
            over_filled = ledger.filled_count(over)
            under_resting = ledger.resting_count(under)

            evt = pair.event_ticker
            name = self._display_name(evt)
            over_ticker = pair.ticker_a if over == Side.A else pair.ticker_b
            under_ticker = pair.ticker_a if under == Side.A else pair.ticker_b
            over_order_id = ledger.resting_order_id(over)

            # Equalize: both sides converge to max(over_filled, under_committed)
            target = max(over_filled, under_committed)
            target_over_resting = max(0, target - over_filled)
            reduce_by = over_resting - target_over_resting

            # Step 2: catch-up on under-side (only if no resting already there)
            gap = target - under_committed
            catchup_qty = 0
            catchup_price = 0
            catchup_ticker: str | None = None
            if gap > 0 and under_resting == 0:
                catchup_qty = min(gap, ledger.unit_size)
                catchup_ticker = under_ticker
                # Get current price from scanner snapshot
                snapshot = self._scanner.all_snapshots.get(pair.event_ticker)
                if snapshot is not None:
                    catchup_price = snapshot.no_a if under == Side.A else snapshot.no_b
                if snapshot is None or catchup_price <= 0:
                    catchup_qty = 0  # Can't determine price — skip catch-up
                    catchup_ticker = None

            # Build step descriptions for the detail text
            steps: list[str] = []
            if reduce_by > 0:
                if target_over_resting == 0:
                    steps.append(f"Cancel {over_resting} resting on {over.value}")
                else:
                    steps.append(
                        f"Reduce {over.value} resting {over_resting} → {target_over_resting}"
                    )
            if catchup_qty > 0:
                steps.append(f"Place {catchup_qty} @ {catchup_price}c on {under.value}")
            if not steps:
                steps.append(
                    f"Side {over.value} has {over_filled} fills vs "
                    f"side {under.value} {ledger.filled_count(under)} "
                    f"(under-side has {under_resting} resting — wait or adjust)"
                )

            logger.warning(
                "position_imbalance",
                event_ticker=evt,
                over_side=over.value,
                committed_over=over_committed,
                committed_under=under_committed,
                delta=abs(delta),
            )

            # Build rebalance data if we have any executable step
            rebalance_data = None
            has_reduce = reduce_by > 0 and over_order_id is not None
            has_catchup = catchup_qty > 0 and catchup_ticker is not None
            if has_reduce or has_catchup:
                rebalance_data = ProposedRebalance(
                    event_ticker=evt,
                    side=over.value,
                    order_id=over_order_id if has_reduce else None,
                    ticker=over_ticker if has_reduce else None,
                    current_resting=over_resting if has_reduce else 0,
                    target_resting=target_over_resting if has_reduce else 0,
                    resting_price=ledger.resting_price(over) if has_reduce else 0,
                    filled_count=over_filled if has_reduce else 0,
                    catchup_ticker=catchup_ticker if has_catchup else None,
                    catchup_price=catchup_price if has_catchup else 0,
                    catchup_qty=catchup_qty if has_catchup else 0,
                )

            key = ProposalKey(
                event_ticker=evt,
                side=over.value,
                kind="rebalance",
            )
            envelope = Proposal(
                key=key,
                kind="rebalance",
                summary=f"REBALANCE {name} side {over.value}",
                detail=(
                    f"Imbalanced: {over.value}={over_committed} vs "
                    f"{under.value}={under_committed} "
                    f"(delta {abs(delta)}). {'; '.join(steps)}"
                ),
                created_at=datetime.now(UTC),
                rebalance=rebalance_data,
            )
            self._proposal_queue.add(envelope)
            pending_keys.add(key)

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
        Ledger is NOT optimistically updated — Kalshi is source of truth (P7/P21),
        and sync_from_orders reconciles on the next polling cycle. The stability
        reset (fix #3) provides the time buffer to prevent duplicate proposals.
        """
        # Block on paused markets
        for ticker in (bid.ticker_a, bid.ticker_b):
            if ticker in self._paused_markets:
                self._notify(f"Bid BLOCKED: {ticker} is paused", "error")
                logger.error("bid_blocked_market_paused", ticker=ticker)
                return

        # Look up ledger for safety gate
        ledger = self._find_ledger_for_bid(bid)

        # Hard safety gate (P16, P18) — blocks if unit exceeded or arb unprofitable
        if ledger is not None:
            for side, price in [(Side.A, bid.no_a), (Side.B, bid.no_b)]:
                ok, reason = ledger.is_placement_safe(side, bid.qty, price)
                if not ok:
                    self._notify(f"Bid BLOCKED (side {side.value}): {reason}", "error")
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
        except Exception as e:
            self._notify(f"Order error: {type(e).__name__}: {e}", "error")
            logger.exception("place_bids_error")

    async def add_games(self, urls: list[str]) -> None:
        """Add games by URL."""
        try:
            pairs = await self._game_manager.add_games(urls)
            for pair in pairs:
                self._adjuster.add_event(pair)
            self._notify(f"Added {len(urls)} game(s)")
        except Exception as e:
            self._notify(f"Error: {e}", "error")
            logger.exception("add_games_error")

    async def remove_game(self, event_ticker: str) -> None:
        """Remove a game from monitoring."""
        try:
            await self._game_manager.remove_game(event_ticker)
            self._notify(f"Removed {event_ticker}")
        except Exception as e:
            self._notify(f"Error: {e}", "error")

    async def clear_games(self) -> None:
        """Clear all monitored games."""
        try:
            count = len(self._game_manager.active_games)
            await self._game_manager.clear_all_games()
            self._notify(f"Cleared {count} game(s)")
        except Exception as e:
            self._notify(f"Error: {e}", "error")

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

        if envelope.kind == "rebalance":
            if envelope.rebalance is not None:
                await self._execute_rebalance(envelope.rebalance)
            else:
                self._notify(f"Acknowledged: {envelope.summary} (manual action needed)")
            return

        if envelope.kind == "adjustment" and envelope.adjustment is not None:
            try:
                await self._adjuster.execute(envelope.adjustment, self._rest)
                self._notify(
                    f"Adjusted: {envelope.adjustment.event_ticker}"
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
        self._notify(f"Rejected: {key.event_ticker} {key.kind}")

    async def _execute_rebalance(self, rebalance: ProposedRebalance) -> None:
        """Execute a two-step rebalance: reduce over-side, then catch up under-side.

        Step 1 (reduce) always runs before step 2 (catch-up) to maintain
        delta neutrality at every intermediate state.
        """
        # Step 1: Reduce over-side resting
        has_reduce = (
            rebalance.order_id is not None
            and rebalance.ticker is not None
            and rebalance.current_resting > rebalance.target_resting
        )
        if has_reduce:
            try:
                assert rebalance.order_id is not None  # guarded by has_reduce
                assert rebalance.ticker is not None
                if rebalance.target_resting == 0:
                    await self._rest.cancel_order(rebalance.order_id)
                    self._notify(
                        f"Rebalance step 1: cancelled {rebalance.current_resting}"
                        f" resting on {rebalance.side} ({rebalance.ticker})",
                    )
                else:
                    # Use decrease_order for quantity-only reductions (preserves
                    # queue position, simpler semantics than amend).
                    fresh_order = await self._rest.get_order(rebalance.order_id)
                    if fresh_order.remaining_count <= rebalance.target_resting:
                        self._notify(
                            f"Rebalance step 1: already at target"
                            f" (remaining={fresh_order.remaining_count})",
                        )
                    else:
                        logger.info(
                            "rebalance_decrease",
                            order_id=rebalance.order_id,
                            order_remaining=fresh_order.remaining_count,
                            target_resting=rebalance.target_resting,
                        )
                        await self._rest.decrease_order(
                            rebalance.order_id,
                            reduce_to=rebalance.target_resting,
                        )
                        self._notify(
                            f"Rebalance step 1: {rebalance.side} resting"
                            f" {fresh_order.remaining_count}"
                            f" \u2192 {rebalance.target_resting}",
                        )
            except KalshiAPIError as e:
                if _is_no_op(e):
                    # Order already at desired state (fills happened between
                    # proposal and execution). Treat as success — proceed to
                    # step 2.
                    self._notify(
                        "Rebalance step 1: already at target (no-op)",
                    )
                else:
                    self._notify(
                        f"Rebalance FAILED (reduce): {e}",
                        "error",
                    )
                    logger.exception(
                        "rebalance_reduce_error",
                        event_ticker=rebalance.event_ticker,
                        side=rebalance.side,
                        order_id=rebalance.order_id,
                    )
                    return  # Don't proceed to catch-up if reduce failed
            except Exception as e:
                self._notify(
                    f"Rebalance FAILED (reduce): {type(e).__name__}: {e}",
                    "error",
                )
                logger.exception(
                    "rebalance_reduce_error",
                    event_ticker=rebalance.event_ticker,
                    side=rebalance.side,
                    order_id=rebalance.order_id,
                )
                return  # Don't proceed to catch-up if reduce failed

        # Step 2: Catch-up bid on under-side
        if rebalance.catchup_ticker and rebalance.catchup_qty > 0:
            under_side = Side.A if rebalance.side == "B" else Side.B

            # Fresh sync from Kalshi before placing (P7/P21 — Kalshi is ALWAYS
            # source of truth). The proposal was computed from potentially stale
            # ledger data. Re-fetch orders and re-verify the imbalance exists.
            pair = self._find_pair(rebalance.event_ticker)
            if pair is None:
                self._notify("Catch-up BLOCKED: pair not found", "error")
                return

            try:
                orders = await self._rest.get_orders(limit=200)
                ledger = self._adjuster.get_ledger(rebalance.event_ticker)
                ledger.sync_from_orders(orders, ticker_a=pair.ticker_a, ticker_b=pair.ticker_b)
            except Exception:
                logger.warning(
                    "rebalance_fresh_sync_failed",
                    event_ticker=rebalance.event_ticker,
                    exc_info=True,
                )
                self._notify("Catch-up BLOCKED: fresh sync failed", "error")
                return

            # Re-check: is there still an imbalance worth catching up?
            over_side = Side.A if rebalance.side == "A" else Side.B
            fresh_over = ledger.total_committed(over_side)
            fresh_under = ledger.total_committed(under_side)
            fresh_delta = fresh_over - fresh_under
            if fresh_delta < ledger.unit_size:
                self._notify(
                    f"Catch-up skipped — fresh sync shows delta"
                    f" {fresh_delta} (< {ledger.unit_size})",
                )
                logger.info(
                    "rebalance_catchup_skipped_after_sync",
                    event_ticker=rebalance.event_ticker,
                    fresh_over=fresh_over,
                    fresh_under=fresh_under,
                    fresh_delta=fresh_delta,
                )
                return

            # Safety gate — same checks as place_bids (P16, P18)
            ok, reason = ledger.is_placement_safe(
                under_side, rebalance.catchup_qty, rebalance.catchup_price
            )
            if not ok:
                self._notify(
                    f"Catch-up BLOCKED ({under_side.value}): {reason}",
                    "warning",
                )
                logger.warning(
                    "rebalance_catchup_blocked",
                    event_ticker=rebalance.event_ticker,
                    side=under_side.value,
                    reason=reason,
                )
                return

            try:
                await self._rest.create_order(
                    ticker=rebalance.catchup_ticker,
                    action="buy",
                    side="no",
                    no_price=rebalance.catchup_price,
                    count=rebalance.catchup_qty,
                )
                self._notify(
                    f"Rebalance step 2: catch-up {rebalance.catchup_ticker}"
                    f" {rebalance.catchup_qty} @ {rebalance.catchup_price}c",
                )
                logger.info(
                    "rebalance_catchup_placed",
                    event_ticker=rebalance.event_ticker,
                    ticker=rebalance.catchup_ticker,
                    qty=rebalance.catchup_qty,
                    price=rebalance.catchup_price,
                )
            except Exception as e:
                self._notify(
                    f"Catch-up FAILED: {type(e).__name__}: {e}",
                    "error",
                )
                logger.exception(
                    "rebalance_catchup_error",
                    event_ticker=rebalance.event_ticker,
                    ticker=rebalance.catchup_ticker,
                )

        # Post-action: always re-sync from Kalshi regardless of outcome.
        # The review must verify reality, not trust the model (P7/P15).
        await self._verify_after_action(rebalance.event_ticker)

    async def _verify_after_action(self, event_ticker: str) -> None:
        """Re-sync from Kalshi after any order action to verify outcome."""
        pair = self._find_pair(event_ticker)
        if pair is None:
            return
        try:
            orders = await self._rest.get_orders(limit=200)
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
            return ""

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

        # Discrepancy — ledger doesn't trust its own data
        if ledger.has_discrepancy:
            return "Discrepancy"

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

            if not self._auto_config.enabled:
                return "Sug. off"

            # Check why proposer isn't suggesting
            opp = self._scanner.get_opportunity(event_ticker)
            if opp is None or opp.fee_edge < self._auto_config.edge_threshold_cents:
                return "Low edge"

            # Check stability timer
            stability = self._proposer.stability_elapsed(event_ticker)
            if (
                stability is not None
                and self._auto_config.stability_seconds > 0
                and stability < self._auto_config.stability_seconds
            ):
                remaining = self._auto_config.stability_seconds - stability
                return f"Stable {remaining:.0f}s"

            # Check cooldown
            cooldown = self._proposer.cooldown_elapsed(event_ticker)
            if cooldown is not None and cooldown < self._auto_config.rejection_cooldown_seconds:
                remaining = self._auto_config.rejection_cooldown_seconds - cooldown
                return f"Cooldown {remaining:.0f}s"

            return "Ready"

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
