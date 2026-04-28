"""Tests for queue-aware price improvement detection and execution."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.bid_adjuster import BidAdjuster
from talos.cpm import FlowKey
from talos.engine import TradingEngine
from talos.game_manager import GameManager
from talos.game_status import GameStatus, GameStatusResolver
from talos.market_feed import MarketFeed
from talos.models.market import OrderBookLevel
from talos.models.order import Order
from talos.models.proposal import ProposalKey, ProposedQueueImprovement
from talos.models.strategy import ArbPair
from talos.orderbook import OrderBookManager
from talos.position_ledger import Side
from talos.rest_client import KalshiRESTClient
from talos.scanner import ArbitrageScanner
from talos.top_of_market import TopOfMarketTracker
from talos.units import ONE_CONTRACT_FP100


def _seed_cpm(
    engine: TradingEngine,
    ticker: str,
    cpm_value: float,
    *,
    outcome: str = "no",
    book_side: str = "BID",
    price_bps: int = 4100,
) -> None:
    """Seed the CPM tracker with synthetic trade events producing the given CPM.

    Defaults match how engine.check_queue_stress queries the behind side:
    outcome="no", book_side="BID", price_bps=4100 (41¢ resting price the
    tests use). Storage is FlowKey-keyed and units are count_fp100 — both
    follow the post-bps-migration / post-granularity-refactor cpm.py.
    """
    now = time.time()
    # Spread events over 5 minutes (300s) — CPM tracker uses 300s window.
    # Total contracts in window = cpm_value * 5; convert to fp100 and split
    # across 6 buckets.
    total_fp100 = int(round(cpm_value * 5 * ONE_CONTRACT_FP100))
    per_event_fp100 = total_fp100 // 6
    events: list[tuple[float, int]] = []
    for i in range(6):
        events.append((now - 300 + i * 50, per_event_fp100))
    key = FlowKey(ticker=ticker, outcome=outcome, book_side=book_side, price_bps=price_bps)
    engine._cpm._events[key] = events


def _seed_cpm_zero(
    engine: TradingEngine,
    ticker: str,
    *,
    outcome: str = "no",
    book_side: str = "BID",
    price_bps: int = 4100,
) -> None:
    """Seed the CPM tracker with zero-volume events (dead market)."""
    now = time.time()
    events: list[tuple[float, int]] = []
    for i in range(6):
        events.append((now - 300 + i * 50, 0))
    key = FlowKey(ticker=ticker, outcome=outcome, book_side=book_side, price_bps=price_bps)
    engine._cpm._events[key] = events


def _make_pair(
    event_ticker: str = "EVT-1",
    ticker_a: str = "TK-A",
    ticker_b: str = "TK-B",
    talos_id: int = 35,
    close_time: str | None = None,
) -> ArbPair:
    return ArbPair(
        talos_id=talos_id,
        event_ticker=event_ticker,
        ticker_a=ticker_a,
        ticker_b=ticker_b,
        close_time=close_time,
    )


def _make_engine(
    pair: ArbPair | None = None,
    game_status: GameStatus | None = None,
    best_ask_price: int | None = None,
) -> TradingEngine:
    """Build a TradingEngine with mock dependencies wired for queue stress testing."""
    pair = pair or _make_pair()
    books = OrderBookManager()
    scanner = ArbitrageScanner(books)
    scanner.add_pair(
        pair.event_ticker,
        pair.ticker_a,
        pair.ticker_b,
        talos_id=pair.talos_id,
        close_time=pair.close_time,
    )

    adjuster = BidAdjuster(books, scanner.pairs, unit_size=5)

    # Set up game status resolver
    resolver = MagicMock(spec=GameStatusResolver)
    if game_status is not None:
        resolver.get.return_value = game_status
    else:
        resolver.get.return_value = None

    feed = MagicMock(spec=MarketFeed)

    # Set up best_ask mock
    book_mgr_mock = MagicMock(spec=OrderBookManager)
    if best_ask_price is not None:
        book_mgr_mock.best_ask.return_value = OrderBookLevel(
            price_bps=best_ask_price * 100, quantity_fp100=10_000
        )
    else:
        book_mgr_mock.best_ask.return_value = None
    feed.book_manager = book_mgr_mock

    engine = TradingEngine(
        scanner=scanner,
        game_manager=MagicMock(spec=GameManager, on_change=None),
        rest_client=AsyncMock(spec=KalshiRESTClient),
        market_feed=feed,
        tracker=TopOfMarketTracker(books),
        adjuster=adjuster,
        game_status_resolver=resolver,
    )

    # Pre-build caches that normally get set in _recompute_positions
    engine._pair_index = {p.event_ticker: p for p in scanner.pairs}
    engine._pending_kinds_cache = {}

    return engine


class TestCheckQueueStressDetection:
    """Test the detection logic in check_queue_stress()."""

    def test_no_game_status_resolver_skips(self):
        """No resolver → no proposals."""
        books = OrderBookManager()
        scanner = ArbitrageScanner(books)
        pair = _make_pair()
        scanner.add_pair(pair.event_ticker, pair.ticker_a, pair.ticker_b)
        adjuster = BidAdjuster(books, scanner.pairs, unit_size=5)
        engine = TradingEngine(
            scanner=scanner,
            game_manager=MagicMock(spec=GameManager, on_change=None),
            rest_client=AsyncMock(spec=KalshiRESTClient),
            market_feed=MagicMock(spec=MarketFeed),
            tracker=TopOfMarketTracker(books),
            adjuster=adjuster,
            game_status_resolver=None,
        )
        engine._pending_kinds_cache = {}
        engine.check_queue_stress()
        assert len(engine._proposal_queue) == 0

    def test_no_fills_skips(self):
        """Pairs with zero fills on both sides are skipped."""
        game_time = datetime.now(UTC) + timedelta(hours=18)
        gs = GameStatus(state="pre", scheduled_start=game_time)
        engine = _make_engine(game_status=gs)
        engine.check_queue_stress()
        assert len(engine._proposal_queue) == 0

    def test_equal_fills_skips(self):
        """Both sides equally filled → no behind side → skip."""
        game_time = datetime.now(UTC) + timedelta(hours=18)
        gs = GameStatus(state="pre", scheduled_start=game_time)
        engine = _make_engine(game_status=gs)

        pair = engine._scanner.pairs[0]
        ledger = engine._adjuster.get_ledger(pair.event_ticker)
        ledger.record_fill(Side.A, count=5, price=57)
        ledger.record_fill(Side.B, count=5, price=41)

        engine.check_queue_stress()
        assert len(engine._proposal_queue) == 0

    def test_behind_side_no_resting_skips(self):
        """Behind side has fills but no resting order → skip."""
        game_time = datetime.now(UTC) + timedelta(hours=18)
        gs = GameStatus(state="pre", scheduled_start=game_time)
        engine = _make_engine(game_status=gs)

        pair = engine._scanner.pairs[0]
        ledger = engine._adjuster.get_ledger(pair.event_ticker)
        ledger.record_fill(Side.A, count=5, price=57)
        ledger.record_fill(Side.B, count=2, price=41)

        engine.check_queue_stress()
        assert len(engine._proposal_queue) == 0

    def test_proposal_generated_when_eta_exceeds_time_remaining(self):
        """Core scenario: behind side stuck, ETA > game time → proposal."""
        game_time = datetime.now(UTC) + timedelta(hours=18)
        gs = GameStatus(state="pre", scheduled_start=game_time)
        engine = _make_engine(game_status=gs, best_ask_price=50)

        pair = engine._scanner.pairs[0]
        ledger = engine._adjuster.get_ledger(pair.event_ticker)

        # A side: fully filled
        ledger.record_fill(Side.A, count=5, price=57)
        # B side: 0 filled, resting at 41c with huge queue
        ledger.record_resting(Side.B, order_id="order-b-1", count=5, price=41)

        engine._queue_cache["order-b-1"] = 186_000

        # CPM: ~100 contracts/min → ETA = 186000/100 = 1860 min = 31h > 18h
        _seed_cpm(engine, pair.ticker_b, 100.0)

        engine.check_queue_stress()

        assert len(engine._proposal_queue) == 1
        proposal = engine._proposal_queue.pending()[0]
        assert proposal.kind == "queue_improve"
        assert proposal.queue_improve is not None
        assert proposal.queue_improve.current_price == 41
        assert proposal.queue_improve.improved_price == 42
        assert proposal.queue_improve.side == "B"
        assert proposal.queue_improve.order_id == "order-b-1"

    def test_eta_below_time_remaining_no_proposal(self):
        """ETA < time remaining → no proposal needed."""
        game_time = datetime.now(UTC) + timedelta(hours=48)
        gs = GameStatus(state="pre", scheduled_start=game_time)
        engine = _make_engine(game_status=gs, best_ask_price=50)

        pair = engine._scanner.pairs[0]
        ledger = engine._adjuster.get_ledger(pair.event_ticker)
        ledger.record_fill(Side.A, count=5, price=57)
        ledger.record_resting(Side.B, order_id="order-b-1", count=5, price=41)
        engine._queue_cache["order-b-1"] = 1000  # Small queue

        _seed_cpm(engine, pair.ticker_b, 100.0)

        engine.check_queue_stress()
        assert len(engine._proposal_queue) == 0

    def test_exit_only_event_skips(self):
        """Events in exit-only mode are skipped."""
        game_time = datetime.now(UTC) + timedelta(hours=18)
        gs = GameStatus(state="pre", scheduled_start=game_time)
        engine = _make_engine(game_status=gs)

        pair = engine._scanner.pairs[0]
        engine._exit_only_events.add(pair.event_ticker)

        engine.check_queue_stress()
        assert len(engine._proposal_queue) == 0

    def test_existing_proposal_blocks_new(self):
        """If any proposal already exists for the event, skip."""
        game_time = datetime.now(UTC) + timedelta(hours=18)
        gs = GameStatus(state="pre", scheduled_start=game_time)
        engine = _make_engine(game_status=gs)

        pair = engine._scanner.pairs[0]
        engine._pending_kinds_cache[pair.event_ticker] = {"bid"}

        engine.check_queue_stress()
        assert len(engine._proposal_queue) == 0


class TestSafetyGates:
    """Test safety gates that prevent unprofitable or spread-crossing improvements."""

    def test_unprofitable_improvement_blocked(self):
        """Improvement price that makes arb unprofitable is blocked."""
        game_time = datetime.now(UTC) + timedelta(hours=18)
        gs = GameStatus(state="pre", scheduled_start=game_time)
        engine = _make_engine(game_status=gs, best_ask_price=60)

        pair = engine._scanner.pairs[0]
        ledger = engine._adjuster.get_ledger(pair.event_ticker)
        # A at 55c, B resting at 44c → 45c would be unprofitable
        # fee_adjusted_cost(45) ≈ 45.43, fee_adjusted_cost(55) ≈ 55.43
        # Total ≈ 100.86 >= 100 → blocked
        ledger._sides[Side.A].filled_count_fp100 = 5 * 100
        ledger._sides[Side.A].filled_total_cost_bps = 55 * 5 * 100
        ledger.record_resting(Side.B, order_id="order-b-1", count=5, price=44)

        engine._queue_cache["order-b-1"] = 186_000
        _seed_cpm(engine, pair.ticker_b, 100.0)

        engine.check_queue_stress()
        assert len(engine._proposal_queue) == 0

    def test_spread_crossing_blocked(self):
        """Improvement that would cross the spread is blocked."""
        game_time = datetime.now(UTC) + timedelta(hours=18)
        gs = GameStatus(state="pre", scheduled_start=game_time)
        engine = _make_engine(game_status=gs, best_ask_price=42)

        pair = engine._scanner.pairs[0]
        ledger = engine._adjuster.get_ledger(pair.event_ticker)
        ledger.record_fill(Side.A, count=5, price=57)
        ledger.record_resting(Side.B, order_id="order-b-1", count=5, price=41)
        engine._queue_cache["order-b-1"] = 186_000
        _seed_cpm(engine, pair.ticker_b, 100.0)

        engine.check_queue_stress()
        assert len(engine._proposal_queue) == 0

    def test_improvement_below_ask_allowed(self):
        """Improvement price below best ask is allowed."""
        game_time = datetime.now(UTC) + timedelta(hours=18)
        gs = GameStatus(state="pre", scheduled_start=game_time)
        engine = _make_engine(game_status=gs, best_ask_price=45)

        pair = engine._scanner.pairs[0]
        ledger = engine._adjuster.get_ledger(pair.event_ticker)
        ledger.record_fill(Side.A, count=5, price=57)
        ledger.record_resting(Side.B, order_id="order-b-1", count=5, price=41)
        engine._queue_cache["order-b-1"] = 186_000
        _seed_cpm(engine, pair.ticker_b, 100.0)

        engine.check_queue_stress()
        assert len(engine._proposal_queue) == 1


class TestCloseTimeFallback:
    """Test that close_time is used as fallback when no game status."""

    def test_close_time_used_when_no_game_status(self):
        """When GameStatusResolver returns None, fall back to close_time."""
        close_time = (datetime.now(UTC) + timedelta(hours=18)).isoformat()
        pair = _make_pair(close_time=close_time)

        engine = _make_engine(pair=pair, game_status=None, best_ask_price=50)

        ledger = engine._adjuster.get_ledger(pair.event_ticker)
        ledger.record_fill(Side.A, count=5, price=57)
        ledger.record_resting(Side.B, order_id="order-b-1", count=5, price=41)
        engine._queue_cache["order-b-1"] = 186_000
        _seed_cpm(engine, pair.ticker_b, 100.0)

        engine.check_queue_stress()
        assert len(engine._proposal_queue) == 1


class TestQueueImprovementExecution:
    """Test the _execute_queue_improvement method."""

    @pytest.mark.asyncio
    async def test_successful_amend(self):
        """Successful amend updates ledger and notifies."""
        game_time = datetime.now(UTC) + timedelta(hours=18)
        gs = GameStatus(state="pre", scheduled_start=game_time)
        engine = _make_engine(game_status=gs, best_ask_price=50)

        pair = engine._scanner.pairs[0]
        ledger = engine._adjuster.get_ledger(pair.event_ticker)
        ledger.record_fill(Side.A, count=5, price=57)
        ledger.record_resting(Side.B, order_id="order-b-1", count=5, price=41)
        # Task 6b-2 Section 8 startup gate: amend is risk-increasing and
        # blocks until ledger.ready() is True.
        ledger._first_orders_sync.set()

        qi = ProposedQueueImprovement(
            event_ticker=pair.event_ticker,
            side="B",
            order_id="order-b-1",
            ticker=pair.ticker_b,
            current_price=41,
            improved_price=42,
            current_queue=186_000,
            eta_minutes=1860,
            time_remaining_minutes=1080,
            other_side_avg=57.0,
            kalshi_side="no",
        )

        old_order = MagicMock(spec=Order)
        old_order.side = "no"
        old_order.fill_count_fp100 = 0
        old_order.maker_fees_bps = 0
        old_order.no_price_bps = 4100
        amended_order = MagicMock(spec=Order)
        amended_order.order_id = "order-b-2"
        amended_order.side = "no"
        amended_order.remaining_count_fp100 = 500
        amended_order.no_price_bps = 4200
        engine._rest.amend_order = AsyncMock(return_value=(old_order, amended_order))

        notifications: list[str] = []
        engine.on_notification = lambda msg, sev="information", toast=False: notifications.append(
            msg
        )
        engine._verify_after_action = AsyncMock()

        await engine._execute_queue_improvement(qi)

        engine._rest.amend_order.assert_called_once()
        assert ledger.resting_order_id(Side.B) == "order-b-2"
        assert ledger.resting_price(Side.B) == 42
        assert any("Queue improved" in n for n in notifications)

    @pytest.mark.asyncio
    async def test_order_changed_skips(self):
        """If the resting order changed between proposal and execution, skip."""
        game_time = datetime.now(UTC) + timedelta(hours=18)
        gs = GameStatus(state="pre", scheduled_start=game_time)
        engine = _make_engine(game_status=gs, best_ask_price=50)

        pair = engine._scanner.pairs[0]
        ledger = engine._adjuster.get_ledger(pair.event_ticker)
        ledger.record_fill(Side.A, count=5, price=57)
        ledger.record_resting(Side.B, order_id="order-b-DIFFERENT", count=5, price=41)

        qi = ProposedQueueImprovement(
            event_ticker=pair.event_ticker,
            side="B",
            order_id="order-b-1",
            ticker=pair.ticker_b,
            current_price=41,
            improved_price=42,
            current_queue=186_000,
            eta_minutes=1860,
            time_remaining_minutes=1080,
            other_side_avg=57.0,
            kalshi_side="no",
        )

        notifications: list[str] = []
        engine.on_notification = lambda msg, sev="information", toast=False: notifications.append(
            msg
        )
        engine._verify_after_action = AsyncMock()

        await engine._execute_queue_improvement(qi)

        engine._rest.amend_order.assert_not_called()  # pyright: ignore[reportAttributeAccessIssue]
        assert any("order changed" in n for n in notifications)

    @pytest.mark.asyncio
    async def test_profitability_recheck_blocks(self):
        """Re-check at execution time blocks if price became unprofitable."""
        game_time = datetime.now(UTC) + timedelta(hours=18)
        gs = GameStatus(state="pre", scheduled_start=game_time)
        engine = _make_engine(game_status=gs, best_ask_price=50)

        pair = engine._scanner.pairs[0]
        ledger = engine._adjuster.get_ledger(pair.event_ticker)
        ledger.record_fill(Side.A, count=5, price=80)
        ledger.record_resting(Side.B, order_id="order-b-1", count=5, price=41)

        qi = ProposedQueueImprovement(
            event_ticker=pair.event_ticker,
            side="B",
            order_id="order-b-1",
            ticker=pair.ticker_b,
            current_price=41,
            improved_price=42,
            current_queue=186_000,
            eta_minutes=1860,
            time_remaining_minutes=1080,
            other_side_avg=80.0,
            kalshi_side="no",
        )

        notifications: list[str] = []
        engine.on_notification = lambda msg, sev="information", toast=False: notifications.append(
            msg
        )
        engine._verify_after_action = AsyncMock()

        await engine._execute_queue_improvement(qi)

        engine._rest.amend_order.assert_not_called()  # pyright: ignore[reportAttributeAccessIssue]
        assert any("unprofitable" in n for n in notifications)


class TestProposalModel:
    """Test the ProposedQueueImprovement model."""

    def test_model_creation(self):
        qi = ProposedQueueImprovement(
            event_ticker="EVT-1",
            side="B",
            order_id="ord-123",
            ticker="TK-B",
            current_price=41,
            improved_price=42,
            current_queue=186_000,
            eta_minutes=1860.0,
            time_remaining_minutes=1080.0,
            other_side_avg=57.4,
            kalshi_side="no",
        )
        assert qi.improved_price == qi.current_price + 1

    def test_proposal_key_with_queue_improve(self):
        key = ProposalKey(
            event_ticker="EVT-1",
            side="B",
            kind="queue_improve",
        )
        assert key.kind == "queue_improve"
        assert hash(key)


class TestBothSidesPartiallyFilled:
    """Test the case where both sides have fills but unequal."""

    def test_improve_side_with_fewer_fills(self):
        """When both sides have fills, improve the side with fewer."""
        game_time = datetime.now(UTC) + timedelta(hours=18)
        gs = GameStatus(state="pre", scheduled_start=game_time)
        engine = _make_engine(game_status=gs, best_ask_price=50)

        pair = engine._scanner.pairs[0]
        ledger = engine._adjuster.get_ledger(pair.event_ticker)
        ledger.record_fill(Side.A, count=5, price=57)
        ledger.record_fill(Side.B, count=2, price=41)
        ledger.record_resting(Side.B, order_id="order-b-1", count=3, price=41)
        engine._queue_cache["order-b-1"] = 186_000
        _seed_cpm(engine, pair.ticker_b, 100.0)

        engine.check_queue_stress()

        assert len(engine._proposal_queue) == 1
        proposal = engine._proposal_queue.pending()[0]
        assert proposal.queue_improve is not None
        assert proposal.queue_improve.side == "B"


class TestDeadMarket:
    """Test CPM=0 (dead market) edge case."""

    def test_cpm_zero_triggers_improvement(self):
        """CPM=0 → ETA=infinity → triggers improvement."""
        game_time = datetime.now(UTC) + timedelta(hours=18)
        gs = GameStatus(state="pre", scheduled_start=game_time)
        engine = _make_engine(game_status=gs, best_ask_price=50)

        pair = engine._scanner.pairs[0]
        ledger = engine._adjuster.get_ledger(pair.event_ticker)
        ledger.record_fill(Side.A, count=5, price=57)
        ledger.record_resting(Side.B, order_id="order-b-1", count=5, price=41)
        engine._queue_cache["order-b-1"] = 186_000

        _seed_cpm_zero(engine, pair.ticker_b)

        engine.check_queue_stress()
        assert len(engine._proposal_queue) == 1
