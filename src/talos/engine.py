"""TradingEngine — central orchestrator for trading logic.

Owns all subsystem dependencies, mutable caches, and polling/action methods.
The TUI delegates to this engine rather than managing trading state directly.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from talos.bid_adjuster import BidAdjuster
from talos.cpm import CPMTracker
from talos.game_manager import GameManager
from talos.market_feed import MarketFeed
from talos.models.order import Order
from talos.models.position import EventPositionSummary
from talos.models.proposal import Proposal, ProposalKey
from talos.position_ledger import Side, compute_display_positions
from talos.proposal_queue import ProposalQueue
from talos.rest_client import KalshiRESTClient
from talos.scanner import ArbitrageScanner
from talos.top_of_market import TopOfMarketTracker

if TYPE_CHECKING:
    from talos.models.strategy import BidConfirmation

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
        proposal_queue: ProposalQueue | None = None,
    ) -> None:
        self._scanner = scanner
        self._game_manager = game_manager
        self._rest = rest_client
        self._feed = market_feed
        self._tracker = tracker
        self._adjuster = adjuster
        self._initial_games = list(initial_games or [])
        self._proposal_queue = proposal_queue or ProposalQueue()

        # Mutable caches
        self._queue_cache: dict[str, int] = {}
        self._orders_cache: list[Order] = []
        self._cpm = CPMTracker()
        self._balance: int = 0
        self._portfolio_value: int = 0
        self._position_summaries: list[EventPositionSummary] = []
        self._order_data: list[dict[str, object]] = []

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
            await self._feed.start()
        except Exception as e:
            logger.exception("feed_connection_error")
            self._notify(f"WebSocket error: {type(e).__name__}: {e}", "error")

    async def refresh_account(self) -> None:
        """Fetch balance + orders, sync ledgers, compute positions."""
        try:
            balance = await self._rest.get_balance()
            self._balance = balance.balance
            self._portfolio_value = balance.portfolio_value

            orders = await self._rest.get_orders(limit=50)
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

            # Compute position summaries from ledger state
            self._position_summaries = compute_display_positions(
                self._adjuster._ledgers,
                self._scanner.pairs,
                self._queue_cache,
                self._cpm,
            )

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

        # Recompute positions from ledgers with updated queue cache
        self._position_summaries = compute_display_positions(
            self._adjuster._ledgers,
            self._scanner.pairs,
            self._queue_cache,
            self._cpm,
        )

    async def refresh_trades(self) -> None:
        """Fetch recent trades for CPM tracking."""
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
            key = ProposalKey(
                event_ticker=proposal.event_ticker,
                side=proposal.side,
                kind="adjustment",
            )
            envelope = Proposal(
                key=key,
                kind="adjustment",
                summary=(
                    f"ADJ {proposal.event_ticker} {proposal.side}"
                    f" {proposal.cancel_price}\u2192{proposal.new_price}c"
                ),
                detail=proposal.reason,
                created_at=datetime.now(UTC),
                adjustment=proposal,
            )
            self._proposal_queue.add(envelope)

    # ── Action methods ──────────────────────────────────────────────

    async def place_bids(self, bid: BidConfirmation) -> None:
        """Place NO orders on both legs."""
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

        if envelope.kind == "adjustment" and envelope.adjustment is not None:
            try:
                await self._adjuster.execute(envelope.adjustment, self._rest)
                self._notify(
                    f"Adjusted: {envelope.adjustment.event_ticker}"
                    f" {envelope.adjustment.side}"
                    f" \u2192 {envelope.adjustment.new_price}c",
                )
            except Exception as e:
                self._notify(
                    f"Adjustment FAILED: {type(e).__name__}: {e}", "error"
                )
                logger.exception(
                    "adjustment_execute_error",
                    event_ticker=envelope.adjustment.event_ticker,
                )
        elif envelope.kind == "bid" and envelope.bid is not None:
            bid = envelope.bid
            from talos.models.strategy import BidConfirmation

            confirmation = BidConfirmation(
                ticker_a=bid.ticker_a,
                ticker_b=bid.ticker_b,
                no_a=bid.no_a,
                no_b=bid.no_b,
                qty=bid.qty,
            )
            await self.place_bids(confirmation)

    def reject_proposal(self, key: ProposalKey) -> None:
        """Reject and remove a queued proposal."""
        self._proposal_queue.reject(key)
        # Also clear from adjuster's internal proposals if it's an adjustment
        if key.kind == "adjustment" and key.side:
            self._adjuster.clear_proposal(key.event_ticker, Side(key.side))
        self._notify(f"Rejected: {key.event_ticker} {key.kind}")

    async def approve_adjustment(self, event_ticker: str, side_value: str) -> None:
        """Execute an approved bid adjustment via amend.

        Delegates to :meth:`approve_proposal` (kept for backward compatibility).
        """
        key = ProposalKey(
            event_ticker=event_ticker, side=side_value, kind="adjustment"
        )
        await self.approve_proposal(key)

    def reject_adjustment(self, event_ticker: str, side_value: str) -> None:
        """Reject a pending bid adjustment proposal.

        Delegates to :meth:`reject_proposal` (kept for backward compatibility).
        """
        key = ProposalKey(
            event_ticker=event_ticker, side=side_value, kind="adjustment"
        )
        self.reject_proposal(key)

    # ── Internal helpers ─────────────────────────────────────────────

    async def _discover_active_events(self) -> list[str]:
        """Query Kalshi for events with positions or resting orders."""
        try:
            event_positions = await self._rest.get_event_positions()
            tickers = [ep.event_ticker for ep in event_positions]
            if tickers:
                logger.info("discovered_active_events", count=len(tickers), tickers=tickers)
            return tickers
        except Exception:
            logger.warning("event_discovery_failed", exc_info=True)
            return []

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
