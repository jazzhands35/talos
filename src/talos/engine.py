"""TradingEngine — central orchestrator for trading logic.

Owns all subsystem dependencies, mutable caches, and polling/action methods.
The TUI delegates to this engine rather than managing trading state directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import structlog

if TYPE_CHECKING:
    from talos.data_collector import DataCollector
    from talos.settlement_tracker import SettlementCache

from talos.automation_config import AutomationConfig
from talos.bid_adjuster import BidAdjuster
from talos.cpm import CPMTracker
from talos.errors import KalshiAPIError, KalshiNotFoundError, KalshiRateLimitError
from talos.fees import MAKER_FEE_RATE, fee_adjusted_cost_bps, fee_adjusted_edge_bps
from talos.game_manager import (
    CommitResult,
    GameManager,
    MarketAdmissionError,
    validate_market_for_admission,
)
from talos.game_status import GameStatusResolver
from talos.lifecycle_feed import LifecycleFeed
from talos.market_feed import MarketFeed
from talos.models.market import Event, Market
from talos.models.order import Order
from talos.models.portfolio import EventPosition, Position, Settlement
from talos.models.position import EventPositionSummary
from talos.models.proposal import Proposal, ProposalKey, ProposedQueueImprovement
from talos.models.tree import RemoveOutcome
from talos.models.ws import FillMessage, TickerMessage, UserOrderMessage
from talos.opportunity_proposer import OpportunityProposer
from talos.orderbook import OrderBookManager
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
from talos.units import (
    ONE_CENT_BPS,
    ONE_CONTRACT_FP100,
    ONE_DOLLAR_BPS,
    bps_to_cents_round,
    format_bps_as_dollars_display,
)

if TYPE_CHECKING:
    from talos.milestones import MilestoneResolver
    from talos.models.strategy import ArbPair, BidConfirmation
    from talos.tree_metadata import TreeMetadataStore

logger = structlog.get_logger()
_STALE_BOOK_RECOVERY_COOLDOWN_S = 120.0
_EVENT_CLAIM_STALE_S = 60.0  # force-release stale per-event claims after this

# Section 8 startup safety gate — how long to block a risk-increasing op
# while waiting for the ledger's confirmation flags to clear, and how long
# to wait before auto-triggering reconcile_from_fills on a stale-fills flag.
STARTUP_SYNC_TIMEOUT_S = 30.0
AUTO_RECONCILE_DELAY_S = 5.0


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


# ── bps/fp100 helpers ─────────────────────────────────────────────
# Post 13a-2e: legacy-field fallback helpers deleted. Only
# ``_order_maker_fees_bps`` remains — it's a thin readability alias that
# isn't part of the 13a-2 dual-field removal.


def _order_maker_fees_bps(order: Order) -> int:
    """Maker fees in bps."""
    return order.maker_fees_bps


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
        tree_metadata_store: TreeMetadataStore | None = None,
        milestone_resolver: MilestoneResolver | None = None,
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
        self._tree_metadata_store = tree_metadata_store
        self._milestone_resolver = milestone_resolver
        self._data_collector = data_collector
        self._settlement_cache = settlement_cache
        self._position_feed = position_feed
        self._proposer = OpportunityProposer(self._auto_config, data_collector=data_collector)

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
        self._winding_down: set[str] = set()  # pairs removed-while-inventory-present
        self._event_fully_removed_listeners: list = []
        self._game_started_events: set[str] = set()  # events where game is live/final
        self._last_jump_eval: dict[str, tuple[int, int]] = {}  # ticker -> (book_top, resting)
        # Dedup for replay log: only write an imbalance-eval row on transition.
        self._last_imbalance_outcome: dict[str, str] = {}
        self._stale_candidates: set[str] = set()  # two-strike stale position cleanup
        self._log_once_keys: set[tuple[str, str]] = set()
        self._dirty_events: set[str] = set()  # events needing imbalance check (WS-driven)
        self._overcommit_events: set[str] = set()  # unit overcommit — priority
        # Reconciliation-derived overcommit targets: event → {side → target_resting}
        # Avoids re-deriving from ledger which may have stale fill_gap.
        self._overcommit_targets: dict[str, dict[str, int]] = {}
        self._full_sweep_counter: int = 0  # counts cycles since last full sweep
        self._initial_sync_done: bool = False  # gate bids until first refresh_account
        # gate reconciliation until first account refresh completes
        self._account_sync_done: bool = False
        self._last_reconcile_fill_mismatch: dict[tuple[str, str], tuple[int, int]] = {}
        self._stale_recovery_retry_after: dict[str, float] = {}  # ticker -> monotonic retry time
        self._pair_index: dict[str, ArbPair] = {}  # rebuilt in recompute_positions
        self._ticker_to_event: dict[str, str] = {}  # rebuilt in recompute_positions
        self._pending_kinds_cache: dict[str, set[str]] = {}  # rebuilt in recompute_positions

        # ── WS reaction pipeline ──
        self._reaction_queue: asyncio.Queue[str] = asyncio.Queue()
        self._reaction_task: asyncio.Task[None] | None = None
        self._event_claims: dict[str, str] = {}  # event_ticker → owner ("ws" | "poll")
        self._event_claim_times: dict[str, float] = {}  # event_ticker → monotonic claim time
        self._last_ws_reaction: dict[str, float] = {}  # observability: last WS reaction timestamp
        self._reaction_consumer_started: bool = False

        # Startup gate: set once the milestone resolver cascade is armed.
        # TradingEngine.wait_for_ready_for_trading() awaits this with a hard cap.
        self._ready_for_trading: asyncio.Event = asyncio.Event()

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
        self.on_blacklist_change: Callable[[list[str]], None] | None = None

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

    def seconds_since_last_book_update(self) -> float:
        """Seconds since any orderbook received a delta. Used for UI data health."""
        import time

        last = self._feed.book_manager.most_recent_update()
        if last <= 0.0:
            return float("inf")
        return time.time() - last

    @property
    def game_status_resolver(self) -> GameStatusResolver | None:
        return self._game_status_resolver

    def mark_event_dirty(self, event_ticker: str) -> None:
        """Flag an event for imbalance checking on the next cycle.

        Called from WS callbacks (fills, orderbook changes) to drive
        event-driven rebalancing instead of polling all 1000+ pairs.
        """
        self._dirty_events.add(event_ticker)

    async def wait_for_ready_for_trading(self) -> None:
        """Block until the resolver cascade is armed, or a hard cap expires.

        Flag-off (tree_mode = False): return immediately (legacy behavior).
        Flag-on: await _ready_for_trading.set() OR startup_milestone_wait_seconds
        elapsed — whichever first. Emits structured log on timeout.
        """
        if not self._auto_config.tree_mode:
            return

        timeout = self._auto_config.startup_milestone_wait_seconds
        try:
            await asyncio.wait_for(self._ready_for_trading.wait(), timeout=timeout)
            logger.info("startup_gate_ready", elapsed_s=None)
        except TimeoutError:
            self._ready_for_trading.set()
            logger.warning(
                "startup_gate_timeout",
                elapsed_s=timeout,
                exit_only_degraded=True,
            )

    # ── Per-event claim mechanism ─────────────���────────────────────

    def _claim_event(self, event_ticker: str, owner: str) -> bool:
        """Try to claim exclusive processing rights for an event.

        Returns True if claimed, False if already claimed by another owner.
        Stale claims (>_EVENT_CLAIM_STALE_S) are force-released with a warning.
        """
        existing = self._event_claims.get(event_ticker)
        if existing is not None and existing != owner:
            claim_time = self._event_claim_times.get(event_ticker, 0.0)
            if time.monotonic() - claim_time > _EVENT_CLAIM_STALE_S:
                logger.warning(
                    "stale_event_claim_released",
                    event_ticker=event_ticker,
                    old_owner=existing,
                    new_owner=owner,
                )
            else:
                return False
        self._event_claims[event_ticker] = owner
        self._event_claim_times[event_ticker] = time.monotonic()
        return True

    def _release_event(self, event_ticker: str, owner: str) -> None:
        """Release claim if we still own it."""
        if self._event_claims.get(event_ticker) == owner:
            del self._event_claims[event_ticker]
            self._event_claim_times.pop(event_ticker, None)

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
        """Resolve event ticker to short human-readable label with Talos ID prefix."""
        tid = self._scanner.get_talos_id(event_ticker)
        label = self._game_manager.labels.get(event_ticker, event_ticker)
        return f"#{tid} {label}" if tid else label

    @property
    def event_positions(self) -> dict[str, EventPosition]:
        """Rich event-level position data from Kalshi."""
        return self._event_positions

    @property
    def unit_size(self) -> int:
        return self._adjuster.unit_size

    @property
    def has_settlement_cache(self) -> bool:
        """True when settlement history is backed by a persistent cache."""
        return self._settlement_cache is not None

    @property
    def book_manager(self) -> OrderBookManager:
        """Expose orderbook state for read-only UI queries."""
        return self._feed.book_manager

    def set_unit_size(self, size: int) -> None:
        """Update unit size across adjuster and all existing ledgers."""
        self._adjuster.set_unit_size(size)
        logger.info("unit_size_changed", unit_size=size)
        if self.on_unit_size_change is not None:
            self.on_unit_size_change(size)

    async def blacklist_ticker(self, entry: str) -> list[str]:
        """Add entry to blacklist, remove matching games, persist. Returns removed tickers."""
        self._game_manager.add_to_blacklist(entry)
        removed = await self._game_manager.remove_blacklisted_games()
        if self.on_blacklist_change is not None:
            self.on_blacklist_change(self._game_manager.ticker_blacklist)
        return removed

    # ── Delegated REST queries ────────────────────────────────────

    async def get_settlements(self, *, limit: int = 200) -> list[Settlement]:
        """Fetch settlement history (single page)."""
        return await self._rest.get_settlements(limit=limit)

    async def get_all_settlements(self) -> list[Settlement]:
        """Fetch ALL settlements by paginating through cursor-based results."""
        return await self._rest.get_all_settlements()

    def performance_settlement_rows(self) -> list[dict[str, object]]:
        """Return cached settlement rows for performance aggregation."""
        if self._settlement_cache is None:
            return []
        return self._settlement_cache.all_settlements()

    def cached_settlement_models(self) -> list[tuple[Settlement, int | None, str]]:
        """Return cached settlement tuples for the settlement history screen."""
        if self._settlement_cache is None:
            return []
        return self._settlement_cache.settlements_as_models()

    def cache_settlements(
        self,
        settlements: list[Settlement],
        est_pnl_map: dict[str, int],
    ) -> None:
        """Store settlements in the persistent cache when available."""
        if self._settlement_cache is None:
            return
        self._settlement_cache.upsert_batch(
            settlements,
            est_pnl_map=est_pnl_map,
            subtitles=self._game_manager.subtitles,
        )

    def log_market_snapshots(self, snapshots: list[dict[str, object]]) -> None:
        """Persist periodic market snapshots for later analysis."""
        if self._data_collector is None:
            return
        self._data_collector.log_market_snapshots(snapshots)

    def log_scan(
        self,
        *,
        events_found: int,
        events_eligible: int,
        events_selected: int,
        series_scanned: int,
        duration_ms: int,
        events: list[dict[str, object]],
    ) -> None:
        """Persist scan results when data collection is enabled."""
        if self._data_collector is None:
            return
        self._data_collector.log_scan(
            events_found=events_found,
            events_eligible=events_eligible,
            events_selected=events_selected,
            series_scanned=series_scanned,
            duration_ms=duration_ms,
            events=events,
        )

    async def scan_events(self, *, scan_mode: str) -> list[Event]:
        """Delegate event discovery to the game manager."""
        return await self._game_manager.scan_events(scan_mode=scan_mode)

    async def refresh_volumes(self) -> None:
        """Refresh per-market 24h volume for monitored events."""
        await self._game_manager.refresh_volumes()

    def orderbook_ages(self, *, now: float) -> dict[str, float | None]:
        """Return per-ticker age of the last orderbook update."""
        ages: dict[str, float | None] = {}
        for ticker in self._feed.book_manager.tickers:
            book = self._feed.book_manager.get_book(ticker)
            if book is None or book.last_update <= 0.0:
                ages[ticker] = None
            else:
                ages[ticker] = now - book.last_update
        return ages

    def get_series_for_event(self, event_ticker: str) -> str | None:
        """Return the series ticker for a given event, or None if unknown."""
        pair = self._game_manager.get_game(event_ticker)
        if pair and pair.series_ticker:
            return pair.series_ticker
        return None

    async def replace_blacklist(self, entries: list[str]) -> list[str]:
        """Replace the ticker blacklist and persist the change."""
        removed = await self._game_manager.replace_blacklist(entries)
        if self.on_blacklist_change is not None:
            self.on_blacklist_change(self._game_manager.ticker_blacklist)
        return removed

    async def enforce_exit_only(self, event_ticker: str) -> None:
        """Execute the async portion of exit-only enforcement."""
        await self._enforce_exit_only(event_ticker)

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

        pair = self.find_pair(event_ticker)
        filled_a = ledger.filled_count(Side.A)
        filled_b = ledger.filled_count(Side.B)
        game_started = event_ticker in self._game_started_events

        async def _cancel(side: Side, order_id: str, reason: str) -> None:
            """Cancel a resting order. On market_closed, clear ledger.

            F36: routes through :meth:`cancel_order_with_verify` so a
            404 on the tracked ID triggers a full resync rather than a
            blind optimistic-clear (F33).
            """
            if pair is None:
                # Pair was removed between ledger lookup and now — rare
                # race. Skip rather than issue a raw cancel that would
                # violate the F36 cancel-discipline guard. Resync will
                # happen via the broader sync_from_orders cycle.
                logger.warning(
                    "exit_only_cancel_no_pair",
                    event_ticker=event_ticker,
                    side=side.value,
                    order_id=order_id,
                )
                return
            try:
                await self.cancel_order_with_verify(order_id, pair)
                logger.info(
                    "exit_only_cancel",
                    event_ticker=event_ticker,
                    side=side.value,
                    order_id=order_id,
                    reason=reason,
                )
            except KalshiAPIError as e:
                if e.status_code == 409 and "market_closed" in str(e).lower():
                    # Market is done — orders no longer exist. Clear ledger
                    # so the cleanup path sees resting=0.
                    with contextlib.suppress(ValueError):
                        ledger.record_cancel(side, order_id)
                    ledger.mark_order_cancelled(order_id)
                    logger.info(
                        "exit_only_cancel_market_closed",
                        event_ticker=event_ticker,
                        side=side.value,
                    )
                else:
                    logger.warning(
                        "exit_only_cancel_failed",
                        event_ticker=event_ticker,
                        side=side.value,
                        exc_info=True,
                    )
            except Exception:
                logger.warning(
                    "exit_only_cancel_failed",
                    event_ticker=event_ticker,
                    side=side.value,
                    exc_info=True,
                )

        if filled_a == filled_b:
            # Balanced — cancel everything on both sides
            reason = "game_started" if game_started else "balanced"
            for side in (Side.A, Side.B):
                order_id = ledger.resting_order_id(side)
                if order_id is not None:
                    await _cancel(side, order_id, reason)
        else:
            # Imbalanced — cancel the ahead side, reduce behind side
            ahead = Side.A if filled_a > filled_b else Side.B
            behind = ahead.other
            order_id = ledger.resting_order_id(ahead)
            if order_id is not None:
                await _cancel(ahead, order_id, "ahead_side")

            # Reduce behind side resting so it can't overshoot ahead's fills
            behind_order_id = ledger.resting_order_id(behind)
            if behind_order_id is not None:
                ahead_filled = ledger.filled_count(ahead)
                behind_filled = ledger.filled_count(behind)
                target_behind_resting = ahead_filled - behind_filled
                current_behind_resting = ledger.resting_count(behind)
                if target_behind_resting <= 0:
                    await _cancel(behind, behind_order_id, "behind_overshoot")
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
                    except KalshiAPIError as e:
                        if e.status_code == 409 and "market_closed" in str(e).lower():
                            import contextlib

                            with contextlib.suppress(ValueError):
                                ledger.record_cancel(behind, behind_order_id)
                            ledger.mark_order_cancelled(behind_order_id)
                        else:
                            logger.warning(
                                "exit_only_reduce_behind_failed",
                                event_ticker=event_ticker,
                                side=behind.value,
                                exc_info=True,
                            )
                    except Exception:
                        logger.warning(
                            "exit_only_reduce_behind_failed",
                            event_ticker=event_ticker,
                            side=behind.value,
                            exc_info=True,
                        )

        await self._verify_after_action(event_ticker)

    def _resolve_event_start(
        self, kalshi_event_ticker: str, pair: Any
    ) -> tuple[datetime | None, str | None]:
        """Resolver cascade per spec §5.2.

        Priority: manual override -> Kalshi milestone -> sports GSR -> nothing.

        Returns (start_time, source) where source is one of:
          - "manual_opt_out": user explicitly disabled exit-only for this event
          - "manual":         user-set override; start_time is the datetime
          - "milestone":      Kalshi milestone start_date
          - "sports_gsr":     sports provider scheduled_start
          - None:             no schedule data available
        """
        # 1. Manual (user-owned)
        if self._tree_metadata_store is not None:
            manual = self._tree_metadata_store.manual_event_start(kalshi_event_ticker)
            if manual == "none":
                return (None, "manual_opt_out")
            if manual is not None:
                return (manual, "manual")

        # 2. Kalshi milestone
        if self._milestone_resolver is not None:
            ms = self._milestone_resolver.event_start(kalshi_event_ticker)
            if ms is not None:
                return (ms, "milestone")

        # 3. Sports GSR (keyed by pair.event_ticker — sports pairs have
        #    event_ticker == kalshi_event_ticker)
        if self._game_status_resolver is not None:
            gs = self._game_status_resolver.get(pair.event_ticker)
            if gs and getattr(gs, "scheduled_start", None):
                return (gs.scheduled_start, "sports_gsr")

        return (None, None)

    def _check_exit_only(self) -> None:
        """Dispatch to legacy or tree-mode cascade based on automation flag."""
        if self._auto_config.tree_mode:
            self._check_exit_only_tree_mode()
        else:
            self._check_exit_only_legacy()

    def _check_exit_only_tree_mode(self) -> None:
        """Resolver-cascade driven auto-trigger per spec §5.2.

        Dedupes by kalshi_event_ticker so multi-market events (e.g., Fed
        presser with 46 markets) get a single scheduling decision applied
        to all sibling pairs via _flip_exit_only_for_key.
        """
        now = datetime.now(UTC)
        seen_events: set[str] = set()

        for pair in self._scanner.pairs:
            key = pair.kalshi_event_ticker or pair.event_ticker
            if key in seen_events:
                continue
            seen_events.add(key)

            if pair.event_ticker in self._exit_only_events:
                continue

            start_time, source = self._resolve_event_start(key, pair)

            if source == "manual_opt_out":
                continue

            if source is None:
                # Defensive degradation: if the cascade has nothing, ask
                # the milestone resolver whether IT considers itself
                # trustworthy right now. Healthy = last refresh recent AND
                # index non-empty. The earlier "last_refresh is None" check
                # only covered the bootstrap window — Kalshi can serve a
                # 200-OK with zero milestones during partial outages, which
                # marked last_refresh but left the index empty. is_healthy()
                # also catches that, so the safety degradation stays armed
                # past the first successful-but-empty refresh.
                ms_resolver = self._milestone_resolver
                ms_unhealthy = ms_resolver is not None and not ms_resolver.is_healthy()
                if ms_unhealthy:
                    self._flip_exit_only_for_key(
                        key,
                        reason="milestones_unavailable",
                    )
                    continue
                self._log_once("exit_only_no_schedule", key)
                continue

            # Sports GSR additionally supplies live/post state — flip immediately
            if source == "sports_gsr" and self._game_status_resolver is not None:
                gs = self._game_status_resolver.get(pair.event_ticker)
                if gs and gs.state in ("live", "post"):
                    self._flip_exit_only_for_key(
                        key,
                        reason=f"sports_{gs.state}",
                    )
                    continue

            if start_time is None:
                continue

            lead_min = self._auto_config.exit_only_minutes
            if (start_time - now).total_seconds() < lead_min * 60:
                self._flip_exit_only_for_key(
                    key,
                    reason=source,
                    scheduled_start=start_time,
                )

    def _flip_exit_only_for_key(
        self,
        kalshi_event_ticker: str,
        *,
        reason: str,
        scheduled_start: datetime | None = None,
    ) -> None:
        """Flip all pairs sharing kalshi_event_ticker into exit-only.

        Adds each sibling pair's pair-level event_ticker to _exit_only_events
        so downstream enforcement (_enforce_all_exit_only, adjuster ledger
        lookups) keeps working — _exit_only_events is a pair-level set.

        For sports (where event_ticker == kalshi_event_ticker), this adds the
        single event key. For non-sports multi-market events, this adds
        every market-pair's event_ticker.
        """
        flipped: list[str] = []
        for pair in self._scanner.pairs:
            pair_key = pair.kalshi_event_ticker or pair.event_ticker
            if pair_key != kalshi_event_ticker:
                continue
            if pair.event_ticker in self._exit_only_events:
                continue
            self._exit_only_events.add(pair.event_ticker)
            self._game_started_events.add(pair.event_ticker)
            flipped.append(pair.event_ticker)

        if not flipped:
            return

        name = self._display_name(kalshi_event_ticker)
        self._notify(
            f"EXIT-ONLY: {name} — {reason}",
            "warning",
            toast=True,
        )
        logger.info(
            "exit_only_auto_trigger",
            kalshi_event_ticker=kalshi_event_ticker,
            reason=reason,
            flipped_pairs=flipped,
            pair_count=len(flipped),
            scheduled_start=(scheduled_start.isoformat() if scheduled_start else None),
        )

    def _log_once(self, event_key: str, event_ticker: str) -> None:
        """Emit a structured log at most once per event_ticker per process."""
        key = (event_key, event_ticker)
        if key in self._log_once_keys:
            return
        self._log_once_keys.add(key)
        logger.info(event_key, event_ticker=event_ticker)

    def _check_exit_only_legacy(self) -> None:
        """Auto-trigger exit-only based on game status, auto-remove when done.

        Called from recompute_positions (runs every refresh cycle).
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

            # Market determined/settled with no resting — nothing more
            # to do. Unmatched positions settle on their own via Kalshi.
            if resting_a == 0 and resting_b == 0:
                market_done = event_ticker in self._settled_markets
                if not market_done:
                    # Check close_time as fallback (no API call needed).
                    # For non-sports, close_time IS the resolution time.
                    # For sports, close_time is a 14-day buffer — but
                    # sports events have game_status which handles them.
                    pair = self.find_pair(event_ticker)
                    if pair is not None and pair.close_time:
                        try:
                            ct = datetime.fromisoformat(pair.close_time.replace("Z", "+00:00"))
                            if datetime.now(UTC) > ct:
                                market_done = True
                        except (ValueError, TypeError):
                            pass
                if market_done:
                    name = self._display_name(event_ticker)
                    self._notify(f"Exit-only DONE: {name} — removing (market closed)")
                    logger.info(
                        "exit_only_auto_remove_determined",
                        event_ticker=event_ticker,
                        filled_a=filled_a,
                        filled_b=filled_b,
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
        Starts the listen loop before sending subscriptions so incoming
        snapshots are processed as they arrive instead of buffering and
        flooding the event loop when listen() finally starts.
        """
        first_connect = True
        while True:
            listen_task: asyncio.Task[None] | None = None
            try:
                await self._feed.connect()
                self._ws_connected = True
                self._start_reaction_consumer()
                self._notify("WebSocket connected")

                # Start the listen loop BEFORE subscribing — this way,
                # orderbook snapshots are processed as they arrive instead
                # of queuing in the WS buffer and hitting all at once.
                listen_task = asyncio.create_task(self._feed.start(), name="ws_listen")

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

                await listen_task
                # If we reach here, the WS exited cleanly
                self._ws_connected = False
                await self._stop_reaction_consumer()
                self._notify("WEBSOCKET DISCONNECTED — prices are stale!", "error", toast=True)
                logger.error("ws_connection_lost", reason="listen loop exited cleanly")
            except Exception as e:
                self._ws_connected = False
                await self._stop_reaction_consumer()
                # Cancel the listen task if it's still running (e.g., subscribe failed)
                if listen_task is not None:
                    if not listen_task.done():
                        listen_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await listen_task
                self._notify(f"WEBSOCKET DISCONNECTED: {e}", "error", toast=True)
                logger.error("ws_connection_lost", reason=str(e), error_type=type(e).__name__)

            # Wait and retry — loop instead of recursion
            logger.info("ws_reconnecting")
            self._notify("Reconnecting WebSocket in 5s...", "warning", toast=True)
            await asyncio.sleep(5)

    # ── WS reaction consumer ─────────────────────────────────────

    def _start_reaction_consumer(self) -> None:
        """Start the background reaction consumer (idempotent)."""
        if self._reaction_consumer_started:
            return
        self._reaction_consumer_started = True
        self._reaction_task = asyncio.create_task(
            self._run_reaction_consumer(), name="ws_reaction_consumer"
        )

    async def _stop_reaction_consumer(self) -> None:
        """Cancel the reaction consumer and drain the queue.

        Called on WS disconnect so the consumer restarts cleanly on reconnect.
        """
        if self._reaction_task is not None and not self._reaction_task.done():
            self._reaction_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaction_task
        self._reaction_task = None
        self._reaction_consumer_started = False
        # Drain stale events — they'll be re-evaluated on reconnect
        while not self._reaction_queue.empty():
            try:
                self._reaction_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._event_claims.clear()
        self._event_claim_times.clear()

    async def _run_reaction_consumer(self) -> None:
        """Drain the reaction queue, coalesce duplicates, react per event."""
        logger.info("reaction_consumer_started")
        while True:
            try:
                first = await self._reaction_queue.get()
                events: set[str] = {first}
                # Greedily drain to coalesce burst events
                while not self._reaction_queue.empty():
                    try:
                        events.add(self._reaction_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                for event_ticker in events:
                    if not self._claim_event(event_ticker, "ws"):
                        continue
                    try:
                        await self._react_to_event(event_ticker)
                    finally:
                        self._release_event(event_ticker, "ws")
            except asyncio.CancelledError:
                logger.info("reaction_consumer_stopped")
                return
            except Exception:
                logger.exception("reaction_consumer_error")
                await asyncio.sleep(1.0)

    async def _react_to_event(self, event_ticker: str) -> None:
        """Run the scoped reaction pipeline for a single event.

        Called by the WS reaction consumer after a fill or order update.
        Claim/release handled by the caller (_run_reaction_consumer).
        """
        if not self._initial_sync_done:
            return
        started = time.monotonic()
        pair = self.find_pair(event_ticker)
        if pair is None:
            return
        self._reevaluate_jumps_for(event_ticker, pair)
        await self._check_imbalance_for(event_ticker, pair)
        self._last_ws_reaction[event_ticker] = time.monotonic()
        logger.info(
            "ws_reaction_complete",
            event_ticker=event_ticker,
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
        )

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
            quarantined_any = False
            quarantined_tickers: list[str] = []
            for data in self._initial_games_full:
                try:
                    pair = self._game_manager.restore_game(data)
                    if pair is None:
                        continue
                    self._adjuster.add_event(pair)
                    # Seed ledger with saved fill state (prevents amnesia
                    # for same-ticker pairs where sync_from_positions is unavailable)
                    saved_ledger = data.get("ledger")
                    if saved_ledger:
                        ledger = self._adjuster.get_ledger(pair.event_ticker)
                        ledger.seed_from_saved(saved_ledger)
                    # Restore persisted 24h volume (avoids slow series-by-series REST refresh)
                    vol_a = data.get("volume_a")
                    vol_b = data.get("volume_b")
                    if vol_a is not None:
                        self._game_manager._volumes_24h[pair.ticker_a] = int(vol_a)
                    if vol_b is not None:
                        self._game_manager._volumes_24h[pair.ticker_b] = int(vol_b)
                    self._apply_persisted_engine_state(pair)
                    # F32: admission re-check for Phase-0-incompatible shapes.
                    # A persisted pair whose market turned fractional/sub-cent
                    # while Talos was offline must restore into exit_only so
                    # new entries are blocked — exits + cancels still work.
                    try:
                        market_a = await self._rest.get_market(pair.ticker_a)
                        if pair.ticker_b != pair.ticker_a:
                            market_b = await self._rest.get_market(pair.ticker_b)
                        else:
                            # YES/NO arb on the same ticker — avoid double fetch
                            market_b = market_a
                        validate_market_for_admission(market_a, market_b)
                    except MarketAdmissionError as exc:
                        if pair.engine_state not in ("exit_only", "winding_down"):
                            pair.engine_state = "exit_only"
                        self._exit_only_events.add(pair.event_ticker)
                        quarantined_any = True
                        quarantined_tickers.append(pair.event_ticker)
                        # Per-pair log kept for forensics; the operator toast
                        # is consolidated below into one summary so a startup
                        # that quarantines N pairs (we've seen N > 100 in
                        # production when Kalshi flips a whole product line
                        # to fractional overnight) doesn't flood the UI.
                        logger.warning(
                            "restore_quarantine_applied",
                            event_ticker=pair.event_ticker,
                            reason=str(exc),
                        )
                    except Exception:
                        # REST failure or unexpected error — log and leave the
                        # pair in its persisted state. Transient network errors
                        # must not trigger false-positive quarantine.
                        logger.warning(
                            "restore_admission_check_failed",
                            event_ticker=pair.event_ticker,
                            exc_info=True,
                        )
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
            # F37: durable persist the quarantine so a crash before the next
            # scheduled save cannot resurrect a quarantined pair as active.
            if quarantined_any:
                # One consolidated toast for the operator; forensics remain
                # in the per-pair restore_quarantine_applied log lines.
                preview = ", ".join(quarantined_tickers[:3])
                if len(quarantined_tickers) > 3:
                    preview += f", +{len(quarantined_tickers) - 3} more"
                self._notify(
                    f"{len(quarantined_tickers)} pair(s) restored in "
                    f"exit_only (Phase 0 admission guard): {preview}",
                    "warning",
                )
                try:
                    self._persist_active_games()
                except Exception:
                    # In-memory quarantine still holds; next scheduled save
                    # will pick it up. A crash before then would lose it —
                    # tradeoff vs. crashing startup hard on a persist glitch.
                    logger.warning(
                        "restore_quarantine_persist_failed",
                        exc_info=True,
                    )
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
            # Only REST-fetch discovered events not already in cache.
            # Check both event_ticker and kalshi_event_ticker — non-sports
            # pairs use market tickers as event_ticker, but discovery returns
            # the Kalshi event ticker.
            cached_kalshi = {p.kalshi_event_ticker for p in pairs if p.kalshi_event_ticker}
            all_cached = cached_tickers | cached_kalshi
            all_tickers = [t for t in all_tickers if t not in all_cached]

        if all_tickers:
            try:
                pairs = await self._game_manager.add_games(all_tickers)
            except Exception:
                # MarketPickerNeeded or other errors during discovery — skip
                pairs = []
                logger.warning("initial_add_games_failed", exc_info=True)
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
            self._balance = balance.balance_bps // ONE_CENT_BPS
            self._portfolio_value = balance.portfolio_value_bps // ONE_CENT_BPS
        except (KalshiAPIError, KalshiRateLimitError, httpx.HTTPError):
            logger.warning("balance_fetch_failed", exc_info=True)

    async def refresh_account(self) -> None:
        """Backup REST sync for orders + positions. WS is primary data source.

        Runs every 30s as a safety net — catches anything WS missed.
        """
        # Startup safety gate: tree_mode waits for milestones to load before
        # the first trading cycle runs. Hard cap inside wait_for_ready_for_trading
        # means we never deadlock. Subsequent ticks pass through immediately
        # because _ready_for_trading stays set.
        if self._auto_config.tree_mode and not self._ready_for_trading.is_set():
            await self.wait_for_ready_for_trading()

        await self._recover_stale_books()

        # Bump sync generation so optimistic placements from this cycle
        # are protected against stale-data overwrites.
        for pair in self._scanner.pairs:
            with contextlib.suppress(KeyError):
                self._adjuster.get_ledger(pair.event_ticker).bump_sync_gen()

        try:
            # First sync: fetch ALL orders (including executed) so
            # sync_from_orders can seed fill counts — critical for same-ticker
            # pairs where sync_from_positions is unavailable.
            # Subsequent syncs: resting-only (WS handles fills in real-time).
            if not self._initial_sync_done:
                orders = await self._rest.get_all_orders()
            else:
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
                tickers = list({o.ticker for o in orders if o.remaining_count_fp100 > 0})
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
            active_ids = {o.order_id for o in orders if o.remaining_count_fp100 > 0}
            self._queue_cache = {
                oid: v for oid, v in self._queue_cache.items() if oid in active_ids
            }

            # Mark stale / purge proposals whose orders have vanished
            self._proposal_queue.tick(active_order_ids=active_ids)

            # Sync position ledgers from orders (Principle 15)
            for pair in self._scanner.pairs:
                try:
                    ledger = self._adjuster.get_ledger(pair.event_ticker)
                    # Multi-market events (temperature, crypto ranges) have
                    # many pairs sharing one event_ticker. Only sync the pair
                    # that owns the ledger — other pairs' empty resting lists
                    # would clobber the owning pair's resting state.
                    if not ledger.owns_tickers(pair.ticker_a, pair.ticker_b):
                        continue
                    ledger.sync_from_orders(orders, ticker_a=pair.ticker_a, ticker_b=pair.ticker_b)
                except KeyError:
                    pass  # Pair not registered with adjuster yet

            # Augment fills from positions API (P7/P15 — Kalshi is source
            # of truth, always). GET /portfolio/orders archives old orders,
            # but GET /portfolio/positions never does. This catches fills
            # invisible to sync_from_orders due to order archival.
            pos_map: dict[str, Position] | None = None
            try:
                market_positions = await self._rest.get_all_positions()
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
                    if not ledger.owns_tickers(pair.ticker_a, pair.ticker_b):
                        continue
                    fills = {
                        Side.A: abs(pos_a.position_fp100 // ONE_CONTRACT_FP100) if pos_a else 0,
                        Side.B: abs(pos_b.position_fp100 // ONE_CONTRACT_FP100) if pos_b else 0,
                    }
                    costs = {
                        Side.A: pos_a.total_traded_bps // ONE_CENT_BPS if pos_a else 0,
                        Side.B: pos_b.total_traded_bps // ONE_CENT_BPS if pos_b else 0,
                    }
                    fees = {
                        Side.A: pos_a.fees_paid_bps // ONE_CENT_BPS if pos_a else 0,
                        Side.B: pos_b.fees_paid_bps // ONE_CENT_BPS if pos_b else 0,
                    }
                    ledger.sync_from_positions(fills, costs, fees)

            except (KalshiAPIError, KalshiRateLimitError, httpx.HTTPError):
                logger.warning("positions_sync_failed", exc_info=True)

            self._reconcile_stale_positions(pos_map)

            self.recompute_positions()

            # Build enriched order dicts for the order log (display-cents /
            # whole-contract view; exact precision is kept in bps/fp100 on the
            # underlying Order — ledger math reads those directly).
            self._order_data = [
                {
                    "ticker": o.ticker,
                    "side": o.side,
                    "price": bps_to_cents_round(
                        o.no_price_bps if o.side == "no" else o.yes_price_bps
                    ),
                    "filled": o.fill_count_fp100 // ONE_CONTRACT_FP100,
                    "total": o.initial_count_fp100 // ONE_CONTRACT_FP100,
                    "remaining": o.remaining_count_fp100 // ONE_CONTRACT_FP100,
                    "status": o.status,
                    "time": (o.created_time[11:16] if len(o.created_time) > 16 else o.created_time),
                    "queue_pos": o.queue_position,
                }
                for o in orders
            ]

            # Full ledger reconciliation is only meaningful after the first
            # account refresh completes; otherwise startup ordering creates
            # false mismatches before authoritative state is hydrated.
            if self._account_sync_done:
                self._reconcile_with_kalshi(orders, pos_map or {})

            if not self._account_sync_done:
                self._account_sync_done = True
                logger.info("account_sync_complete")

            # Re-evaluate jumped tickers that have no pending proposal (P20)
            self.reevaluate_jumps()

            # Check for position imbalances (P16)
            await self.check_imbalances()

            # Evaluate scanner opportunities for automated bid proposals
            self.evaluate_opportunities()

            # Check for queue-stressed resting orders (price improvement)
            self.check_queue_stress()

            # Check exit-only triggers and enforce cancellations
            self._check_exit_only()
            if self._auto_config.tree_mode:
                await self._reconcile_winding_down()
            await self._enforce_all_exit_only()

            # Persist ledger fill state so restarts don't lose fills
            # (critical for same-ticker pairs where sync_from_positions is unavailable)
            if self._game_manager.on_change:
                self._game_manager.on_change()

            if not self._initial_sync_done:
                self._initial_sync_done = True
                # First sync — mark all pairs dirty so first check_imbalances
                # does a full review before WS events start driving it.
                for pair in self._scanner.pairs:
                    self._dirty_events.add(pair.event_ticker)
                logger.info("initial_sync_complete")
        except (KalshiAPIError, KalshiRateLimitError, httpx.HTTPError):
            logger.exception("refresh_account_error")

    async def refresh_queue_positions(self) -> None:
        """Fast-cadence queue poll with conservative merge."""
        try:
            tickers = self._active_market_tickers(resting_only=True)
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

        self.recompute_positions()

    async def refresh_trades(self) -> None:
        """Fetch recent trades for CPM tracking.

        Limits concurrency to 5 parallel requests and caps the entire
        batch at 30s to prevent task storms when the API is slow.
        """
        # Scope to monitored pairs only — _orders_cache contains ALL resting
        # orders on the account, which can be hundreds of unmonitored tickers.
        monitored = {t for p in self._scanner.pairs for t in (p.ticker_a, p.ticker_b)}
        tickers = [t for t in self._active_market_tickers() if t in monitored]
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
                old_fill_count_fp100 = order.fill_count_fp100

                # Monotonic update — WS can never decrease fills.
                order.status = msg.status
                order.fill_count_fp100 = max(
                    order.fill_count_fp100, msg.fill_count_fp100
                )
                order.remaining_count_fp100 = msg.remaining_count_fp100
                order.maker_fill_cost_bps = max(
                    order.maker_fill_cost_bps, msg.maker_fill_cost_bps
                )
                order.taker_fill_cost_bps = max(
                    order.taker_fill_cost_bps, msg.taker_fill_cost_bps
                )
                order.maker_fees_bps = max(order.maker_fees_bps, msg.maker_fees_bps)

                new_fills_fp100 = order.fill_count_fp100 - old_fill_count_fp100
                new_fills = new_fills_fp100 // ONE_CONTRACT_FP100
                if new_fills_fp100 > 0:
                    price_bps = (
                        msg.no_price_bps if msg.side == "no" else msg.yes_price_bps
                    )
                    price = bps_to_cents_round(price_bps)
                    self._notify(
                        f"WS fill: {new_fills} @ {price}¢ on {msg.ticker}",
                    )
                    logger.info(
                        "ws_order_fill",
                        order_id=msg.order_id,
                        ticker=msg.ticker,
                        new_fills=new_fills,
                        total_fills=order.fill_count_fp100 // ONE_CONTRACT_FP100,
                    )
                    # Enqueue WS reaction for immediate processing
                    if self._initial_sync_done:
                        for p in self._scanner.pairs:
                            if msg.ticker in (p.ticker_a, p.ticker_b):
                                self._reaction_queue.put_nowait(p.event_ticker)
                                break

                # Re-sync the affected pair's ledger and mark dirty
                for pair in self._scanner.pairs:
                    if msg.ticker in (pair.ticker_a, pair.ticker_b):
                        self._dirty_events.add(pair.event_ticker)
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
                self.recompute_positions()
                # Log order state change
                if self._data_collector is not None:
                    event_ticker = ""
                    for pair in self._scanner.pairs:
                        if msg.ticker in (pair.ticker_a, pair.ticker_b):
                            event_ticker = pair.event_ticker
                            break
                    # DB schema is integer cents / whole contracts; round from
                    # bps/fp100 for the log insert.
                    ws_price_bps = (
                        msg.no_price_bps if msg.side == "no" else msg.yes_price_bps
                    )
                    self._data_collector.log_order(
                        event_ticker=event_ticker,
                        order_id=msg.order_id,
                        ticker=msg.ticker,
                        side=msg.side,
                        status=msg.status,
                        price=bps_to_cents_round(ws_price_bps),
                        initial_count=(
                            msg.fill_count_fp100 + msg.remaining_count_fp100
                        ) // ONE_CONTRACT_FP100,
                        fill_count=msg.fill_count_fp100 // ONE_CONTRACT_FP100,
                        remaining_count=msg.remaining_count_fp100 // ONE_CONTRACT_FP100,
                        maker_fill_cost=bps_to_cents_round(msg.maker_fill_cost_bps),
                        maker_fees=bps_to_cents_round(msg.maker_fees_bps),
                        source="ws_update",
                    )
                return

        # Order not in cache — add it so WS is self-sufficient
        # Check if this order's side matches one of our pair's expected sides
        evt = self._ticker_to_event.get(msg.ticker)
        ws_pair = self.find_pair(evt) if evt else None
        expected = ws_pair is not None and msg.side in {ws_pair.side_a, ws_pair.side_b}
        if expected and msg.status in ("resting", "executed"):
            new_fill_fp100 = msg.fill_count_fp100
            new_remaining_fp100 = msg.remaining_count_fp100
            new_order = Order(
                order_id=msg.order_id,
                ticker=msg.ticker,
                action="buy",
                side=msg.side,
                status=msg.status,
                no_price_bps=msg.no_price_bps,
                yes_price_bps=msg.yes_price_bps,
                fill_count_fp100=new_fill_fp100,
                remaining_count_fp100=new_remaining_fp100,
                initial_count_fp100=new_fill_fp100 + new_remaining_fp100,
                maker_fill_cost_bps=msg.maker_fill_cost_bps,
                taker_fill_cost_bps=msg.taker_fill_cost_bps,
                maker_fees_bps=msg.maker_fees_bps,
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
            self.recompute_positions()
            # Enqueue reaction if new order arrived with fills
            if (
                new_order.fill_count_fp100 > 0
                and self._initial_sync_done
                and ws_pair is not None
            ):
                self._reaction_queue.put_nowait(ws_pair.event_ticker)

    def _on_fill(self, msg: FillMessage) -> None:
        """Handle a real-time fill from the fill WS channel.

        Marks the event dirty, enqueues a WS reaction, and checks for
        position drift against Kalshi's authoritative post_position.
        """
        # Log cents rounded from the exact bps sibling so fractional-market
        # fills don't silently lose precision.
        msg_yes_bps = msg.yes_price_bps
        logger.info(
            "ws_fill_detail",
            trade_id=msg.trade_id,
            order_id=msg.order_id,
            ticker=msg.market_ticker,
            side=msg.side,
            count=msg.count_fp100 // ONE_CONTRACT_FP100,
            price=bps_to_cents_round(msg_yes_bps),
            is_taker=msg.is_taker,
            post_position=msg.post_position_fp100 // ONE_CONTRACT_FP100,
        )

        # ── Mark dirty + enqueue reaction ──
        event_ticker = ""
        fill_pair: ArbPair | None = None
        for pair in self._scanner.pairs:
            if msg.market_ticker in (pair.ticker_a, pair.ticker_b):
                event_ticker = pair.event_ticker
                fill_pair = pair
                self._dirty_events.add(event_ticker)
                break
        if event_ticker and self._initial_sync_done:
            self._reaction_queue.put_nowait(event_ticker)

        # ── Post-position drift check (observability only) ──
        if fill_pair is not None and msg.post_position_fp100 != 0:
            try:
                ledger = self._adjuster.get_ledger(event_ticker)
                side: Side | None = None
                if fill_pair.is_same_ticker:
                    side_map = {fill_pair.side_a: Side.A, fill_pair.side_b: Side.B}
                    side = side_map.get(msg.side)
                else:
                    if msg.market_ticker == fill_pair.ticker_a:
                        side = Side.A
                    elif msg.market_ticker == fill_pair.ticker_b:
                        side = Side.B
                if side is not None:
                    kalshi_pos = abs(msg.post_position_fp100 // ONE_CONTRACT_FP100)
                    ledger_pos = ledger.filled_count(side)
                    if kalshi_pos != ledger_pos:
                        logger.warning(
                            "ws_fill_position_drift",
                            event_ticker=event_ticker,
                            side=side.value,
                            kalshi_post_position=kalshi_pos,
                            ledger_filled=ledger_pos,
                            fill_count=msg.count_fp100 // ONE_CONTRACT_FP100,
                        )
            except KeyError:
                pass

        # ── Data collector logging ──
        if self._data_collector is not None:
            placed_at = self._order_placed_at.get(msg.order_id)
            time_since = time.monotonic() - placed_at if placed_at else None
            qp = self._queue_cache.get(msg.order_id)
            # log_fill schema is integer cents / whole contracts; round from
            # bps/fp100 to preserve DB-layer compatibility.
            self._data_collector.log_fill(
                event_ticker=event_ticker,
                trade_id=msg.trade_id,
                order_id=msg.order_id,
                ticker=msg.market_ticker,
                side=msg.side,
                price=bps_to_cents_round(msg_yes_bps),
                count=msg.count_fp100 // ONE_CONTRACT_FP100,
                fee_cost=bps_to_cents_round(msg.fee_cost_bps),
                is_taker=msg.is_taker,
                post_position=msg.post_position_fp100 // ONE_CONTRACT_FP100,
                queue_position=qp,
                time_since_order=time_since,
            )

    # ── Lifecycle event handlers ────────────────────────────────

    def _is_our_market(self, ticker: str) -> bool:
        """Check if a ticker belongs to a market we're actively tracking."""
        return any(ticker in (p.ticker_a, p.ticker_b) for p in self._scanner.pairs)

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

            # Set exit-only immediately — market is no longer tradeable
            if event_ticker and event_ticker not in self._exit_only_events:
                self._exit_only_events.add(event_ticker)
                self._enforce_exit_only_sync(event_ticker)

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
            revenue_bps = s.revenue_bps
            fee_cost_bps = s.fee_cost_bps
            net_bps = revenue_bps - fee_cost_bps
            self._notify(
                f"Settlement {ticker}: "
                f"{'won' if s.market_result == 'yes' else 'lost'} "
                f"rev {format_bps_as_dollars_display(revenue_bps)} "
                f"fee {format_bps_as_dollars_display(fee_cost_bps)} "
                f"net {format_bps_as_dollars_display(net_bps)}"
            )
            logger.info(
                "settlement_fetched",
                ticker=ticker,
                result=s.market_result,
                revenue_bps=revenue_bps,
                fee_cost_bps=fee_cost_bps,
                no_count_fp100=s.no_count_fp100,
                yes_count_fp100=s.yes_count_fp100,
            )
            # Cache settlement with our estimated P&L (still available at this point)
            if self._settlement_cache is not None:
                est_pnl: int | None = None
                sub = ""
                for ps in self._position_summaries:
                    if ps.event_ticker == s.event_ticker:
                        est_pnl = bps_to_cents_round(int(ps.locked_profit_bps))
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

        # Exact-precision reads from the ledger; legacy accessors below
        # remain for DB schema compatibility (schema is cents / whole
        # contracts).
        filled_a_fp100 = ledger.filled_count_fp100(Side.A)
        filled_b_fp100 = ledger.filled_count_fp100(Side.B)
        filled_a = filled_a_fp100 // ONE_CONTRACT_FP100
        filled_b = filled_b_fp100 // ONE_CONTRACT_FP100
        results = self._settled_markets.get(event_ticker, {})
        result_a = results.get(pair.ticker_a, "")
        result_b = results.get(pair.ticker_b, "")

        prefix = event_ticker.split("-")[0]
        from talos.ui.widgets import _SPORT_LEAGUE

        sport, league = _SPORT_LEAGUE.get(prefix, ("", ""))

        total_cost_a_bps = ledger.filled_total_cost_bps(Side.A)
        total_cost_b_bps = ledger.filled_total_cost_bps(Side.B)
        total_fees_a_bps = ledger.filled_fees_bps(Side.A)
        total_fees_b_bps = ledger.filled_fees_bps(Side.B)

        # Compute revenue in bps: our side wins → payout = 1 contract = $1.
        # Exact derivation: revenue_bps = filled_fp100 * ONE_DOLLAR_BPS //
        # ONE_CONTRACT_FP100  (fp100 × bps / fp100 = bps, units cancel).
        side_a = pair.side_a if pair else "no"
        side_b = pair.side_b if pair else "no"
        revenue_bps = 0
        if result_a == side_a:
            revenue_bps += filled_a_fp100 * ONE_DOLLAR_BPS // ONE_CONTRACT_FP100
        if result_b == side_b:
            revenue_bps += filled_b_fp100 * ONE_DOLLAR_BPS // ONE_CONTRACT_FP100

        total_pnl_bps = (
            revenue_bps
            - total_cost_a_bps
            - total_cost_b_bps
            - total_fees_a_bps
            - total_fees_b_bps
        )

        # Round bps → cents at the DB-schema boundary.
        total_cost_a = bps_to_cents_round(total_cost_a_bps)
        total_cost_b = bps_to_cents_round(total_cost_b_bps)
        total_fees_a = bps_to_cents_round(total_fees_a_bps)
        total_fees_b = bps_to_cents_round(total_fees_b_bps)
        revenue = bps_to_cents_round(revenue_bps)
        total_pnl = bps_to_cents_round(total_pnl_bps)

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
            # Same-ticker YES/NO pairs: positions API reports net (YES+NO=0),
            # so position=0 doesn't mean settled. Skip stale detection.
            if pair.is_same_ticker:
                continue
            pos_a = pos_map.get(pair.ticker_a)
            pos_b = pos_map.get(pair.ticker_b)
            both_zero = (pos_a is None or pos_a.position_fp100 == 0) and (
                pos_b is None or pos_b.position_fp100 == 0
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
            # Only reconcile the pair that owns the ledger — other pairs
            # sharing the same event_ticker would produce false alarms.
            if not ledger.owns_tickers(pair.ticker_a, pair.ticker_b):
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
                order_fill_contracts = order.fill_count_fp100 // ONE_CONTRACT_FP100
                order_remaining_contracts = order.remaining_count_fp100 // ONE_CONTRACT_FP100
                if order_fill_contracts > 0:
                    kalshi_fills[side] += order_fill_contracts
                if order_remaining_contracts > 0 and order.status in (
                    "resting",
                    "executed",
                ):
                    # Skip orders the ledger knows are cancelled but Kalshi's
                    # GET still returns due to eventual consistency
                    if order.order_id in ledger._recently_cancelled:
                        continue
                    kalshi_resting[side] += order_remaining_contracts
                    kalshi_resting_order_count[side] += 1

            # ── Augment fills from positions API ──
            pos_fills: dict[Side, int] = {Side.A: 0, Side.B: 0}
            if not pair.is_same_ticker:
                for side, ticker in ((Side.A, pair.ticker_a), (Side.B, pair.ticker_b)):
                    pos = pos_map.get(ticker)
                    if pos is not None:
                        pos_fills[side] = abs(pos.position_fp100 // ONE_CONTRACT_FP100)
            # Authoritative fill count = max of all sources.
            # Ledger persists fills that may have dropped from Kalshi's
            # orders API time window (old fully-filled orders expire).
            auth_fills = {
                s: max(kalshi_fills[s], pos_fills[s], ledger.filled_count(s))
                for s in (Side.A, Side.B)
            }

            for side in (Side.A, Side.B):
                sl = side.value  # "A" or "B"

                # Check 1: Unit overcommit (hard invariant P16)
                # Allow extra resting when closing a cross-side fill gap
                # (catch-up orders are risk-reducing, not true overcommits).
                filled_in_unit = auth_fills[side] % ledger.unit_size
                other_side = Side.B if side == Side.A else Side.A
                fill_gap = max(0, auth_fills[other_side] - auth_fills[side])
                allowed = max(ledger.unit_size - filled_in_unit, fill_gap)
                if kalshi_resting[side] > allowed:
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
                    # Ensure check_imbalances processes this event next cycle
                    # (it may be skipped by the dirty-set filter otherwise)
                    self._dirty_events.add(pair.event_ticker)
                    # Priority flag — processed before normal rebalances so
                    # overcommit resolution gets API budget first.
                    self._overcommit_events.add(pair.event_ticker)
                    # Store reconciliation-derived target so resolution uses
                    # the same data as detection (ledger fill_gap may differ).
                    targets = self._overcommit_targets.setdefault(pair.event_ticker, {})
                    targets[sl] = allowed

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
                mismatch_key = (pair.event_ticker, sl)
                mismatch_state = (ledger_fills, auth_fills[side])
                if ledger_fills != auth_fills[side]:
                    if self._last_reconcile_fill_mismatch.get(mismatch_key) == mismatch_state:
                        continue
                    self._last_reconcile_fill_mismatch[mismatch_key] = mismatch_state
                    logger.warning(
                        "reconcile_fill_mismatch",
                        event_ticker=pair.event_ticker,
                        side=sl,
                        ledger=ledger_fills,
                        kalshi=auth_fills[side],
                    )
                else:
                    self._last_reconcile_fill_mismatch.pop(mismatch_key, None)

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
        self,
        ticker: str,
        *,
        side: str = "no",
        at_top: bool = False,
        trigger: str = "ws_top_change",
    ) -> None:
        """Evaluate a jump and enqueue a proposal if appropriate.

        Shared by on_top_of_market_change (with toast) and reevaluate_jumps
        (silent). The notification is the caller's responsibility.

        ``trigger`` labels the origin for the replay timeline.
        """
        evt_ticker = self._adjuster.resolve_event(ticker)
        exit_only = self.is_exit_only(evt_ticker) if evt_ticker else False
        proposal = self._adjuster.evaluate_jump(
            ticker, at_top, exit_only=exit_only, side=side, trigger=trigger
        )
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

    def _log_imbalance_outcome(
        self,
        event_ticker: str,
        outcome: str,
        reason: str,
    ) -> None:
        """Dedup'd decision log for imbalance/topup paths.

        Writes a row only when the outcome differs from the last
        observation for this event — keeps the replay timeline
        readable while still capturing every transition.
        """
        if self._data_collector is None:
            return
        if self._last_imbalance_outcome.get(event_ticker) == outcome:
            return
        self._last_imbalance_outcome[event_ticker] = outcome
        self._data_collector.log_decision(
            event_ticker=event_ticker,
            trigger="imbalance_check",
            outcome=outcome,
            reason=reason,
        )

    # ── Scoped reaction helpers (WS-triggered) ────────────────────

    def _reevaluate_jumps_for(self, event_ticker: str, pair: ArbPair) -> None:
        """Scoped jump reevaluation for a single pair.

        Extracts the per-pair logic from reevaluate_jumps() — same behavior,
        single event scope.
        """
        pending_keys = {p.key for p in self._proposal_queue.pending()}
        has_withdraw = any(
            k.event_ticker == event_ticker and k.kind == "withdraw" for k in pending_keys
        )
        if has_withdraw:
            return
        for ticker, side_label, pair_side in [
            (pair.ticker_a, "A", pair.side_a),
            (pair.ticker_b, "B", pair.side_b),
        ]:
            at_top = self._tracker.is_at_top(ticker, side=pair_side)
            if at_top is not None and not at_top:
                has_proposal = any(
                    k.event_ticker == event_ticker
                    and k.side == side_label
                    and k.kind in ("adjustment", "hold")
                    for k in pending_keys
                )
                if not has_proposal:
                    book_top = self._tracker.book_top_price(ticker, side=pair_side) or 0
                    resting = self._tracker.resting_price(ticker, side=pair_side) or 0
                    eval_key = (book_top, resting)
                    if eval_key == self._last_jump_eval.get(ticker):
                        if self._data_collector is not None:
                            self._data_collector.log_decision(
                                event_ticker=event_ticker,
                                ticker=ticker,
                                side=side_label,
                                trigger="reevaluate_jumps_for",
                                outcome="skip_unchanged",
                                reason=(
                                    f"book_top={book_top} resting={resting} "
                                    "unchanged since last eval"
                                ),
                                book_top=book_top,
                                resting_price=resting,
                            )
                        continue
                    self._last_jump_eval[ticker] = eval_key
                    self._generate_jump_proposal(
                        ticker, side=pair_side, trigger="reevaluate_jumps_for"
                    )

    async def _check_imbalance_for(self, event_ticker: str, pair: ArbPair) -> None:
        """Scoped imbalance check for a single event.

        Conservative v1: cross-side rebalance + overcommit reduction only.
        Mid-unit topups and opportunity evaluation remain poll-only
        (handled by check_imbalances / evaluate_opportunities in
        refresh_account every 30s). This scope limitation is intentional —
        the WS path covers safety-critical reactions only.
        """
        try:
            ledger = self._adjuster.get_ledger(event_ticker)
        except KeyError:
            return

        snapshot = self._scanner.all_snapshots.get(event_ticker)
        proposal = compute_rebalance_proposal(
            event_ticker,
            ledger,
            pair,
            snapshot,
            self._display_name(event_ticker),
            self._feed.book_manager,
        )
        if proposal is not None and proposal.rebalance is not None:
            self._log_imbalance_outcome(
                event_ticker,
                "rebalance_proposed",
                f"rebalance on {proposal.rebalance.side} (ws)",
            )
            await _execute_rebalance(
                proposal.rebalance,
                rest_client=self._rest,
                adjuster=self._adjuster,
                scanner=self._scanner,
                notify=self._notify,
                cancel_with_verify=self.cancel_order_with_verify,
                feed=self._feed,
                name=self._display_name(event_ticker),
            )
            return

        # No cross-side imbalance — check single-side overcommit
        overcommit = compute_overcommit_reduction(
            event_ticker,
            ledger,
            pair,
            self._display_name(event_ticker),
        )
        if overcommit is not None:
            self._log_imbalance_outcome(
                event_ticker,
                "overcommit_reduction",
                f"overcommit reduction on {overcommit.side} (ws)",
            )
            await _execute_rebalance(
                overcommit,
                rest_client=self._rest,
                adjuster=self._adjuster,
                scanner=self._scanner,
                notify=self._notify,
                cancel_with_verify=self.cancel_order_with_verify,
                feed=self._feed,
                name=self._display_name(event_ticker),
            )
        else:
            self._log_imbalance_outcome(
                event_ticker,
                "imbalance_clean",
                "no cross-side imbalance or overcommit (ws)",
            )

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
                            if self._data_collector is not None:
                                self._data_collector.log_decision(
                                    event_ticker=pair.event_ticker,
                                    ticker=ticker,
                                    side=side_label,
                                    trigger="reevaluate_jumps",
                                    outcome="skip_unchanged",
                                    reason=(
                                        f"book_top={book_top} resting={resting} "
                                        "unchanged since last eval"
                                    ),
                                    book_top=book_top,
                                    resting_price=resting,
                                )
                            continue
                        self._last_jump_eval[ticker] = eval_key
                        self._generate_jump_proposal(
                            ticker, side=pair_side, trigger="reevaluate_jumps"
                        )

    async def check_imbalances(self) -> None:
        """Detect and auto-execute rebalance catch-ups (P16).

        Event-driven: only processes pairs flagged dirty by WS callbacks
        (fills, orderbook changes). Full sweep every 10 cycles (~5 min)
        as safety net for changes WS doesn't report (expirations, Kalshi-
        side cancels).
        """
        # Determine which events to check this cycle
        self._full_sweep_counter += 1
        full_sweep = self._full_sweep_counter >= 10
        if full_sweep:
            self._full_sweep_counter = 0

        dirty = self._dirty_events.copy()
        self._dirty_events.clear()
        overcommit_priority = self._overcommit_events.copy()
        self._overcommit_events.clear()
        overcommit_targets = self._overcommit_targets.copy()
        self._overcommit_targets.clear()
        # Overcommit events must be in dirty set for the filter below
        dirty |= overcommit_priority

        # Process overcommit events first so they get API budget before
        # normal rebalances exhaust the rate limit.
        pairs_ordered = (
            sorted(
                self._scanner.pairs,
                key=lambda p: 0 if p.event_ticker in overcommit_priority else 1,
            )
            if overcommit_priority
            else self._scanner.pairs
        )

        executed_this_cycle: set[str] = set()
        for pair in pairs_ordered:
            if pair.event_ticker in executed_this_cycle:
                continue
            if not full_sweep and pair.event_ticker not in dirty:
                continue

            # Shared claim — skip if WS consumer owns this event
            if not self._claim_event(pair.event_ticker, "poll"):
                logger.debug("poll_skipped_ws_claimed", event_ticker=pair.event_ticker)
                continue

            try:
                ledger = self._adjuster.get_ledger(pair.event_ticker)
            except KeyError:
                self._release_event(pair.event_ticker, "poll")
                continue

            try:
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
                    # (balanced committed counts but unit capacity violated).
                    # Pass reconciliation-derived targets when available — these
                    # use auth_fills (max of all sources) so the fill_gap is
                    # authoritative and won't miss overcommits the ledger misses.
                    overcommit = compute_overcommit_reduction(
                        pair.event_ticker,
                        ledger,
                        pair,
                        self._display_name(pair.event_ticker),
                        reconciled_targets=overcommit_targets.get(pair.event_ticker),
                    )
                    if overcommit is not None:
                        self._log_imbalance_outcome(
                            pair.event_ticker,
                            "overcommit_reduction",
                            f"overcommit reduction on {overcommit.side}",
                        )
                        await _execute_rebalance(
                            overcommit,
                            rest_client=self._rest,
                            adjuster=self._adjuster,
                            scanner=self._scanner,
                            notify=self._notify,
                            cancel_with_verify=self.cancel_order_with_verify,
                            feed=self._feed,
                            name=self._display_name(pair.event_ticker),
                        )
                        executed_this_cycle.add(pair.event_ticker)
                        continue

                    # No overcommit — check for mid-unit top-up
                    if not self.is_exit_only(pair.event_ticker):
                        dn = self._display_name(pair.event_ticker)
                        topup_needs = compute_topup_needs(ledger, pair, snapshot)
                        if not topup_needs:
                            self._log_imbalance_outcome(
                                pair.event_ticker,
                                "imbalance_clean",
                                "no rebalance/overcommit/topup needed",
                            )
                        for side, (qty, price) in topup_needs.items():
                            ok, reason = ledger.is_placement_safe(
                                side, qty, price, rate=pair.fee_rate
                            )
                            if not ok:
                                self._notify(
                                    f"[{dn}] Top-up BLOCKED ({side.value}): {reason}",
                                    "warning",
                                )
                                self._log_imbalance_outcome(
                                    pair.event_ticker,
                                    f"topup_blocked_{side.value}",
                                    f"top-up {side.value} blocked: {reason}",
                                )
                                continue
                            ticker = pair.ticker_a if side == Side.A else pair.ticker_b
                            # Section 8 startup gate — block risk-
                            # increasing placement until ledger confirmed.
                            if not await self._wait_for_ledger_ready(
                                pair, "top-up"
                            ):
                                continue
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
                                    f"[{dn}] Top-up {side.value}: {qty} @ {price}c",
                                    "information",
                                )
                                self._log_imbalance_outcome(
                                    pair.event_ticker,
                                    f"topup_placed_{side.value}",
                                    f"top-up {side.value}: {qty} @ {price}c",
                                )
                                logger.info(
                                    "topup_placed",
                                    event_ticker=pair.event_ticker,
                                    side=side.value,
                                    qty=qty,
                                    price=price,
                                )
                            except (
                                KalshiAPIError,
                                KalshiRateLimitError,
                                httpx.HTTPError,
                            ) as e:
                                self._notify(
                                    f"[{dn}] Top-up FAILED ({side.value}): {type(e).__name__}: {e}",
                                    "error",
                                )
                                logger.exception(
                                    "topup_error",
                                    event_ticker=pair.event_ticker,
                                    side=side.value,
                                )
                                # "post only cross" = stale local orderbook — resubscribe
                                if (
                                    isinstance(e, KalshiAPIError)
                                    and "post only cross" in str(e).lower()
                                ):
                                    await self._feed.unsubscribe(ticker)
                                    await self._feed.subscribe(ticker)
                    continue

                # Auto-execute catch-up — no ProposalQueue
                self._log_imbalance_outcome(
                    pair.event_ticker,
                    "rebalance_proposed",
                    f"rebalance on {proposal.rebalance.side}",
                )
                await _execute_rebalance(
                    proposal.rebalance,
                    rest_client=self._rest,
                    adjuster=self._adjuster,
                    scanner=self._scanner,
                    notify=self._notify,
                    cancel_with_verify=self.cancel_order_with_verify,
                    feed=self._feed,
                    name=self._display_name(pair.event_ticker),
                )
                executed_this_cycle.add(pair.event_ticker)
            finally:
                self._release_event(pair.event_ticker, "poll")

    def evaluate_opportunities(self) -> None:
        """Run OpportunityProposer against all scanner pairs.

        Only active when automation is enabled.
        """
        if not self._auto_config.enabled:
            return
        pending_keys = {p.key for p in self._proposal_queue.pending()}
        vols = self._game_manager.volumes_24h
        for pair in self._scanner.pairs:
            if self._game_manager.is_blacklisted(pair.event_ticker):
                continue
            opp = self._scanner.get_opportunity(pair.event_ticker)
            if opp is None:
                continue
            try:
                ledger = self._adjuster.get_ledger(pair.event_ticker)
            except KeyError:
                continue
            pair_volume = min(
                vols.get(pair.ticker_a, 0),
                vols.get(pair.ticker_b, 0),
            )
            proposal = self._proposer.evaluate(
                pair,
                opp,
                ledger,
                pending_keys,
                display_name=self._display_name(pair.event_ticker),
                exit_only=self.is_exit_only(pair.event_ticker),
                pair_volume_24h=pair_volume,
            )
            if proposal is not None:
                self._proposal_queue.add(proposal)
                pending_keys.add(proposal.key)

    def check_queue_stress(self) -> None:
        """Detect resting orders stuck deep in queue and propose 1c improvements.

        Scans partially-filled pairs where the behind side's ETA exceeds
        time remaining before game start. Generates ProposedQueueImprovement
        proposals that flow through the standard ProposalQueue.
        """
        if self._game_status_resolver is None:
            return

        now = datetime.now(UTC)

        for pair in self._scanner.pairs:
            event_ticker = pair.event_ticker

            # Skip exit-only events
            if event_ticker in self._exit_only_events:
                continue

            # Skip if a proposal already exists for this event
            pending_kinds = self._pending_kinds_cache.get(event_ticker, set())
            if pending_kinds:
                continue

            try:
                ledger = self._adjuster.get_ledger(event_ticker)
            except KeyError:
                continue

            filled_a = ledger.filled_count(Side.A)
            filled_b = ledger.filled_count(Side.B)

            # Must be partially filled — at least one side has fills
            if filled_a == 0 and filled_b == 0:
                continue
            # Both sides equal means fully matched — no stuck side
            if filled_a == filled_b:
                continue

            # Determine the behind (stuck) side
            if filled_a < filled_b:
                behind_side = Side.A
                ahead_fills = filled_b
            elif filled_b < filled_a:
                behind_side = Side.B
                ahead_fills = filled_a
            else:
                continue  # Equal fills

            behind_fills = ledger.filled_count(behind_side)
            _ = ahead_fills  # used for clarity above

            # Behind side must have a resting order
            order_id = ledger.resting_order_id(behind_side)
            if order_id is None:
                continue

            resting_price = ledger.resting_price(behind_side)
            if resting_price <= 0:
                continue

            # Need queue position and ETA
            queue_pos = self._queue_cache.get(order_id)
            if queue_pos is None:
                continue

            ticker = pair.ticker_a if behind_side == Side.A else pair.ticker_b
            eta = self._cpm.eta_minutes(ticker, queue_pos)
            if eta is None:
                # CPM = 0 means dead market — treat as infinite ETA
                cpm = self._cpm.cpm(ticker)
                if cpm is not None and cpm == 0:
                    eta = float("inf")
                else:
                    continue

            # Compute time remaining until game
            gs = self._game_status_resolver.get(event_ticker)
            game_time: datetime | None = None
            if gs is not None and gs.scheduled_start is not None:
                game_time = gs.scheduled_start
            elif pair.close_time:
                with contextlib.suppress(ValueError, TypeError):
                    game_time = datetime.fromisoformat(pair.close_time)

            if game_time is None:
                continue

            time_remaining_minutes = (game_time - now).total_seconds() / 60
            if time_remaining_minutes <= 0:
                continue  # Game already started

            # Trigger: ETA exceeds time remaining
            if eta <= time_remaining_minutes:
                continue

            # Proposed improvement: +1c
            improved_price = resting_price + 1

            # Safety gate #1: profitability (P18)
            ahead_side = behind_side.other
            other_avg = ledger.open_avg_filled_price(ahead_side)
            if other_avg <= 0:
                continue

            eff_improved_bps = fee_adjusted_cost_bps(
                improved_price * ONE_CENT_BPS, rate=pair.fee_rate
            )
            eff_other_bps = fee_adjusted_cost_bps(
                int(round(other_avg)) * ONE_CENT_BPS, rate=pair.fee_rate
            )
            if eff_improved_bps + eff_other_bps >= ONE_DOLLAR_BPS:
                continue

            # Safety gate #2: no spread crossing
            kalshi_side = pair.side_a if behind_side == Side.A else pair.side_b
            ask_level = self._feed.book_manager.best_ask(ticker, side=kalshi_side)
            if (
                ask_level is not None
                and improved_price >= ask_level.price_bps // ONE_CENT_BPS
            ):
                continue

            # Build proposal
            name = self._display_name(event_ticker)
            eta_str = f"{eta / 60:.0f}h" if eta >= 60 else f"{eta:.0f}m"
            tr_str = (
                f"{time_remaining_minutes / 60:.0f}h"
                if time_remaining_minutes >= 60
                else f"{time_remaining_minutes:.0f}m"
            )
            queue_k = f"{queue_pos / 1000:.0f}k" if queue_pos >= 1000 else str(queue_pos)

            summary = (
                f"QUEUE: #{pair.talos_id} {name} "
                f"{resting_price}c → {improved_price}c "
                f"(queue {queue_k}, ETA {eta_str}, game in {tr_str})"
            )
            resting_n = ledger.resting_count(behind_side)
            detail = (
                f"{behind_side.value}-side stuck: "
                f"{behind_fills} filled, {resting_n} resting @ {resting_price}c. "
                f"Other side avg {other_avg:.1f}c."
            )

            key = ProposalKey(
                event_ticker=event_ticker,
                side=behind_side.value,
                kind="queue_improve",
            )
            qi = ProposedQueueImprovement(
                event_ticker=event_ticker,
                side=behind_side.value,
                order_id=order_id,
                ticker=ticker,
                current_price=resting_price,
                improved_price=improved_price,
                current_queue=queue_pos,
                eta_minutes=eta,
                time_remaining_minutes=time_remaining_minutes,
                other_side_avg=other_avg,
                kalshi_side=kalshi_side,
            )
            proposal = Proposal(
                key=key,
                kind="queue_improve",
                summary=summary,
                detail=detail,
                created_at=datetime.now(UTC),
                queue_improve=qi,
            )
            self._proposal_queue.add(proposal)
            logger.info(
                "queue_improvement_proposed",
                event_ticker=event_ticker,
                side=behind_side.value,
                current_price=resting_price,
                improved_price=improved_price,
                eta_minutes=eta,
                time_remaining_minutes=time_remaining_minutes,
            )

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

        # Hard safety gate — pair placements check the NEW pair of prices for
        # profitability (P18), not each new price vs historical avg. This allows
        # re-entry after market moves. P16 (unit capacity) still checked per-side.
        if ledger is not None:
            pair = self.find_pair(ledger.event_ticker)
            fee_rate = pair.fee_rate if pair is not None else MAKER_FEE_RATE
            # P18: pair profitability — new prices must sum < 100 after fees
            pair_edge_bps = fee_adjusted_edge_bps(
                bid.no_a * ONE_CENT_BPS, bid.no_b * ONE_CENT_BPS, rate=fee_rate
            )
            if pair_edge_bps < 0:
                name = self._display_name(ledger.event_ticker)
                self._notify(
                    f"Bid BLOCKED {name}: pair not profitable "
                    f"({bid.no_a}+{bid.no_b} edge={pair_edge_bps / ONE_CENT_BPS:.1f}c)",
                    "error",
                    toast=True,
                )
                return
            # P16: unit capacity — per-side check
            for side, price in [(Side.A, bid.no_a), (Side.B, bid.no_b)]:
                ok, reason = ledger.is_placement_safe(side, bid.qty, price, rate=fee_rate)
                if not ok and "unit" in reason.lower():
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
        pair = self.find_pair(event_ticker) if event_ticker else None
        if pair is None:
            logger.error("place_bids_no_pair", ticker_a=bid.ticker_a)
            self._notify(f"BLOCKED: no pair found for {bid.ticker_a}", "error")
            return

        side_a = pair.side_a
        side_b = pair.side_b

        # Guard: never send a 0-price order (no orderbook data)
        if bid.no_a <= 0 or bid.no_b <= 0:
            logger.warning(
                "place_bids_zero_price",
                ticker_a=bid.ticker_a,
                no_a=bid.no_a,
                no_b=bid.no_b,
            )
            return

        # Section 8 startup gate — block paired placement until ledger
        # confirmed. Cancel path is NOT gated (F31).
        if not await self._wait_for_ledger_ready(pair, "place_bids"):
            return

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
            try:
                order_b = await self._rest.create_order(
                    ticker=bid.ticker_b,
                    action="buy",
                    side=side_b,
                    yes_price=bid.no_b if side_b == "yes" else None,
                    no_price=bid.no_b if side_b == "no" else None,
                    count=bid.qty,
                )
            except (KalshiAPIError, KalshiRateLimitError, httpx.HTTPError):
                # Order A succeeded but order B failed — cancel A to avoid
                # unhedged exposure. Rebalance would eventually catch this,
                # but the exposure window is dangerous.
                logger.warning(
                    "place_bids_cancel_orphan_a",
                    ticker_a=bid.ticker_a,
                    order_id_a=order_a.order_id,
                )
                try:
                    # F36: route compensating cancel through verify wrapper
                    # so F33 resync runs rather than blind optimistic-clear.
                    await self.cancel_order_with_verify(order_a.order_id, pair)
                except (KalshiAPIError, KalshiRateLimitError, httpx.HTTPError):
                    # Compensating cancel failed — log separately but preserve
                    # the original placement failure (re-raised below).
                    logger.error(
                        "place_bids_orphan_cancel_failed",
                        order_id=order_a.order_id,
                        exc_info=True,
                    )
                raise
            logger.info("order_placed", ticker=bid.ticker_b, order_id=order_b.order_id)
            dn = self._display_name(evt_for_bid) if evt_for_bid else bid.ticker_a
            self._notify(
                f"[{dn}] Orders placed: {bid.ticker_a} @ {bid.no_a}c, {bid.ticker_b} @ {bid.no_b}c",
            )

            # Optimistic ledger update — prevents duplicate proposals when a
            # concurrent refresh_account has stale data (the orders weren't in
            # the API response it fetched before placement). The generation
            # guard in sync_from_orders prevents stale syncs from clearing this.
            if ledger is not None:
                ledger.record_placement(
                    Side.A,
                    order_a.order_id,
                    order_a.remaining_count_fp100 // ONE_CONTRACT_FP100,
                    bid.no_a,
                )
                ledger.record_placement(
                    Side.B,
                    order_b.order_id,
                    order_b.remaining_count_fp100 // ONE_CONTRACT_FP100,
                    bid.no_b,
                )
            # Track placement time for fill latency calculation
            import time as _pt

            _now = _pt.monotonic()
            self._order_placed_at[order_a.order_id] = _now
            self._order_placed_at[order_b.order_id] = _now
            # Add to orders cache so WS handler can match future updates
            self._orders_cache.extend([order_a, order_b])
            # Log to data collector (schema is integer cents / whole contracts).
            if self._data_collector is not None:
                for order in (order_a, order_b):
                    price_bps = (
                        order.no_price_bps if order.side == "no" else order.yes_price_bps
                    )
                    self._data_collector.log_order(
                        event_ticker=pair.api_event_ticker,
                        order_id=order.order_id,
                        ticker=order.ticker,
                        side=order.side,
                        status=order.status,
                        price=bps_to_cents_round(price_bps),
                        initial_count=order.initial_count_fp100 // ONE_CONTRACT_FP100,
                        fill_count=order.fill_count_fp100 // ONE_CONTRACT_FP100,
                        remaining_count=order.remaining_count_fp100 // ONE_CONTRACT_FP100,
                        source="auto_accept" if self._auto_config.enabled else "manual",
                    )
        except KalshiRateLimitError:
            raise  # Let auto-accept back off
        except (KalshiAPIError, httpx.HTTPError) as e:
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
            # 403 "not permitted" = market restricted, auto-blacklist to stop retrying
            is_not_permitted = (
                isinstance(e, KalshiAPIError)
                and e.status_code == 403
                and "not_permitted" in str(e).lower()
            )
            if is_not_permitted and event_ticker:
                series = event_ticker.split("-")[0]
                self._game_manager.add_to_blacklist(series)
                if self.on_blacklist_change is not None:
                    self.on_blacklist_change(self._game_manager.ticker_blacklist)
                logger.info("auto_blacklisted_not_permitted", series=series)
                self._notify(f"Auto-blacklisted {series} (not permitted)", "warning")

            # 409 "market_closed" = market no longer tradeable. Set exit-only
            # so resting orders are cancelled and game auto-removes.
            is_market_closed = (
                isinstance(e, KalshiAPIError)
                and e.status_code == 409
                and "market_closed" in str(e).lower()
            )
            if is_market_closed and event_ticker and event_ticker not in self._exit_only_events:
                self._exit_only_events.add(event_ticker)
                self._enforce_exit_only_sync(event_ticker)
                name = self._display_name(event_ticker)
                self._notify(
                    f"Exit-only ON: {name} (market closed)",
                    "warning",
                )
                logger.info(
                    "exit_only_market_closed",
                    event_ticker=event_ticker,
                )

            is_cross = isinstance(e, KalshiAPIError) and "post only cross" in str(e).lower()
            if is_cross:
                for ticker in (bid.ticker_a, bid.ticker_b):
                    await self._feed.unsubscribe(ticker)
                    await self._feed.subscribe(ticker)
                logger.info(
                    "orderbook_resync_after_cross",
                    ticker_a=bid.ticker_a,
                    ticker_b=bid.ticker_b,
                )

    async def add_games(self, urls: list[str], source: str = "scan") -> list[ArbPair]:
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
            return pairs
        except MarketAdmissionError as exc:
            # Phase 0: fractional / sub-cent shape rejected by
            # GameManager.add_game. Surface a specific operator-visible
            # toast instead of the generic "Error: ..." path below.
            self._notify(
                f"Market rejected (admission guard): {exc}",
                "error",
                toast=True,
            )
            logger.warning("add_games_admission_rejected", reason=str(exc))
            return []
        except Exception as e:
            from talos.game_manager import MarketPickerNeeded

            if isinstance(e, MarketPickerNeeded):
                raise  # Propagate to UI for market picker
            self._notify(f"Error: {e}", "error", toast=True)
            logger.exception("add_games_error")
            return []

    async def add_market_pairs(
        self,
        event: Event,
        markets: list[Market],
    ) -> list[ArbPair]:
        """Add YES/NO arb pairs for selected markets from a non-sports event.

        Called from the UI after the market picker selects markets.
        Handles full engine wiring: adjuster, game status, persistence.
        """
        from talos.models.market import Event as EventModel
        from talos.models.market import Market as MarketModel

        if not isinstance(event, EventModel):
            logger.warning(
                "add_market_pairs_bad_event_type",
                event_type=type(event).__name__,
            )
            return []
        pairs: list[ArbPair] = []
        # Phase 0: collect per-market admission rejections so we can surface
        # a consolidated notification after the loop (separate from the
        # success toast) rather than losing rejected tickers into the
        # generic "add_market_pair_failed" warning log.
        rejected_tickers: list[tuple[str, str]] = []
        for market in markets:
            if not isinstance(market, MarketModel):
                logger.warning(
                    "add_market_pairs_bad_market_type",
                    market_type=type(market).__name__,
                )
                continue
            try:
                pair = await self._game_manager.add_market_as_pair(event, market)
                self._adjuster.add_event(pair)
                if self._game_status_resolver is not None:
                    self._game_status_resolver.set_expiration(
                        pair.event_ticker,
                        pair.expected_expiration_time,
                    )
                pairs.append(pair)
            except MarketAdmissionError as exc:
                rejected_tickers.append((market.ticker, str(exc)))
                logger.warning(
                    "add_market_pair_admission_rejected",
                    market_ticker=market.ticker,
                    reason=str(exc),
                )
            except Exception:
                logger.warning(
                    "add_market_pair_failed",
                    market_ticker=getattr(market, "ticker", "?"),
                    exc_info=True,
                )
        if pairs:
            self._notify(f"Added {len(pairs)} market pair(s)", toast=True)
        if rejected_tickers:
            reasons = "\n".join(f"  - {t}: {r}" for t, r in rejected_tickers)
            self._notify(
                f"Rejected {len(rejected_tickers)} market(s) by admission "
                f"guard:\n{reasons}",
                "error",
                toast=True,
            )
        return pairs

    async def add_pairs_from_selection(self, records: list[dict[str, Any]]) -> CommitResult:
        """Commit path for tree-selected pairs.

        Returns a ``CommitResult`` summarising the outcome:
          * ``admitted``: pairs that passed admission and flowed through the
            full 6-step pipeline (including pre-existing no-op pairs).
          * ``rejected``: ``(record, MarketAdmissionError)`` tuples for
            records whose live market shape fails Phase 0 invariants.

        Mirrors add_games (engine.py:~2839) step-for-step, with an
        admission guard inserted BEFORE Step 1 on a per-record basis:
          0. fetch fresh Market for ticker_a + ticker_b via _rest, run
             validate_market_for_admission — MarketAdmissionError is
             captured into ``result.rejected`` and the record is skipped.
             Infrastructure errors (KalshiAPIError, etc.) propagate out.
          1. restore_game per record (inside suppress_on_change)
          1.5 seed _volumes_24h from record fields (not populated by restore_game)
          2. adjuster ledger wiring
          3. GSR set_expiration + resolve_batch
          4. feed subscribes
          5. data_collector.log_game_add
          6. persist once

        If steps 3 or 4 raise (resolver / subscribe failure), every pair
        added so far in this call is rolled back: GameManager, adjuster, and
        any open feed subscriptions are reverted, and the exception is
        re-raised so the UI commit path can leave staging intact for retry.
        Without rollback, partial state would persist invisibly and a retry
        would double-add. The rollback path re-raises before returning, so
        ``result.rejected`` is never surfaced on an infrastructure failure —
        callers see either ``CommitResult`` (success, possibly partial
        admission) or an exception (infra failure).
        """
        result = CommitResult()
        pairs: list[ArbPair] = []
        # Round-3 review fix #1: Track which pairs were already present
        # BEFORE this call so we can skip re-wiring them in steps 2-4.
        # Without this, retrying a preserved-staging commit (e.g. add
        # succeeded but a later metadata write failed) would call
        # adjuster.add_event / feed.subscribe / GSR.set_expiration a
        # second time, producing duplicate ticker_map entries, duplicate
        # subscribes, and duplicate _resolve_batch wiring. The retry
        # contract that the round-1 toast advertises ("adds become
        # no-ops on retry") only holds if we genuinely no-op here.
        pre_existing_event_tickers: set[str] = set(
            self._game_manager._games.keys()
        )
        added_to_adjuster: list[ArbPair] = []
        subscribed_tickers: list[str] = []

        # Steps 1 + 1.5: reconstitute + volume seeding, with on_change suppressed
        with self._game_manager.suppress_on_change():
            for r in records:
                # Step 0: admission guard. Fetch fresh Market state for
                # both sides and validate shape invariants (Phase 0: no
                # fractional, no sub-cent tick). MarketAdmissionError is
                # captured per-record into result.rejected; infrastructure
                # errors (KalshiAPIError, timeouts, etc.) propagate out so
                # the caller treats them as a hard commit failure and can
                # preserve staged_changes for retry.
                try:
                    # Parallelize the two per-record REST calls so a batch
                    # of N pairs costs ~N get_market latencies instead of 2N.
                    market_a, market_b = await asyncio.gather(
                        self._rest.get_market(str(r["ticker_a"])),
                        self._rest.get_market(str(r["ticker_b"])),
                    )
                    validate_market_for_admission(market_a, market_b)
                except MarketAdmissionError as exc:
                    logger.warning(
                        "add_pair_admission_rejected",
                        event_ticker=r.get("event_ticker"),
                        reason=str(exc),
                    )
                    result.rejected.append((r, exc))
                    continue

                try:
                    pair = self._game_manager.restore_game(
                        {**r, "source": r.get("source", "tree")},
                    )
                    if pair is None:
                        continue
                    vol_a = r.get("volume_24h_a")
                    vol_b = r.get("volume_24h_b")
                    if vol_a is not None:
                        self._game_manager._volumes_24h[pair.ticker_a] = int(vol_a)
                    if vol_b is not None and pair.ticker_b != pair.ticker_a:
                        self._game_manager._volumes_24h[pair.ticker_b] = int(vol_b)
                    pairs.append(pair)
                except Exception:
                    logger.warning(
                        "tree_add_failed",
                        pair_ticker=r.get("event_ticker"),
                        exc_info=True,
                    )

        # Filter to genuinely-new pairs for downstream wiring. Pre-existing
        # pairs were already wired by the prior successful add — their
        # adjuster ledger, GSR registration, and feed subscribes are
        # already in place. Re-wiring would duplicate side effects.
        new_pairs: list[ArbPair] = [
            p for p in pairs if p.event_ticker not in pre_existing_event_tickers
        ]

        gsr_seeded: list[str] = []
        try:
            # Step 2: adjuster (only for genuinely-new pairs)
            for pair in new_pairs:
                self._adjuster.add_event(pair)
                added_to_adjuster.append(pair)

            # Step 3: GSR wiring + resolve_batch (only for genuinely-new pairs)
            if self._game_status_resolver is not None and new_pairs:
                for pair in new_pairs:
                    self._game_status_resolver.set_expiration(
                        pair.event_ticker,
                        pair.expected_expiration_time,
                    )
                    gsr_seeded.append(pair.event_ticker)
                batch = [
                    (
                        p.event_ticker,
                        self._game_manager.subtitles.get(p.event_ticker, ""),
                    )
                    for p in new_pairs
                ]
                await self._game_status_resolver.resolve_batch(batch)

            # Step 4: feed subscribes (only for genuinely-new pairs)
            for pair in new_pairs:
                await self._feed.subscribe(pair.ticker_a)
                subscribed_tickers.append(pair.ticker_a)
                if pair.ticker_b != pair.ticker_a:
                    await self._feed.subscribe(pair.ticker_b)
                    subscribed_tickers.append(pair.ticker_b)
        except BaseException as exc:
            # BaseException, not Exception — asyncio.CancelledError inherits
            # from BaseException in Py3.12+. Without this, a cancelled commit
            # worker (e.g. user mashes 'c' and exclusive=True kills us) would
            # skip rollback entirely and leave engine state half-mutated.
            cancelled = isinstance(exc, asyncio.CancelledError)
            logger.warning(
                "add_pairs_from_selection_rollback",
                attempted=len(pairs),
                rolled_back_adjuster=len(added_to_adjuster),
                rolled_back_gsr=len(gsr_seeded),
                rolled_back_subscribes=len(subscribed_tickers),
                cancelled=cancelled,
                exc_type=type(exc).__name__,
                exc_msg=str(exc),
            )
            # Shield rollback so a secondary cancellation can't truncate it
            # mid-cleanup. asyncio.shield wraps a single coroutine; we need
            # one task that performs all the awaits.
            try:
                await asyncio.shield(
                    self._rollback_partial_add(
                        # Round-5 review fix #2: rollback must only undo
                        # side effects of the CURRENT call. Pre-existing
                        # pairs (returned by restore_game on duplicate)
                        # were wired by a prior successful add — their
                        # adjuster/GSR/feed state is correct. Removing
                        # them here would erase legitimate engine state
                        # the user did not ask to delete.
                        pairs=new_pairs,
                        added_to_adjuster=added_to_adjuster,
                        gsr_seeded=gsr_seeded,
                        subscribed_tickers=subscribed_tickers,
                    )
                )
            except asyncio.CancelledError:
                # If the shielded rollback itself is cancelled (process
                # shutdown), let the cancellation propagate after we've at
                # least logged what was leaked.
                logger.warning(
                    "add_pairs_rollback_interrupted",
                    pairs=len(new_pairs),
                )
            raise

        # Step 5: data_collector — only on the happy path. log_game_add
        # writes an audit row that has no companion log_game_remove, so on
        # rollback we simply don't emit it (no phantom audit entry).
        # Round-5 review fix #3: iterate new_pairs (not pairs) so a retry
        # over an already-monitored pair does NOT emit a duplicate audit
        # row. The round-3 toast claim ("retry has zero downstream side
        # effects") only holds if step 5 also dedupes.
        if self._data_collector is not None:
            for pair in new_pairs:
                gs = (
                    self._game_status_resolver.get(pair.event_ticker)
                    if self._game_status_resolver
                    else None
                )
                scheduled = gs.scheduled_start.isoformat() if gs and gs.scheduled_start else None
                self._data_collector.log_game_add(
                    event_ticker=pair.event_ticker,
                    series_ticker=pair.series_ticker,
                    sport="",
                    league="",
                    source="tree",
                    ticker_a=pair.ticker_a,
                    ticker_b=pair.ticker_b,
                    volume_a=self._game_manager.volumes_24h.get(pair.ticker_a, 0),
                    volume_b=self._game_manager.volumes_24h.get(pair.ticker_b, 0),
                    fee_type=pair.fee_type,
                    fee_rate=pair.fee_rate,
                    scheduled_start=scheduled,
                )

        # Step 6: persist once. If persistence fails (disk full, antivirus
        # lock on games_full.json, etc.), the in-memory engine state has
        # already been mutated but the on-disk snapshot is stale. A restart
        # at that point would lose engine_state for any newly-added pair —
        # exactly the SURVIVOR-class bug the durability work prevents. So
        # we run the same rollback as a step-3/4 failure: undo every side
        # effect, then re-raise so the UI sees a hard commit failure and
        # preserves staged_changes.
        try:
            self._persist_active_games()
        except Exception as exc:
            logger.warning(
                "add_pairs_persistence_failed_rolling_back",
                attempted=len(new_pairs),
                exc_type=type(exc).__name__,
                exc_msg=str(exc),
            )
            try:
                await asyncio.shield(
                    self._rollback_partial_add(
                        # Round-5 review fix #2 (second site): same
                        # rationale as the step-3/4 rollback above —
                        # pass new_pairs only so a retry-with-
                        # existing-pair persist failure does NOT
                        # tear down the pre-existing pair.
                        pairs=new_pairs,
                        added_to_adjuster=added_to_adjuster,
                        gsr_seeded=gsr_seeded,
                        subscribed_tickers=subscribed_tickers,
                    )
                )
            except asyncio.CancelledError:
                logger.warning(
                    "add_pairs_rollback_interrupted",
                    pairs=len(new_pairs),
                )
            raise
        # admitted = every pair returned by restore_game (including
        # pre-existing no-op duplicates) so the TreeScreen commit UX sees
        # the same "what's in the game manager for this selection" set
        # the prior list[ArbPair] return contract guaranteed.
        result.admitted = pairs
        return result

    async def _rollback_partial_add(
        self,
        *,
        pairs: list[ArbPair],
        added_to_adjuster: list[ArbPair],
        gsr_seeded: list[str],
        subscribed_tickers: list[str],
    ) -> None:
        """Reverse every side effect of add_pairs_from_selection that
        succeeded before failure.

        Each step catches BaseException (not just Exception) because a
        cancellation that lands inside one cleanup step would otherwise
        escape the inner try/except, abort the rest of the cleanup, and
        leak the side effects of all later steps. Catching BaseException
        per step lets us log the cancellation, continue cleaning up, and
        let the outer caller decide whether to re-raise.
        """
        for ticker in subscribed_tickers:
            try:
                await self._feed.unsubscribe(ticker)
            except BaseException as exc:
                logger.warning(
                    "rollback_unsubscribe_failed",
                    ticker=ticker,
                    exc_type=type(exc).__name__,
                    exc_msg=str(exc),
                )
        if self._game_status_resolver is not None:
            for et in gsr_seeded:
                try:
                    self._game_status_resolver.remove(et)
                except BaseException as exc:
                    logger.warning(
                        "rollback_gsr_failed",
                        event_ticker=et,
                        exc_type=type(exc).__name__,
                        exc_msg=str(exc),
                    )
        for pair in added_to_adjuster:
            try:
                self._adjuster.remove_event(pair.event_ticker)
            except BaseException as exc:
                logger.warning(
                    "rollback_adjuster_failed",
                    event_ticker=pair.event_ticker,
                    exc_type=type(exc).__name__,
                    exc_msg=str(exc),
                )
        for pair in pairs:
            try:
                await self._game_manager.remove_game(pair.event_ticker)
            except BaseException as exc:
                logger.warning(
                    "rollback_game_manager_failed",
                    event_ticker=pair.event_ticker,
                    exc_type=type(exc).__name__,
                    exc_msg=str(exc),
                )

    async def remove_pairs_from_selection(
        self,
        pairs: list[tuple[str, str]],
    ) -> list[RemoveOutcome]:
        """Commit path for tree-unticked pairs.

        Input shape (round-7 plan Fix #1): list of (pair_ticker,
        kalshi_event_ticker) tuples. The kalshi_event_ticker is captured
        at staging time so retry-after-persist-failure can populate the
        outcome with the correct event identity even when the pair is
        already gone from game_manager.

        Returns per-pair RemoveOutcome so TreeScreen can decide per-event
        whether to set deliberately_unticked, defer, or retry.

        Per-transition durability: for inventory-bearing pairs that
        transition to winding_down, an immediate persist runs after the
        engine state is mutated. On persist failure, the pair's prior
        engine state is restored (snapshot-and-restore) and a
        RemoveBatchPersistenceError is raised carrying the count of
        successfully-persisted transitions.
        """
        from talos.persistence_errors import (
            PersistenceError,
            RemoveBatchPersistenceError,
        )

        outcomes: list[RemoveOutcome] = []
        successful_winding_count = 0

        with self._game_manager.suppress_on_change():
            for pt, input_kalshi_et in pairs:
                pair = self._game_manager.get_game(pt)
                if pair is None:
                    outcomes.append(
                        RemoveOutcome(
                            pair_ticker=pt,
                            kalshi_event_ticker=input_kalshi_et,
                            status="not_found",
                        )
                    )
                    continue
                kalshi_et = pair.kalshi_event_ticker or pair.event_ticker

                try:
                    ledger = self._adjuster.get_ledger(pt)
                    # Tests use MagicMock ledgers with has_filled_positions /
                    # has_resting_orders; real PositionLedger has neither, so
                    # derive has_inventory from filled_count / resting_count.
                    has_inventory = False
                    if ledger is not None:
                        has_filled = getattr(ledger, "has_filled_positions", None)
                        has_resting = getattr(ledger, "has_resting_orders", None)
                        if callable(has_filled) or callable(has_resting):
                            has_inventory = bool(
                                (callable(has_filled) and has_filled())
                                or (callable(has_resting) and has_resting()),
                            )
                        else:
                            try:
                                has_inventory = (
                                    ledger.filled_count(Side.A) > 0
                                    or ledger.filled_count(Side.B) > 0
                                    or ledger.resting_count(Side.A) > 0
                                    or ledger.resting_count(Side.B) > 0
                                )
                            except Exception:
                                has_inventory = False

                    if has_inventory:
                        # Snapshot prior state for accurate rollback if
                        # the per-transition persist fails. Plan Fix #2
                        # (round-5 v0.1.1 finding #2): pairs can already
                        # be in _exit_only_events via other engine paths
                        # (milestone, sports-game-started, etc.); a
                        # hardcoded rollback would silently strip those
                        # safety conditions. Restore exactly the prior
                        # values, not hardcoded "active".
                        prior_was_winding = pt in self._winding_down
                        prior_was_exit_only = pt in self._exit_only_events
                        prior_engine_state = pair.engine_state

                        self._winding_down.add(pt)
                        await self.enforce_exit_only(pt)
                        self._mark_engine_state(pt, "winding_down")

                        # Per-transition durability: persist immediately
                        # so a crash here doesn't lose the winding_down
                        # state. force_during_suppress=True bypasses the
                        # outer suppress_on_change() block.
                        try:
                            self._persist_active_games(force_during_suppress=True)
                        except PersistenceError as exc:
                            # Restore prior state EXACTLY. Cancelled
                            # orders stay cancelled (irreversible) but
                            # idempotent for retry.
                            if not prior_was_winding:
                                self._winding_down.discard(pt)
                            if not prior_was_exit_only:
                                self._exit_only_events.discard(pt)
                            self._mark_engine_state(pt, prior_engine_state)
                            raise RemoveBatchPersistenceError(
                                persisted_count=successful_winding_count,
                                message=(
                                    f"persistence failed after "
                                    f"{successful_winding_count} winding-down "
                                    f"transitions (current pair: {pt})"
                                ),
                                original=exc,
                                phase="transition",
                            ) from exc
                        successful_winding_count += 1

                        try:
                            fa = ledger.filled_count(Side.A) if ledger else "?"
                            fb = ledger.filled_count(Side.B) if ledger else "?"
                            ra = ledger.resting_count(Side.A) if ledger else "?"
                            rb = ledger.resting_count(Side.B) if ledger else "?"
                        except Exception:
                            fa = fb = ra = rb = "?"
                        reason = f"filled={fa},{fb} resting={ra},{rb}"
                        logger.info(
                            "winding_down_started",
                            pair_ticker=pt,
                            reason=reason,
                        )
                        outcomes.append(
                            RemoveOutcome(
                                pair_ticker=pt,
                                kalshi_event_ticker=kalshi_et,
                                status="winding_down",
                                reason=reason,
                            )
                        )
                        continue

                    # Clean removal (reverse of add flow). Order matters:
                    # do the dangerous async work (game_manager.remove_game,
                    # which awaits feed.unsubscribe) FIRST. If it raises,
                    # the `except Exception` below records "failed" and the
                    # cheap engine-state cleanup never runs — so retry
                    # finds the pair still present on every layer and can
                    # complete the removal cleanly. The earlier ordering
                    # cleared exit_only/stale/GSR/adjuster first, so an
                    # unsubscribe failure left the engine in a half-cleared
                    # state and the retry returned not_found.
                    await self._game_manager.remove_game(pt)
                    self._exit_only_events.discard(pt)
                    self._stale_candidates.discard(pt)
                    if self._game_status_resolver is not None:
                        self._game_status_resolver.remove(pt)
                    self._adjuster.remove_event(pt)
                    outcomes.append(
                        RemoveOutcome(
                            pair_ticker=pt,
                            kalshi_event_ticker=kalshi_et,
                            status="removed",
                        )
                    )
                except RemoveBatchPersistenceError:
                    # Per-transition persist failure already raised; let
                    # it escape the suppress block to the caller.
                    raise
                except Exception as exc:
                    logger.warning(
                        "tree_remove_failed",
                        pair_ticker=pt,
                        exc_info=True,
                    )
                    outcomes.append(
                        RemoveOutcome(
                            pair_ticker=pt,
                            kalshi_event_ticker=kalshi_et,
                            status="failed",
                            reason=str(exc),
                        )
                    )

        # Batch-end persist (covers clean removes; per-transition saves
        # already covered winding_down transitions). Wrap in the same
        # RemoveBatchPersistenceError envelope so failures here also
        # carry the count of successful winding_down transitions for
        # the user-facing toast.
        try:
            self._persist_active_games(force_during_suppress=True)
        except PersistenceError as exc:
            if isinstance(exc, RemoveBatchPersistenceError):
                raise
            raise RemoveBatchPersistenceError(
                persisted_count=successful_winding_count,
                message=(
                    f"per-transition winding-down saves succeeded for "
                    f"{successful_winding_count} pairs; final batch save "
                    f"failed (clean removes in this batch may not be durable "
                    f"and will reappear from the stale snapshot on restart)"
                ),
                original=exc,
                phase="batch_end",
            ) from exc
        return outcomes

    async def _reconcile_winding_down(self) -> None:
        """Remove winding-down pairs whose ledger has cleared.

        For each cleanly-removed pair, check if it was the last one sharing
        its kalshi_event_ticker. If so, emit event_fully_removed to all
        subscribed listeners (TreeScreen uses this to apply deferred [.] flags).
        """
        to_check = list(self._winding_down)
        to_remove: list[str] = []
        for pt in to_check:
            ledger = self._adjuster.get_ledger(pt)
            if ledger is None:
                continue
            # Support both ledger API (real) and MagicMock (tests)
            has_filled = getattr(ledger, "has_filled_positions", None)
            has_resting = getattr(ledger, "has_resting_orders", None)
            if callable(has_filled) and callable(has_resting):
                if has_filled() or has_resting():
                    continue
            else:
                # Real PositionLedger: use count accessors
                if ledger.filled_count(Side.A) or ledger.filled_count(Side.B):
                    continue
                if ledger.resting_count(Side.A) or ledger.resting_count(Side.B):
                    continue
            to_remove.append(pt)

        if not to_remove:
            return

        # Build (pair_ticker, kalshi_event_ticker) tuples for the new
        # remove_pairs_from_selection signature (round-7 plan Fix #1).
        remove_input: list[tuple[str, str]] = []
        for pt in to_remove:
            pair = self._game_manager.get_game(pt)
            kalshi_et = ""
            if pair is not None:
                kalshi_et = pair.kalshi_event_ticker or pair.event_ticker
            remove_input.append((pt, kalshi_et))
        outcomes = await self.remove_pairs_from_selection(remove_input)
        # Round-3 review fix #2: only discard pairs from _winding_down when
        # the outcome is actually terminal for that pair. A `failed`
        # outcome (e.g. unsubscribe raised) leaves the pair still present
        # in GameManager; if we discarded it from _winding_down, the next
        # reconciliation cycle wouldn't retry the removal and any deferred
        # untick would stay stuck pending forever (until restart). Only
        # `removed` (clean removal completed) and `not_found` (pair already
        # gone — idempotency case) are terminal.
        terminal_statuses = {"removed", "not_found"}
        terminal_pts = {
            o.pair_ticker for o in outcomes if o.status in terminal_statuses
        }
        for pt in terminal_pts:
            self._winding_down.discard(pt)

        # Emit event_fully_removed for any kalshi_event_ticker that no longer
        # has any pair in GameManager._games. Pairs we just asked to remove in
        # this pass are excluded from the "still present" check since the real
        # remove_pairs_from_selection has already popped them from _games (and
        # tests mock remove_pairs_from_selection directly, so we treat the
        # pair_tickers we scheduled for removal as already-gone).
        # Round-3 review fix #2 (paired): only count actually-terminal pairs
        # as "just removed". Otherwise a partial-failure batch where event K
        # had P1 (removed) + P2 (failed) would treat both as gone and emit
        # event_fully_removed(K) prematurely, promoting the deferred untick
        # while P2 was still alive in _games and _winding_down.
        just_removed_pts = terminal_pts
        removed_events = {o.kalshi_event_ticker for o in outcomes if o.status == "removed"}
        for kalshi_et in removed_events:
            still_present = any(
                pt not in just_removed_pts
                and (p.kalshi_event_ticker or p.event_ticker) == kalshi_et
                for pt, p in self._game_manager._games.items()
            )
            if not still_present:
                for listener in self._event_fully_removed_listeners:
                    try:
                        listener(kalshi_et)
                    except Exception:
                        logger.warning(
                            "event_fully_removed_listener_failed",
                            exc_info=True,
                        )

    def add_event_fully_removed_listener(self, fn) -> None:
        self._event_fully_removed_listeners.append(fn)

    def _mark_engine_state(self, pair_ticker: str, state: str) -> None:
        """Set per-pair engine_state on the ArbPair in GameManager._games
        so the next _persist_games write picks it up."""
        pair = self._game_manager.get_game(pair_ticker)
        if pair is not None:
            pair.engine_state = state

    def _apply_persisted_engine_state(self, pair: ArbPair) -> None:
        """Apply a pair's persisted engine_state after restore.

        Winding-down pairs re-enter _winding_down + _exit_only_events so the
        next tick immediately applies exit-only behavior — preventing the
        SURVIVOR-class failure mode where a crash during wind-down could
        result in the pair resuming normal trading after restart.
        """
        state = getattr(pair, "engine_state", "active")
        if state == "winding_down":
            self._winding_down.add(pair.event_ticker)
            self._exit_only_events.add(pair.event_ticker)
            logger.info(
                "winding_down_restored",
                pair_ticker=pair.event_ticker,
            )
        elif state == "exit_only":
            self._exit_only_events.add(pair.event_ticker)
            logger.info(
                "exit_only_restored",
                pair_ticker=pair.event_ticker,
            )

    def _persist_active_games(self, *, force_during_suppress: bool = False) -> None:
        """Single persist point for batch add/remove paths.

        Delegates to GameManager.on_change if it's wired to the legacy
        _persist_games writer in __main__.py. That writer serializes all
        pairs in game_manager.active_games to games_full.json.

        force_during_suppress: bypass the suppress_on_change() null-out
        and call the saved (suppressed) callback. Used by the per-
        transition winding_down persist inside remove_pairs_from_selection,
        which runs INSIDE the suppress block but needs durability NOW.

        Failure semantics:
        - PersistenceError always propagates so callers can roll back.
        - Under force_during_suppress=True: ANY callback exception
          (TypeError, AttributeError from a wiring bug, etc.) is
          converted to PersistenceError. The caller demanded durability;
          a silent swallow would break the safety contract.
        - Under force_during_suppress=False: non-PersistenceError
          exceptions are logged but not raised (legacy fire-and-forget
          behavior for non-safety on_change writers).
        """
        from talos.persistence_errors import PersistenceError

        cb = self._game_manager.on_change
        if cb is None and force_during_suppress:
            cb = self._game_manager.suppressed_on_change
        if cb is None:
            if force_during_suppress:
                # Fail-closed: caller demanded durability and there's no
                # writer to deliver it. Indicates a wiring bug.
                raise PersistenceError(
                    "_persist_active_games(force_during_suppress=True) "
                    "called but no on_change writer is wired"
                )
            return
        try:
            cb()
        except PersistenceError:
            raise
        except Exception as exc:
            if force_during_suppress:
                # Fail-closed: any unexpected callback exception under
                # force is converted to PersistenceError to preserve the
                # writer's exit contract ("raises iff persistence-relevant,
                # only PersistenceError type"). Silent swallow would
                # break the durability invariant the same way a swallowed
                # save_games_full failure would.
                raise PersistenceError(
                    f"persistence callback raised {type(exc).__name__}: {exc}"
                ) from exc
            # Non-force path: legacy fire-and-forget behavior preserved.
            logger.warning("persist_active_games_failed", exc_info=True)

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
                    cancel_with_verify=self.cancel_order_with_verify,
                    feed=self._feed,
                    name=self._display_name(envelope.key.event_ticker),
                )
                await self._verify_after_action(envelope.rebalance.event_ticker)
            else:
                self._notify(f"Acknowledged: {envelope.summary} (manual action needed)")
            return

        if envelope.kind == "queue_improve" and envelope.queue_improve is not None:
            await self._execute_queue_improvement(envelope.queue_improve)
            return

        if envelope.kind == "adjustment" and envelope.adjustment is not None:
            try:
                await self._adjuster.execute(envelope.adjustment, self._rest)
                # Invalidate jump-eval cache — resting price changed
                adj_pair = self.find_pair(envelope.adjustment.event_ticker)
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
            except httpx.PoolTimeout:
                # Transient congestion — log quietly, next polling cycle retries.
                logger.debug(
                    "adjustment_pool_timeout",
                    event_ticker=envelope.adjustment.event_ticker,
                )
            except (KalshiAPIError, httpx.HTTPError) as e:
                if (
                    isinstance(e, KalshiAPIError)
                    and e.status_code == 409
                    and "market_closed" in str(e).lower()
                ):
                    evt = envelope.adjustment.event_ticker
                    if evt not in self._exit_only_events:
                        self._exit_only_events.add(evt)
                        self._enforce_exit_only_sync(evt)
                        name = self._display_name(evt)
                        self._notify(
                            f"Exit-only ON: {name} (market closed)",
                            "warning",
                        )
                        logger.info(
                            "exit_only_market_closed",
                            event_ticker=evt,
                        )
                else:
                    self._notify(
                        f"Adjustment FAILED: {type(e).__name__}: {e}",
                        "error",
                        toast=True,
                    )
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

        pair = self.find_pair(event_ticker)
        if pair is None:
            # Rare: pair removed between queue-add and execution. Skip
            # rather than issue a raw cancel that would violate the
            # F36 cancel-discipline guard.
            self._notify(
                f"Withdraw SKIPPED: pair gone for {event_ticker}",
                "warning",
            )
            return
        name = self._display_name(event_ticker)
        cancelled = 0
        for side in (Side.A, Side.B):
            order_id = ledger.resting_order_id(side)
            if order_id is not None:
                try:
                    # F36: route through verify wrapper so F33 resync
                    # runs on 404 instead of blind optimistic-clear.
                    await self.cancel_order_with_verify(order_id, pair)
                    cancelled += 1
                    logger.info(
                        "withdrawal_cancelled",
                        event_ticker=event_ticker,
                        side=side.value,
                        order_id=order_id,
                    )
                except (KalshiAPIError, KalshiRateLimitError, httpx.HTTPError) as e:
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

    async def _execute_queue_improvement(self, qi: ProposedQueueImprovement) -> None:
        """Execute a queue improvement: amend resting order to improved price.

        Re-checks safety gates with fresh data before executing (P7).
        """
        name = self._display_name(qi.event_ticker)
        pair = self.find_pair(qi.event_ticker)
        if pair is None:
            self._notify(f"Queue improve FAILED: no pair for {qi.event_ticker}", "error")
            return

        try:
            ledger = self._adjuster.get_ledger(qi.event_ticker)
        except KeyError:
            self._notify(f"Queue improve FAILED: no ledger for {qi.event_ticker}", "error")
            return

        side = Side(qi.side)

        # Re-check: order still resting
        current_oid = ledger.resting_order_id(side)
        if current_oid != qi.order_id:
            self._notify(f"Queue improve skipped: order changed for {name}", "warning")
            return

        # Re-check: profitability with fresh data
        other_avg = ledger.open_avg_filled_price(side.other)
        if other_avg <= 0:
            self._notify(f"Queue improve skipped: no fills on other side for {name}", "warning")
            return

        eff_improved_bps = fee_adjusted_cost_bps(
            qi.improved_price * ONE_CENT_BPS, rate=pair.fee_rate
        )
        eff_other_bps = fee_adjusted_cost_bps(
            int(round(other_avg)) * ONE_CENT_BPS, rate=pair.fee_rate
        )
        if eff_improved_bps + eff_other_bps >= ONE_DOLLAR_BPS:
            self._notify(
                f"Queue improve BLOCKED: {name} {qi.improved_price}c unprofitable",
                "warning",
            )
            return

        # Re-check: no spread crossing
        ask_level = self._feed.book_manager.best_ask(qi.ticker, side=qi.kalshi_side)
        if (
            ask_level is not None
            and qi.improved_price >= ask_level.price_bps // ONE_CENT_BPS
        ):
            self._notify(
                f"Queue improve BLOCKED: {name} {qi.improved_price}c would cross spread",
                "warning",
            )
            return

        # Section 8 startup gate — amend is a risk-increasing op (price
        # or quantity change); block until ledger confirmed.
        if not await self._wait_for_ledger_ready(pair, "queue_improve"):
            return

        # Execute amend
        try:
            resting_count = ledger.resting_count(side)
            total_count = ledger.filled_count(side) + resting_count

            amend_kwargs: dict[str, object] = {
                "ticker": qi.ticker,
                "side": qi.kalshi_side,
                "action": "buy",
                "count": total_count,
            }
            if qi.kalshi_side == "yes":
                amend_kwargs["yes_price"] = qi.improved_price
            else:
                amend_kwargs["no_price"] = qi.improved_price

            old_order, amended_order = await self._rest.amend_order(
                qi.order_id,
                **amend_kwargs,  # type: ignore[arg-type]
            )

            # Update fills from amend response (exact-precision via bps/fp100
            # siblings of the ledger accessors + Order fields).
            fill_delta_fp100 = (
                old_order.fill_count_fp100 - ledger.filled_count_fp100(side)
            )
            if fill_delta_fp100 > 0:
                old_price_bps = (
                    old_order.no_price_bps if old_order.side == "no" else old_order.yes_price_bps
                )
                fee_delta_bps = (
                    _order_maker_fees_bps(old_order) - ledger.filled_fees_bps(side)
                )
                ledger.record_fill_bps(
                    side,
                    count_fp100=fill_delta_fp100,
                    price_bps=old_price_bps,
                    fees_bps=max(0, fee_delta_bps),
                )

            # Update ledger with new resting state (exact-precision sibling).
            amended_price_bps = (
                amended_order.no_price_bps
                if amended_order.side == "no"
                else amended_order.yes_price_bps
            )
            ledger.record_resting_bps(
                side,
                order_id=amended_order.order_id,
                count_fp100=amended_order.remaining_count_fp100,
                price_bps=amended_price_bps,
            )

            self._notify(
                f"Queue improved: {name} {qi.side} {qi.current_price}c → {qi.improved_price}c",
            )
            logger.info(
                "queue_improvement_executed",
                event_ticker=qi.event_ticker,
                side=qi.side,
                old_price=qi.current_price,
                new_price=qi.improved_price,
            )
        except KalshiRateLimitError:
            raise
        except (KalshiAPIError, httpx.HTTPError) as e:
            if (
                isinstance(e, KalshiAPIError)
                and e.status_code == 409
                and "market_closed" in str(e).lower()
            ):
                evt = qi.event_ticker
                if evt not in self._exit_only_events:
                    self._exit_only_events.add(evt)
                    self._enforce_exit_only_sync(evt)
                    self._notify(f"Exit-only ON: {name} (market closed)", "warning")
            else:
                self._notify(
                    f"Queue improve FAILED: {type(e).__name__}: {e}",
                    "error",
                    toast=True,
                )
                logger.exception(
                    "queue_improvement_error",
                    event_ticker=qi.event_ticker,
                    side=qi.side,
                )

        await self._verify_after_action(qi.event_ticker)

    async def _verify_after_action(self, event_ticker: str) -> None:
        """Re-sync from Kalshi after any order action to verify outcome."""
        pair = self.find_pair(event_ticker)
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
                    Side.A: abs(pos_a.position_fp100 // ONE_CONTRACT_FP100) if pos_a else 0,
                    Side.B: abs(pos_b.position_fp100 // ONE_CONTRACT_FP100) if pos_b else 0,
                }
                costs = {
                    Side.A: pos_a.total_traded_bps // ONE_CENT_BPS if pos_a else 0,
                    Side.B: pos_b.total_traded_bps // ONE_CENT_BPS if pos_b else 0,
                }
                fees = {
                    Side.A: pos_a.fees_paid_bps // ONE_CENT_BPS if pos_a else 0,
                    Side.B: pos_b.fees_paid_bps // ONE_CENT_BPS if pos_b else 0,
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
        except httpx.PoolTimeout:
            # Transient congestion — log only, polling cycle catches up.
            logger.debug(
                "post_action_verify_pool_timeout",
                event_ticker=event_ticker,
            )
        except (KalshiAPIError, httpx.HTTPError) as e:
            # Action already succeeded — warn but don't panic on normal
            # API turbulence. Unknown exceptions escape (real bugs).
            logger.warning(
                "post_action_verify_failed",
                event_ticker=event_ticker,
                exc_info=True,
            )
            name = self._display_name(event_ticker)
            self._notify(
                f"Verify FAILED for {name} ({type(e).__name__}) — position data may be stale",
                "warning",
            )

    # ── Section 8 startup safety gate ────────────────────────────────

    async def _wait_for_ledger_ready(
        self, pair: ArbPair, op_name: str
    ) -> bool:
        """Block risk-increasing ops until the ledger is confirmed.

        Returns True if the ledger clears within
        :data:`STARTUP_SYNC_TIMEOUT_S`. Returns False (with operator
        notification) if timeout exceeded or the flag requires operator
        action (``legacy_migration_pending``,
        ``reconcile_mismatch_pending``).

        Caller pattern::

            if not await self._wait_for_ledger_ready(pair, "create_order"):
                return None

        ``cancel_order`` is NOT gated — use
        :meth:`cancel_order_with_verify` instead (F31 carve-out).
        """
        try:
            ledger = self._adjuster.get_ledger(pair.event_ticker)
        except KeyError:
            # No ledger yet — fresh pair with no position. Blocking makes
            # no sense; safety gates downstream handle placement rules.
            return True

        deadline = time.monotonic() + STARTUP_SYNC_TIMEOUT_S
        reconcile_attempted = False
        while not ledger.ready():
            if (
                ledger.legacy_migration_pending
                or ledger.reconcile_mismatch_pending
            ):
                self._notify(
                    f"Confirm or reconcile {pair.event_ticker} before {op_name}",
                    "error",
                )
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._notify(
                    f"Confirmation pending for {pair.event_ticker} "
                    f"— {op_name} blocked",
                    "error",
                )
                return False
            # Auto-reconcile trigger: if stale_fills_unconfirmed is still
            # set after AUTO_RECONCILE_DELAY_S, call reconcile_from_fills
            # to attempt an authoritative rebuild from per-fill data.
            elapsed = STARTUP_SYNC_TIMEOUT_S - remaining
            if (
                ledger.stale_fills_unconfirmed
                and not reconcile_attempted
                and elapsed >= AUTO_RECONCILE_DELAY_S
            ):
                reconcile_attempted = True
                try:
                    await ledger.reconcile_from_fills(
                        self._rest, self._persist_games_now
                    )
                except Exception:
                    logger.exception(
                        "auto_reconcile_failed",
                        event_ticker=pair.event_ticker,
                    )
                    # Fall through — next loop iteration re-checks flags.
            await asyncio.sleep(min(0.2, remaining))
        return True

    def _persist_games_now(
        self,
        proposed: Any | None,
        event_ticker: str | None,
    ) -> None:
        """Synchronous persist callback for reconcile and accept paths.

        Iterates active pairs; for the pair matching ``event_ticker``,
        substitutes ``proposed`` (a :class:`LedgerSnapshot`) for the
        live ``ledger.to_save_dict()`` output. For all other pairs, uses
        their current live ``to_save_dict()``. Writes ``games_full.json``
        via atomic temp+rename.

        **MUST remain synchronous** — the v11 atomicity contract rests
        on this: the ledger's mutation phase is a single sync block and
        this persist runs inside that block, with no ``await`` or lock.
        """
        from talos.persistence import (
            save_games,
            save_games_full,
            snapshot_to_save_dict,
        )
        from talos.persistence_errors import PersistenceError

        try:
            save_games(
                [p.event_ticker for p in self._game_manager.active_games]
            )
            games_data: list[dict[str, object]] = []
            for p in self._game_manager.active_games:
                entry: dict[str, object] = {
                    "event_ticker": p.event_ticker,
                    "ticker_a": p.ticker_a,
                    "ticker_b": p.ticker_b,
                    "fee_type": p.fee_type,
                    "fee_rate": p.fee_rate,
                    "close_time": p.close_time,
                    "expected_expiration_time": p.expected_expiration_time,
                    "label": self._game_manager.labels.get(p.event_ticker, ""),
                    "sub_title": self._game_manager.subtitles.get(
                        p.event_ticker, ""
                    ),
                    "side_a": p.side_a,
                    "side_b": p.side_b,
                    "kalshi_event_ticker": p.kalshi_event_ticker,
                    "series_ticker": p.series_ticker,
                    "talos_id": p.talos_id,
                }
                if p.source is not None:
                    entry["source"] = p.source
                entry["engine_state"] = p.engine_state
                vol_a = self._game_manager.volumes_24h.get(p.ticker_a)
                vol_b = self._game_manager.volumes_24h.get(p.ticker_b)
                if vol_a is not None:
                    entry["volume_a"] = vol_a
                if vol_b is not None:
                    entry["volume_b"] = vol_b
                # Substitute proposed snapshot for the target event.
                if (
                    proposed is not None
                    and event_ticker is not None
                    and p.event_ticker == event_ticker
                ):
                    entry["ledger"] = snapshot_to_save_dict(proposed)
                else:
                    try:
                        ledger = self._adjuster.get_ledger(p.event_ticker)
                        entry["ledger"] = ledger.to_save_dict()
                    except KeyError:
                        pass
                games_data.append(entry)
            ok = save_games_full(games_data)
            if not ok:
                raise PersistenceError(
                    "save_games_full() returned failure"
                )
        except PersistenceError:
            raise
        except Exception as exc:
            raise PersistenceError(
                f"persistence-path failure during _persist_games_now: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    async def cancel_order_with_verify(
        self, order_id: str, pair: ArbPair
    ) -> None:
        """Fail-safe cancel. Always allowed regardless of ``ledger.ready()``.

        F33: a 404 on a single ``order_id`` does NOT prove the side has
        zero resting exposure. :class:`PositionLedger` stores only the
        *first* resting order_id per side, but Kalshi supports multiple
        live orders on a side. A stale first ID disappearing could mean
        either one-gone-others-exist or all-gone. We resync via
        :meth:`PositionLedger.sync_from_orders` to get ground truth
        rather than blind-clearing.
        """
        # Phase 1: probe the order.
        live: Order | None = None
        try:
            live = await self._rest.get_order(order_id)
        except KalshiNotFoundError:
            # Tracked ID is gone. Could mean fully cancelled, fully
            # filled, or simply evicted from Kalshi's order store.
            # Resync gives ground truth; do NOT attempt cancel.
            await self._resync_pair_orders(pair)
            return
        except (KalshiAPIError, httpx.HTTPError):
            # Network / non-404 error on the probe. Fall through and
            # still attempt the cancel — we'd rather cancel blindly
            # than skip. Resync after either outcome.
            live = None

        if live is not None and live.status not in ("resting", "executed"):
            # Terminal state already (cancelled, closed, etc.). No cancel
            # to issue; resync is sufficient to align ledger to truth.
            await self._resync_pair_orders(pair)
            return

        # Phase 2: issue the cancel.
        try:
            await self._rest.cancel_order(order_id)
        except KalshiNotFoundError:
            # Race: order disappeared between probe and cancel.
            # Resync instead of optimistic-clear.
            await self._resync_pair_orders(pair)
            return
        except KalshiRateLimitError:
            raise
        except (KalshiAPIError, httpx.HTTPError) as e:
            # Best-effort resync so ledger tracks whatever state did land.
            # Don't swallow the error — caller may want to surface it.
            logger.warning(
                "cancel_order_api_error",
                order_id=order_id,
                event_ticker=pair.event_ticker,
                exc_type=type(e).__name__,
            )
            with contextlib.suppress(Exception):
                await self._resync_pair_orders(pair)
            raise

        # Successful cancel — resync rather than optimistically update.
        await self._resync_pair_orders(pair)

    async def _resync_pair_orders(self, pair: ArbPair) -> None:
        """Fetch the pair's active orders and reconcile ledger resting state.

        Calls :meth:`PositionLedger.sync_from_orders` with the union of
        resting orders for both sides. For same-ticker pairs, only one
        GET is issued (YES/NO on the same market share a ticker).
        """
        try:
            ledger = self._adjuster.get_ledger(pair.event_ticker)
        except KeyError:
            return
        try:
            orders_a = await self._rest.get_orders(
                ticker=pair.ticker_a, status="resting"
            )
            orders_b = (
                []
                if pair.is_same_ticker
                else await self._rest.get_orders(
                    ticker=pair.ticker_b, status="resting"
                )
            )
        except (KalshiAPIError, httpx.HTTPError):
            logger.warning(
                "resync_pair_orders_fetch_failed",
                event_ticker=pair.event_ticker,
                exc_info=True,
            )
            return
        orders = list(orders_a) + list(orders_b)
        ledger.sync_from_orders(
            orders, ticker_a=pair.ticker_a, ticker_b=pair.ticker_b
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

        # Balanced — no resting orders and either balanced fills or markets closed
        if resting_a == 0 and resting_b == 0 and (filled_a > 0 or filled_b > 0):
            if filled_a == filled_b:
                return "Balanced"
            # Unbalanced fills, no resting, markets settled → done.
            # Use the explicit settlement signal from the lifecycle WS feed,
            # not empty orderbooks (stale WS / low liquidity look the same).
            settled = self._settled_markets.get(event_ticker, {})
            if len(settled) >= 2:
                return "Settled"

        # Jumped — resting orders not at top of market
        if resting_a > 0 or resting_b > 0:
            pair = self.find_pair(event_ticker)
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

    def find_pair(self, event_ticker: str) -> ArbPair | None:
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

    def recompute_positions(self) -> None:
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
                summary.kalshi_pnl_bps = ep.realized_pnl_bps

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
        started = time.monotonic()
        stale = self._feed.book_manager.stale_tickers()
        if not stale:
            return
        active = {t for p in self._scanner.pairs for t in (p.ticker_a, p.ticker_b)}
        active_stale = [ticker for ticker in stale if ticker in active]
        active_stale_set = set(active_stale)
        self._stale_recovery_retry_after = {
            ticker: retry_after
            for ticker, retry_after in self._stale_recovery_retry_after.items()
            if ticker in active_stale_set
        }
        now = time.monotonic()
        attempted = 0
        skipped_cooldown = 0
        recovered = 0
        failed = 0
        for ticker in stale:
            if ticker not in active:
                continue
            retry_after = self._stale_recovery_retry_after.get(ticker, 0.0)
            if retry_after > now:
                skipped_cooldown += 1
                continue
            attempted += 1
            try:
                await self._feed.unsubscribe(ticker)
                await self._feed.subscribe(ticker)
                recovered += 1
                self._stale_recovery_retry_after[ticker] = (
                    time.monotonic() + _STALE_BOOK_RECOVERY_COOLDOWN_S
                )
                logger.info("stale_book_recovered", ticker=ticker)
            except Exception:
                failed += 1
                self._stale_recovery_retry_after[ticker] = (
                    time.monotonic() + _STALE_BOOK_RECOVERY_COOLDOWN_S
                )
                logger.warning("stale_book_recovery_failed", ticker=ticker, exc_info=True)
        logger.info(
            "stale_book_recovery_cycle",
            stale_count=len(stale),
            active_stale_count=len(active_stale),
            attempted_count=attempted,
            skipped_cooldown_count=skipped_cooldown,
            recovered_count=recovered,
            failed_count=failed,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )

    def _active_market_tickers(self, *, resting_only: bool = False) -> list[str]:
        """Collect market tickers from cached orders.

        When ``resting_only=True``, returns only tickers with resting orders
        (for queue position polling — filled orders have no queue position).
        Otherwise returns all tickers (for CPM trade tracking).
        """
        if resting_only:
            tickers = {o.ticker for o in self._orders_cache if o.status == "resting"}
        else:
            tickers = {o.ticker for o in self._orders_cache}
        return list(tickers)

    def _notify(self, message: str, severity: str = "information", *, toast: bool = False) -> None:
        """Emit a notification to the UI if callback is set.

        By default, notifications go to the ActivityLog panel (zero asyncio
        overhead). Pass ``toast=True`` for critical errors or user-initiated
        action results that need an interruptive Textual toast.
        """
        if self.on_notification:
            self.on_notification(message, severity, toast)
