"""Tests for TradingEngine."""

from __future__ import annotations

import asyncio
from datetime import UTC
from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.automation_config import AutomationConfig
from talos.bid_adjuster import BidAdjuster
from talos.engine import TradingEngine
from talos.game_manager import GameManager
from talos.market_feed import MarketFeed
from talos.models.order import Order
from talos.models.portfolio import Balance, Position, Settlement
from talos.models.proposal import ProposalKey
from talos.models.strategy import ArbPair, Opportunity
from talos.models.ws import (
    FillMessage,
    OrderBookSnapshot,
    UserOrderMessage,
)
from talos.orderbook import OrderBookManager
from talos.position_ledger import Side
from talos.rest_client import KalshiRESTClient
from talos.scanner import ArbitrageScanner
from talos.top_of_market import TopOfMarketTracker


def _make_engine(**overrides) -> TradingEngine:
    """Build a TradingEngine with mock dependencies."""
    books = OrderBookManager()
    scanner = overrides.pop("scanner", ArbitrageScanner(books))
    defaults = dict(
        scanner=scanner,
        game_manager=overrides.pop("game_manager", MagicMock(spec=GameManager)),
        rest_client=overrides.pop("rest_client", AsyncMock(spec=KalshiRESTClient)),
        market_feed=overrides.pop("market_feed", MagicMock(spec=MarketFeed)),
        tracker=overrides.pop("tracker", TopOfMarketTracker(books)),
        adjuster=overrides.pop("adjuster", BidAdjuster(books, [], unit_size=10)),
    )
    defaults.update(overrides)
    return TradingEngine(**defaults)


class TestScaffold:
    def test_construction(self):
        engine = _make_engine()
        assert engine is not None

    def test_default_state(self):
        engine = _make_engine()
        assert engine.orders == []
        assert engine.order_data == []
        assert engine.position_summaries == []
        assert engine.balance == 0
        assert engine.portfolio_value == 0

    def test_properties_expose_dependencies(self):
        books = OrderBookManager()
        scanner = ArbitrageScanner(books)
        tracker = TopOfMarketTracker(books)
        adjuster = BidAdjuster(books, [], unit_size=10)
        engine = _make_engine(scanner=scanner, tracker=tracker, adjuster=adjuster)
        assert engine.scanner is scanner
        assert engine.tracker is tracker
        assert engine.adjuster is adjuster

    def test_initial_games_stored(self):
        engine = _make_engine(initial_games=["EVT-1", "EVT-2"])
        assert engine._initial_games == ["EVT-1", "EVT-2"]

    def test_callbacks_default_none(self):
        engine = _make_engine()
        assert engine.on_notification is None

    def test_proposal_queue_property(self):
        engine = _make_engine()
        assert engine.proposal_queue is not None
        assert len(engine.proposal_queue) == 0

    def test_active_market_tickers_empty(self):
        engine = _make_engine()
        assert engine._active_market_tickers() == []

    def test_active_market_tickers_with_pairs(self):
        books = OrderBookManager()
        scanner = ArbitrageScanner(books)
        scanner.add_pair("EVT-1", "TK-A", "TK-B")
        engine = _make_engine(scanner=scanner)
        tickers = engine._active_market_tickers()
        assert "TK-A" in tickers
        assert "TK-B" in tickers

    def test_notify_calls_callback(self):
        engine = _make_engine()
        callback = MagicMock()
        engine.on_notification = callback
        engine._notify("hello", "warning")
        callback.assert_called_once_with("hello", "warning")

    def test_notify_noop_without_callback(self):
        engine = _make_engine()
        engine._notify("hello")  # should not raise


def _make_order(
    ticker: str,
    *,
    order_id: str = "ord-1",
    fill_count: int = 0,
    remaining_count: int = 0,
    no_price: int = 45,
    status: str = "resting",
) -> Order:
    return Order(
        order_id=order_id,
        ticker=ticker,
        action="buy",
        side="no",
        no_price=no_price,
        initial_count=fill_count + remaining_count,
        remaining_count=remaining_count,
        fill_count=fill_count,
        status=status,
    )


def _engine_with_pair() -> tuple[TradingEngine, AsyncMock]:
    """Build an engine with one registered pair and a mock REST client."""
    books = OrderBookManager()
    scanner = ArbitrageScanner(books)
    scanner.add_pair("EVT-1", "TK-A", "TK-B")
    pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
    adjuster = BidAdjuster(books, [pair], unit_size=10)
    rest = AsyncMock(spec=KalshiRESTClient)
    engine = _make_engine(
        scanner=scanner,
        adjuster=adjuster,
        rest_client=rest,
    )
    return engine, rest


def _engine_with_pair_and_books(
    no_a: int = 45,
    no_b: int = 48,
) -> tuple[TradingEngine, AsyncMock]:
    """Engine with a registered pair AND orderbook data for catch-up pricing."""
    books = OrderBookManager()
    scanner = ArbitrageScanner(books)
    scanner.add_pair("EVT-1", "TK-A", "TK-B")
    books.apply_snapshot(
        "TK-A",
        OrderBookSnapshot(market_ticker="TK-A", market_id="m1", yes=[], no=[[no_a, 100]]),
    )
    books.apply_snapshot(
        "TK-B",
        OrderBookSnapshot(market_ticker="TK-B", market_id="m2", yes=[], no=[[no_b, 100]]),
    )
    scanner.scan("TK-A")
    pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
    adjuster = BidAdjuster(books, [pair], unit_size=10)
    rest = AsyncMock(spec=KalshiRESTClient)
    engine = _make_engine(
        scanner=scanner,
        adjuster=adjuster,
        rest_client=rest,
    )
    return engine, rest


class TestPolling:
    @pytest.mark.asyncio
    async def test_refresh_balance_updates_balance(self):
        engine, rest = _engine_with_pair()
        rest.get_balance.return_value = Balance(balance=50000, portfolio_value=60000)

        await engine.refresh_balance()

        assert engine.balance == 50000
        assert engine.portfolio_value == 60000

    @pytest.mark.asyncio
    async def test_refresh_account_fetches_all_orders(self):
        engine, rest = _engine_with_pair()
        rest.get_balance.return_value = Balance(balance=1000, portfolio_value=1000)
        rest.get_all_orders.return_value = []
        rest.get_queue_positions.return_value = {}

        await engine.refresh_account()

        rest.get_all_orders.assert_called_once()  # resting only

    @pytest.mark.asyncio
    async def test_refresh_account_stores_orders(self):
        engine, rest = _engine_with_pair()
        orders = [
            _make_order("TK-A", order_id="ord-a", fill_count=5, remaining_count=5),
        ]
        rest.get_balance.return_value = Balance(balance=1000, portfolio_value=1000)
        # First call (resting) returns the order, second call (executed) returns empty
        rest.get_all_orders.return_value = orders
        rest.get_queue_positions.return_value = {}

        await engine.refresh_account()

        assert len(engine.orders) == 1
        assert engine.orders[0].order_id == "ord-a"

    @pytest.mark.asyncio
    async def test_refresh_account_computes_position_summaries(self):
        engine, rest = _engine_with_pair()
        # Pre-seed ledger so sync_from_orders sees consistency
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=5, price=45)
        ledger.record_resting(Side.A, order_id="ord-a", count=5, price=45)
        ledger.record_fill(Side.B, count=5, price=47)
        ledger.record_resting(Side.B, order_id="ord-b", count=5, price=47)

        orders = [
            _make_order("TK-A", order_id="ord-a", fill_count=5, remaining_count=5, no_price=45),
            _make_order("TK-B", order_id="ord-b", fill_count=5, remaining_count=5, no_price=47),
        ]
        rest.get_balance.return_value = Balance(balance=1000, portfolio_value=1000)
        rest.get_all_orders.return_value = orders
        rest.get_queue_positions.return_value = {}

        await engine.refresh_account()

        assert len(engine.position_summaries) == 1
        s = engine.position_summaries[0]
        assert s.matched_pairs == 5
        assert s.leg_a.filled_count == 5

    @pytest.mark.asyncio
    async def test_refresh_account_builds_order_data(self):
        engine, rest = _engine_with_pair()
        orders = [
            _make_order("TK-A", order_id="ord-a", fill_count=3, remaining_count=7),
        ]
        rest.get_balance.return_value = Balance(balance=1000, portfolio_value=1000)
        rest.get_all_orders.return_value = orders
        rest.get_queue_positions.return_value = {}

        await engine.refresh_account()

        assert len(engine.order_data) == 1
        assert engine.order_data[0]["ticker"] == "TK-A"
        assert engine.order_data[0]["filled"] == 3

    @pytest.mark.asyncio
    async def test_refresh_queue_positions_merges_conservatively(self):
        engine, rest = _engine_with_pair()
        # Prime with orders via refresh_account
        orders = [
            _make_order("TK-A", order_id="ord-a", fill_count=0, remaining_count=10),
        ]
        rest.get_balance.return_value = Balance(balance=1000, portfolio_value=1000)
        rest.get_all_orders.return_value = orders
        rest.get_queue_positions.return_value = {"ord-a": 50}
        await engine.refresh_account()

        # Second poll returns worse position — should keep the better one
        rest.get_queue_positions.return_value = {"ord-a": 100}
        await engine.refresh_queue_positions()

        assert engine._queue_cache["ord-a"] == 50  # kept the smaller value

    @pytest.mark.asyncio
    async def test_refresh_trades_ingests_into_cpm(self):
        engine, rest = _engine_with_pair()
        from datetime import datetime

        from talos.models.market import Trade

        recent = datetime.now(UTC).isoformat()
        trades = [
            Trade(trade_id="t1", ticker="TK-A", count=50, price=45, side="no", created_time=recent),
        ]
        rest.get_trades.return_value = trades

        await engine.refresh_trades()

        assert engine._cpm.cpm("TK-A") is not None

    @pytest.mark.asyncio
    async def test_refresh_account_prunes_queue_cache(self):
        engine, rest = _engine_with_pair()
        # Seed cache with an old order
        engine._queue_cache["old-order"] = 10

        rest.get_balance.return_value = Balance(balance=1000, portfolio_value=1000)
        rest.get_all_orders.return_value = [
            _make_order("TK-A", order_id="new-order", remaining_count=10),
        ]
        rest.get_queue_positions.return_value = {}

        await engine.refresh_account()

        assert "old-order" not in engine._queue_cache

    @pytest.mark.asyncio
    async def test_refresh_account_syncs_ledger(self):
        engine, rest = _engine_with_pair()
        # Use resting-only orders — empty ledger matches (no fills)
        orders = [
            _make_order("TK-A", order_id="ord-a", fill_count=0, remaining_count=10, no_price=45),
        ]
        rest.get_balance.return_value = Balance(balance=1000, portfolio_value=1000)
        rest.get_all_orders.return_value = orders
        rest.get_queue_positions.return_value = {}

        await engine.refresh_account()

        ledger = engine.adjuster.get_ledger("EVT-1")
        # sync_from_orders updates resting state from Kalshi when consistent
        assert ledger.resting_order_id(Side.A) == "ord-a"
        assert ledger.resting_count(Side.A) == 10

    @pytest.mark.asyncio
    async def test_refresh_account_augments_fills_from_positions(self):
        """Fills from archived orders are recovered via positions API."""
        engine, rest = _engine_with_pair()
        # Orders API returns nothing (orders archived)
        rest.get_balance.return_value = Balance(balance=1000, portfolio_value=1000)
        rest.get_all_orders.return_value = []
        rest.get_queue_positions.return_value = {}
        # Positions API shows actual holdings
        rest.get_positions.return_value = [
            Position(ticker="TK-A", position=-30, total_traded=1380),
            Position(ticker="TK-B", position=-10, total_traded=520),
        ]

        await engine.refresh_account()

        ledger = engine.adjuster.get_ledger("EVT-1")
        assert ledger.filled_count(Side.A) == 30
        assert ledger.filled_count(Side.B) == 10
        assert ledger.filled_total_cost(Side.A) == 1380

    @pytest.mark.asyncio
    async def test_refresh_account_positions_failure_is_non_fatal(self):
        """If positions API fails, sync_from_orders still works."""
        engine, rest = _engine_with_pair()
        rest.get_all_orders.return_value = []
        rest.get_queue_positions.return_value = {}
        rest.get_positions.side_effect = RuntimeError("API error")

        # Should not raise — positions failure is logged, not fatal
        await engine.refresh_account()


class TestActions:
    @pytest.mark.asyncio
    async def test_place_bids_calls_create_order_twice(self):
        engine, rest = _engine_with_pair()
        rest.create_order.return_value = _make_order("TK-A", order_id="new-1")

        from talos.models.strategy import BidConfirmation

        bid = BidConfirmation(ticker_a="TK-A", ticker_b="TK-B", no_a=45, no_b=47, qty=10)
        await engine.place_bids(bid)

        assert rest.create_order.call_count == 2
        call_a, call_b = rest.create_order.call_args_list
        assert call_a.kwargs["ticker"] == "TK-A"
        assert call_b.kwargs["ticker"] == "TK-B"

    @pytest.mark.asyncio
    async def test_place_bids_error_notifies(self):
        engine, rest = _engine_with_pair()
        rest.create_order.side_effect = RuntimeError("API down")
        notifications = []
        engine.on_notification = lambda msg, sev: notifications.append((msg, sev))

        from talos.models.strategy import BidConfirmation

        bid = BidConfirmation(ticker_a="TK-A", ticker_b="TK-B", no_a=45, no_b=47, qty=10)
        await engine.place_bids(bid)

        assert any("error" in sev for _, sev in notifications)

    @pytest.mark.asyncio
    async def test_add_games_delegates_to_game_manager(self):
        engine, rest = _engine_with_pair()
        gm = engine.game_manager
        gm.add_games = AsyncMock()

        await engine.add_games(["url1", "url2"])

        gm.add_games.assert_called_once_with(["url1", "url2"])

    @pytest.mark.asyncio
    async def test_remove_game_delegates(self):
        engine, rest = _engine_with_pair()
        gm = engine.game_manager
        gm.remove_game = AsyncMock()

        await engine.remove_game("EVT-1")

        gm.remove_game.assert_called_once_with("EVT-1")

    @pytest.mark.asyncio
    async def test_approve_proposal_no_pending(self):
        engine, rest = _engine_with_pair()
        notifications = []
        engine.on_notification = lambda msg, sev: notifications.append((msg, sev))

        key = ProposalKey(event_ticker="EVT-1", side="A", kind="adjustment")
        await engine.approve_proposal(key)

        assert any("No pending proposal" in msg for msg, _ in notifications)

    def test_reject_proposal_clears_adjuster(self):
        engine, _ = _engine_with_pair()
        from datetime import datetime

        from talos.models.adjustment import ProposedAdjustment
        from talos.models.proposal import Proposal

        proposal = ProposedAdjustment(
            event_ticker="EVT-1",
            side="A",
            action="follow_jump",
            cancel_order_id="ord-1",
            cancel_count=10,
            cancel_price=45,
            new_count=10,
            new_price=48,
            reason="test",
            position_before="",
            position_after="",
            safety_check="",
        )
        engine.adjuster._proposals.setdefault("EVT-1", {})[Side.A] = proposal
        assert engine.adjuster.has_pending_proposal("EVT-1", Side.A)

        key = ProposalKey(event_ticker="EVT-1", side="A", kind="adjustment")
        envelope = Proposal(
            key=key,
            kind="adjustment",
            summary="test",
            detail="test",
            created_at=datetime.now(UTC),
            adjustment=proposal,
        )
        engine.proposal_queue.add(envelope)

        engine.reject_proposal(key)

        assert not engine.adjuster.has_pending_proposal("EVT-1", Side.A)


class TestPlaceBidsSafety:
    """Tests for place_bids safety gate and optimistic ledger update."""

    @pytest.mark.asyncio
    async def test_safety_gate_blocks_duplicate_placement(self):
        """When resting covers the full unit, place_bids blocks (exceeds unit capacity)."""
        engine, rest = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_resting(Side.A, order_id="existing-a", count=10, price=45)

        notifications: list[tuple[str, str]] = []
        engine.on_notification = lambda msg, sev: notifications.append((msg, sev))

        from talos.models.strategy import BidConfirmation

        bid = BidConfirmation(ticker_a="TK-A", ticker_b="TK-B", no_a=45, no_b=47, qty=10)
        await engine.place_bids(bid)

        assert rest.create_order.call_count == 0
        assert any("BLOCKED" in msg for msg, _ in notifications)

    @pytest.mark.asyncio
    async def test_safety_gate_blocks_exceeding_unit(self):
        """When partially filled and new qty would exceed unit, blocks."""
        engine, rest = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.B, count=5, price=47)

        notifications: list[tuple[str, str]] = []
        engine.on_notification = lambda msg, sev: notifications.append((msg, sev))

        from talos.models.strategy import BidConfirmation

        bid = BidConfirmation(ticker_a="TK-A", ticker_b="TK-B", no_a=45, no_b=47, qty=10)
        await engine.place_bids(bid)

        assert rest.create_order.call_count == 0
        assert any("BLOCKED" in msg for msg, _ in notifications)

    @pytest.mark.asyncio
    async def test_safety_gate_allows_reentry_after_unit_complete(self):
        """When both sides have a complete unit, new placement is allowed."""
        engine, rest = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=10, price=45)
        ledger.record_fill(Side.B, count=10, price=47)

        rest.create_order.return_value = _make_order("TK-A", order_id="new-1", remaining_count=10)

        from talos.models.strategy import BidConfirmation

        bid = BidConfirmation(ticker_a="TK-A", ticker_b="TK-B", no_a=46, no_b=48, qty=10)
        await engine.place_bids(bid)

        assert rest.create_order.call_count == 2

    @pytest.mark.asyncio
    async def test_ledger_updated_optimistically_after_placement(self):
        """Ledger IS updated optimistically to prevent duplicate proposals
        from concurrent refresh_account with stale data."""
        engine, rest = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")

        # create_order returns different orders for each call
        order_a = _make_order("TK-A", order_id="new-a", remaining_count=10, no_price=45)
        order_b = _make_order("TK-B", order_id="new-b", remaining_count=10, no_price=47)
        rest.create_order.side_effect = [order_a, order_b]

        from talos.models.strategy import BidConfirmation

        bid = BidConfirmation(ticker_a="TK-A", ticker_b="TK-B", no_a=45, no_b=47, qty=10)
        await engine.place_bids(bid)

        # Ledger should reflect the placed orders immediately
        assert ledger.resting_order_id(Side.A) == "new-a"
        assert ledger.resting_count(Side.A) == 10
        assert ledger.resting_order_id(Side.B) == "new-b"
        assert ledger.resting_count(Side.B) == 10
        # Orders also added to cache for WS handler
        assert any(o.order_id == "new-a" for o in engine.orders)
        assert any(o.order_id == "new-b" for o in engine.orders)


class FakeBookManager:
    """Minimal fake for OrderBookManager.best_ask()."""

    def __init__(self, prices: dict[str, int]):
        self._prices = prices

    def best_ask(self, ticker: str):
        price = self._prices.get(ticker)
        if price is None:
            return None

        class Level:
            pass

        level = Level()
        level.price = price
        return level


def _engine_with_jump_setup() -> TradingEngine:
    """Engine with a pair where side B can be jumped from 47->48."""
    books = FakeBookManager({"TK-A": 50, "TK-B": 48})
    scanner = ArbitrageScanner(books)
    scanner.add_pair("EVT-1", "TK-A", "TK-B")
    pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
    adjuster = BidAdjuster(books, [pair], unit_size=10)
    tracker = TopOfMarketTracker(books)
    engine = TradingEngine(
        scanner=scanner,
        game_manager=MagicMock(spec=GameManager),
        rest_client=AsyncMock(spec=KalshiRESTClient),
        market_feed=MagicMock(spec=MarketFeed),
        tracker=tracker,
        adjuster=adjuster,
    )
    # Setup: side A filled, side B resting at 47
    ledger = adjuster.get_ledger("EVT-1")
    ledger.record_fill(Side.A, count=10, price=50)
    ledger.record_resting(Side.B, order_id="ord-b", count=10, price=47)
    return engine


class TestProposalQueue:
    def test_jump_adds_proposal_to_queue(self):
        engine = _engine_with_jump_setup()
        engine.on_top_of_market_change("TK-B", at_top=False)
        assert len(engine.proposal_queue) == 1
        p = engine.proposal_queue.pending()[0]
        assert p.kind == "adjustment"
        assert p.adjustment is not None
        assert p.adjustment.new_price == 48

    def test_back_at_top_no_proposal(self):
        engine = _engine_with_jump_setup()
        engine.on_top_of_market_change("TK-B", at_top=True)
        assert len(engine.proposal_queue) == 0

    @pytest.mark.asyncio
    async def test_approve_proposal_executes_adjustment(self):
        engine = _engine_with_jump_setup()
        engine.on_top_of_market_change("TK-B", at_top=False)
        key = engine.proposal_queue.pending()[0].key
        # Mock the amend call
        old_order = _make_order(
            "TK-B", order_id="ord-b", fill_count=0, remaining_count=10, no_price=47
        )
        new_order = _make_order(
            "TK-B", order_id="ord-b-new", fill_count=0, remaining_count=10, no_price=48
        )
        engine._rest.amend_order = AsyncMock(return_value=(old_order, new_order))
        await engine.approve_proposal(key)
        assert len(engine.proposal_queue) == 0
        engine._rest.amend_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_approve_missing_proposal_notifies(self):
        engine = _engine_with_jump_setup()
        notifications: list[tuple[str, str]] = []
        engine.on_notification = lambda msg, sev: notifications.append((msg, sev))
        key = ProposalKey(event_ticker="EVT-1", side="B", kind="adjustment")
        await engine.approve_proposal(key)
        assert any("No pending" in msg for msg, _ in notifications)

    def test_reject_proposal_removes_from_queue(self):
        engine = _engine_with_jump_setup()
        engine.on_top_of_market_change("TK-B", at_top=False)
        key = engine.proposal_queue.pending()[0].key
        engine.reject_proposal(key)
        assert len(engine.proposal_queue) == 0

    def test_unprofitable_no_fills_generates_withdraw(self):
        """When jumped and unprofitable with 0 fills, propose withdrawal."""
        # fee_adjusted_cost(53) + fee_adjusted_cost(50) ≈ 53.87 + 50.875 = 104.7 >= 100
        books = FakeBookManager({"TK-A": 50, "TK-B": 53})
        scanner = ArbitrageScanner(books)
        scanner.add_pair("EVT-1", "TK-A", "TK-B")
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        adjuster = BidAdjuster(books, [pair], unit_size=10)
        tracker = TopOfMarketTracker(books)
        engine = TradingEngine(
            scanner=scanner,
            game_manager=MagicMock(spec=GameManager),
            rest_client=AsyncMock(spec=KalshiRESTClient),
            market_feed=MagicMock(spec=MarketFeed),
            tracker=tracker,
            adjuster=adjuster,
        )
        ledger = adjuster.get_ledger("EVT-1")
        # Both sides resting, 0 fills
        ledger.record_resting(Side.A, order_id="ord-a", count=10, price=48)
        ledger.record_resting(Side.B, order_id="ord-b", count=10, price=47)

        engine.on_top_of_market_change("TK-B", at_top=False)
        assert len(engine.proposal_queue) == 1
        p = engine.proposal_queue.pending()[0]
        assert p.kind == "withdraw"
        assert p.key.side == ""  # event-level, not side-level

    @pytest.mark.asyncio
    async def test_approve_withdraw_cancels_both_orders(self):
        """Approving a withdraw proposal cancels both sides' resting orders."""
        books = FakeBookManager({"TK-A": 50, "TK-B": 53})
        scanner = ArbitrageScanner(books)
        scanner.add_pair("EVT-1", "TK-A", "TK-B")
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        adjuster = BidAdjuster(books, [pair], unit_size=10)
        tracker = TopOfMarketTracker(books)
        rest = AsyncMock(spec=KalshiRESTClient)
        rest.get_orders = AsyncMock(return_value=[])
        rest.get_positions = AsyncMock(return_value=[])
        engine = TradingEngine(
            scanner=scanner,
            game_manager=MagicMock(spec=GameManager),
            rest_client=rest,
            market_feed=MagicMock(spec=MarketFeed),
            tracker=tracker,
            adjuster=adjuster,
        )
        ledger = adjuster.get_ledger("EVT-1")
        ledger.record_resting(Side.A, order_id="ord-a", count=10, price=48)
        ledger.record_resting(Side.B, order_id="ord-b", count=10, price=47)

        engine.on_top_of_market_change("TK-B", at_top=False)
        key = engine.proposal_queue.pending()[0].key
        await engine.approve_proposal(key)

        # Both orders should have been cancelled
        cancel_calls = rest.cancel_order.call_args_list
        cancelled_ids = {call.args[0] for call in cancel_calls}
        assert cancelled_ids == {"ord-a", "ord-b"}


# ── Automation / OpportunityProposer integration ──────────────────────


def _engine_with_automation() -> tuple[TradingEngine, AsyncMock]:
    """Engine with automation enabled and one profitable scanner pair."""
    books = OrderBookManager()
    scanner = ArbitrageScanner(books)
    scanner.add_pair("EVT-1", "TK-A", "TK-B")

    # Apply snapshots: NO-A=45, NO-B=48 → fee_edge ≈ 6.04c (above 1.5c threshold)
    books.apply_snapshot(
        "TK-A",
        OrderBookSnapshot(market_ticker="TK-A", market_id="m1", yes=[], no=[[45, 100]]),
    )
    books.apply_snapshot(
        "TK-B",
        OrderBookSnapshot(market_ticker="TK-B", market_id="m2", yes=[], no=[[48, 100]]),
    )
    scanner.scan("TK-A")
    assert scanner.get_opportunity("EVT-1") is not None

    pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
    adjuster = BidAdjuster(books, [pair], unit_size=10)
    rest = AsyncMock(spec=KalshiRESTClient)
    config = AutomationConfig(
        edge_threshold_cents=1.5,
        stability_seconds=0.0,
        enabled=True,
    )
    engine = TradingEngine(
        scanner=scanner,
        game_manager=MagicMock(spec=GameManager),
        rest_client=rest,
        market_feed=MagicMock(spec=MarketFeed),
        tracker=TopOfMarketTracker(books),
        adjuster=adjuster,
        automation_config=config,
    )
    return engine, rest


class TestOpportunityProposerIntegration:
    def test_automation_config_property(self):
        """Engine exposes automation config via property."""
        engine, _ = _engine_with_automation()
        assert engine.automation_config.enabled is True
        assert engine.automation_config.edge_threshold_cents == 1.5

    def test_evaluate_opportunities_disabled(self):
        """No proposals when automation is disabled."""
        engine, _ = _engine_with_automation()
        engine._auto_config.enabled = False
        engine.evaluate_opportunities()
        assert len(engine.proposal_queue) == 0

    def test_evaluate_opportunities_proposes_bid(self):
        """Profitable opportunity generates a bid proposal."""
        engine, _ = _engine_with_automation()
        engine.evaluate_opportunities()
        assert len(engine.proposal_queue) == 1
        p = engine.proposal_queue.pending()[0]
        assert p.kind == "bid"
        assert p.bid is not None
        assert p.bid.event_ticker == "EVT-1"

    def test_evaluate_opportunities_no_duplicate(self):
        """Second evaluate does not create a duplicate proposal."""
        engine, _ = _engine_with_automation()
        engine.evaluate_opportunities()
        engine.evaluate_opportunities()
        assert len(engine.proposal_queue) == 1

    def test_reject_bid_records_cooldown(self):
        """Rejecting a bid proposal starts cooldown, blocking re-proposal."""
        engine, _ = _engine_with_automation()
        engine.evaluate_opportunities()
        assert len(engine.proposal_queue) == 1
        key = engine.proposal_queue.pending()[0].key
        engine.reject_proposal(key)
        assert len(engine.proposal_queue) == 0
        # Proposer should now be in cooldown for this event
        engine.evaluate_opportunities()
        assert len(engine.proposal_queue) == 0  # still in cooldown

    @pytest.mark.asyncio
    async def test_refresh_account_calls_evaluate(self):
        """refresh_account calls evaluate_opportunities at end of cycle."""
        engine, rest = _engine_with_automation()
        rest.get_balance.return_value = Balance(balance=1000, portfolio_value=1000)
        rest.get_all_orders.return_value = []
        rest.get_queue_positions.return_value = {}

        await engine.refresh_account()

        # Should have created a bid proposal during the polling cycle
        assert len(engine.proposal_queue) == 1
        assert engine.proposal_queue.pending()[0].kind == "bid"


# ── Imbalance detection ────────────────────────────────────────────


class TestCheckImbalances:
    @pytest.mark.asyncio
    async def test_no_duplicate_rebalance_proposals(self):
        """check_imbalances auto-executes and doesn't queue proposals."""
        engine, rest = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 50, 45)
        ledger.record_resting(Side.A, "ord-a", 60, 45)
        ledger.record_fill(Side.B, 50, 47)
        ledger.record_resting(Side.B, "ord-b", 10, 47)

        engine.scanner._all_snapshots["EVT-1"] = Opportunity(
            event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B",
            no_a=45, no_b=47, qty_a=100, qty_b=100,
            raw_edge=8, fee_edge=0.0, tradeable_qty=100,
            timestamp="2026-03-16T00:00:00Z",
        )

        rest.get_all_orders = AsyncMock(return_value=[
            _make_order("TK-A", order_id="ord-a", fill_count=50, remaining_count=60, no_price=45),
            _make_order("TK-B", order_id="ord-b", fill_count=50, remaining_count=10, no_price=47),
        ])
        rest.cancel_order = AsyncMock()
        rest.create_order = AsyncMock(return_value=_make_order("TK-B", order_id="new-b"))
        rest.create_order_group = AsyncMock(return_value="grp-test")

        await engine.check_imbalances()

        # No proposals queued — auto-executed directly
        assert len(engine.proposal_queue) == 0

    @pytest.mark.asyncio
    async def test_rebalance_approve_verifies_after_action(self):
        """Approving a rebalance runs verify_after_action from the engine."""
        from datetime import UTC, datetime

        from talos.models.proposal import Proposal, ProposalKey, ProposedRebalance

        engine, rest = _engine_with_pair_and_books()
        ledger = engine.adjuster.get_ledger("EVT-1")
        # A: 40f+10r=50, B: 20f+10r=30 -> reduce A only (B has resting)
        ledger.record_fill(Side.A, 40, 45)
        ledger.record_resting(Side.A, "ord-a", 10, 45)
        ledger.record_fill(Side.B, 20, 48)
        ledger.record_resting(Side.B, "ord-b", 10, 48)

        # Manually seed ProposalQueue (check_imbalances no longer queues)
        key = ProposalKey(event_ticker="EVT-1", side="A", kind="rebalance")
        proposal = Proposal(
            key=key,
            kind="rebalance",
            summary="REBALANCE EVT-1 side A",
            detail="test",
            created_at=datetime.now(UTC),
            rebalance=ProposedRebalance(
                event_ticker="EVT-1",
                side="A",
                order_id="ord-a",
                ticker="TK-A",
                current_resting=10,
                target_resting=0,
                filled_count=40,
                resting_price=45,
            ),
        )
        engine.proposal_queue.add(proposal)

        rest.cancel_order = AsyncMock()
        # Verification sync happens after step 1 (per-ticker fetches)
        rest.get_orders = AsyncMock(return_value=[])
        rest.get_positions = AsyncMock(return_value=[])

        await engine.approve_proposal(key)

        rest.cancel_order.assert_called_once_with("ord-a")
        # Post-action verification did run
        assert rest.get_orders.call_count == 2  # one per ticker
        rest.get_positions.assert_called_once()

    @pytest.mark.asyncio
    async def test_verify_failure_notifies_operator(self):
        """When post-action verify fails, operator sees a warning toast."""
        from datetime import UTC, datetime

        from talos.models.proposal import Proposal, ProposalKey, ProposedRebalance

        engine, rest = _engine_with_pair_and_books()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 40, 45)
        ledger.record_resting(Side.A, "ord-a", 10, 45)
        ledger.record_fill(Side.B, 20, 48)
        ledger.record_resting(Side.B, "ord-b", 10, 48)

        # Manually seed ProposalQueue (check_imbalances no longer queues)
        key = ProposalKey(event_ticker="EVT-1", side="A", kind="rebalance")
        proposal = Proposal(
            key=key,
            kind="rebalance",
            summary="REBALANCE EVT-1 side A",
            detail="test",
            created_at=datetime.now(UTC),
            rebalance=ProposedRebalance(
                event_ticker="EVT-1",
                side="A",
                order_id="ord-a",
                ticker="TK-A",
                current_resting=10,
                target_resting=0,
                filled_count=40,
                resting_price=45,
            ),
        )
        engine.proposal_queue.add(proposal)

        rest.cancel_order = AsyncMock()
        # Verify fails — API unreachable after action
        rest.get_orders = AsyncMock(side_effect=RuntimeError("API down"))

        notifications: list[tuple[str, str]] = []
        engine.on_notification = lambda msg, sev: notifications.append((msg, sev))

        await engine.approve_proposal(key)

        # Action succeeded, but verify failed — operator should see warning
        rest.cancel_order.assert_called_once()
        assert any("Verify FAILED" in msg for msg, _ in notifications)
        assert any(sev == "warning" for _, sev in notifications)


class TestComputeEventStatus:
    def test_jumped_a_when_not_at_top(self):
        """Status is 'Jumped A' when side A resting order is not at top of market."""
        books = OrderBookManager()
        scanner = ArbitrageScanner(books)
        scanner.add_pair("EVT-1", "TK-A", "TK-B")
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        tracker = TopOfMarketTracker(books)
        adjuster = BidAdjuster(books, [pair], unit_size=10)
        engine = _make_engine(
            scanner=scanner,
            adjuster=adjuster,
            tracker=tracker,
        )

        # Record resting orders on side A
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_resting(Side.A, "ord-a", 10, 45)

        # Set up tracker: resting at 45, book top at 46 → jumped
        order_a = Order(
            order_id="ord-a",
            ticker="TK-A",
            side="no",
            action="buy",
            no_price=45,
            remaining_count=10,
            status="resting",
        )
        tracker.update_orders([order_a], [pair])
        books.apply_snapshot(
            "TK-A",
            OrderBookSnapshot(market_ticker="TK-A", market_id="m1", yes=[], no=[[46, 50]]),
        )
        tracker.check("TK-A")
        assert tracker.is_at_top("TK-A") is False

        status = engine._compute_event_status("EVT-1")
        assert status == "Jumped A"

    def test_jumped_ab_when_both_not_at_top(self):
        """Status is 'Jumped AB' when both sides are not at top of market."""
        books = OrderBookManager()
        scanner = ArbitrageScanner(books)
        scanner.add_pair("EVT-1", "TK-A", "TK-B")
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        tracker = TopOfMarketTracker(books)
        adjuster = BidAdjuster(books, [pair], unit_size=10)
        engine = _make_engine(
            scanner=scanner,
            adjuster=adjuster,
            tracker=tracker,
        )

        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_resting(Side.A, "ord-a", 10, 45)
        ledger.record_resting(Side.B, "ord-b", 10, 48)

        order_a = Order(
            order_id="ord-a",
            ticker="TK-A",
            side="no",
            action="buy",
            no_price=45,
            remaining_count=10,
            status="resting",
        )
        order_b = Order(
            order_id="ord-b",
            ticker="TK-B",
            side="no",
            action="buy",
            no_price=48,
            remaining_count=10,
            status="resting",
        )
        tracker.update_orders([order_a, order_b], [pair])
        books.apply_snapshot(
            "TK-A",
            OrderBookSnapshot(market_ticker="TK-A", market_id="m1", yes=[], no=[[46, 50]]),
        )
        books.apply_snapshot(
            "TK-B",
            OrderBookSnapshot(market_ticker="TK-B", market_id="m2", yes=[], no=[[49, 50]]),
        )
        tracker.check("TK-A")
        tracker.check("TK-B")

        status = engine._compute_event_status("EVT-1")
        assert status == "Jumped AB"

    def test_no_jumped_when_at_top(self):
        """No Jumped status when resting orders are at top of market."""
        books = OrderBookManager()
        scanner = ArbitrageScanner(books)
        scanner.add_pair("EVT-1", "TK-A", "TK-B")
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        tracker = TopOfMarketTracker(books)
        adjuster = BidAdjuster(books, [pair], unit_size=10)
        engine = _make_engine(
            scanner=scanner,
            adjuster=adjuster,
            tracker=tracker,
        )

        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_resting(Side.A, "ord-a", 10, 45)
        ledger.record_resting(Side.B, "ord-b", 10, 48)

        order_a = Order(
            order_id="ord-a",
            ticker="TK-A",
            side="no",
            action="buy",
            no_price=45,
            remaining_count=10,
            status="resting",
        )
        order_b = Order(
            order_id="ord-b",
            ticker="TK-B",
            side="no",
            action="buy",
            no_price=48,
            remaining_count=10,
            status="resting",
        )
        tracker.update_orders([order_a, order_b], [pair])
        # Book top matches our resting prices → at top
        books.apply_snapshot(
            "TK-A",
            OrderBookSnapshot(market_ticker="TK-A", market_id="m1", yes=[], no=[[45, 50]]),
        )
        books.apply_snapshot(
            "TK-B",
            OrderBookSnapshot(market_ticker="TK-B", market_id="m2", yes=[], no=[[48, 50]]),
        )
        tracker.check("TK-A")
        tracker.check("TK-B")

        status = engine._compute_event_status("EVT-1")
        assert not status.startswith("Jumped")


# ── Stale book recovery ────────────────────────────────────────────


def _engine_with_real_feed() -> tuple[TradingEngine, AsyncMock, OrderBookManager]:
    """Build an engine with a real OrderBookManager and mock MarketFeed."""
    books = OrderBookManager()
    scanner = ArbitrageScanner(books)
    scanner.add_pair("EVT-1", "TK-A", "TK-B")
    pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
    adjuster = BidAdjuster(books, [pair], unit_size=10)

    # Apply initial snapshots so books exist
    books.apply_snapshot(
        "TK-A",
        OrderBookSnapshot(market_ticker="TK-A", market_id="m1", yes=[], no=[[45, 100]]),
    )
    books.apply_snapshot(
        "TK-B",
        OrderBookSnapshot(market_ticker="TK-B", market_id="m2", yes=[], no=[[48, 100]]),
    )

    feed = AsyncMock(spec=MarketFeed)
    feed.book_manager = books
    rest = AsyncMock(spec=KalshiRESTClient)
    engine = _make_engine(
        scanner=scanner,
        adjuster=adjuster,
        rest_client=rest,
        market_feed=feed,
    )
    return engine, rest, books


class TestStaleBookRecovery:
    @pytest.mark.asyncio
    async def test_no_stale_books_does_nothing(self):
        engine, _, books = _engine_with_real_feed()
        # No books are stale — subscribe/unsubscribe should not be called
        await engine._recover_stale_books()
        engine._feed.unsubscribe.assert_not_called()
        engine._feed.subscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_book_triggers_resubscribe(self):
        engine, _, books = _engine_with_real_feed()
        # Mark TK-A as stale
        books.get_book("TK-A").stale = True

        await engine._recover_stale_books()

        engine._feed.unsubscribe.assert_called_once_with("TK-A")
        engine._feed.subscribe.assert_called_once_with("TK-A")

    @pytest.mark.asyncio
    async def test_multiple_stale_books_all_recovered(self):
        engine, _, books = _engine_with_real_feed()
        books.get_book("TK-A").stale = True
        books.get_book("TK-B").stale = True

        await engine._recover_stale_books()

        assert engine._feed.unsubscribe.call_count == 2
        assert engine._feed.subscribe.call_count == 2

    @pytest.mark.asyncio
    async def test_stale_non_active_ticker_ignored(self):
        engine, _, books = _engine_with_real_feed()
        # Add a stale book for a ticker not in any active pair
        books.apply_snapshot(
            "TK-ORPHAN",
            OrderBookSnapshot(market_ticker="TK-ORPHAN", market_id="m3", yes=[], no=[[50, 10]]),
        )
        books.get_book("TK-ORPHAN").stale = True

        await engine._recover_stale_books()

        engine._feed.unsubscribe.assert_not_called()
        engine._feed.subscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_resubscribe_failure_does_not_crash(self):
        engine, _, books = _engine_with_real_feed()
        books.get_book("TK-A").stale = True
        engine._feed.unsubscribe.side_effect = RuntimeError("WS disconnected")

        # Should not raise
        await engine._recover_stale_books()

    @pytest.mark.asyncio
    async def test_refresh_account_calls_recovery(self):
        """refresh_account triggers stale book recovery before main logic."""
        engine, rest, books = _engine_with_real_feed()
        books.get_book("TK-A").stale = True

        rest.get_balance.return_value = Balance(balance=1000, portfolio_value=1000)
        rest.get_all_orders.return_value = []
        rest.get_queue_positions.return_value = {}

        await engine.refresh_account()

        # Recovery should have been called (unsubscribe + subscribe for TK-A)
        engine._feed.unsubscribe.assert_called_once_with("TK-A")
        engine._feed.subscribe.assert_called_once_with("TK-A")


class TestPortfolioFeedWiring:
    def test_engine_without_portfolio_feed_works(self):
        engine = _make_engine()
        assert engine._portfolio_feed is None

    def test_engine_with_portfolio_feed_wires_callbacks(self):
        from talos.portfolio_feed import PortfolioFeed

        ws = MagicMock()
        ws.on_message = MagicMock()
        pf = PortfolioFeed(ws_client=ws)
        _make_engine(portfolio_feed=pf)
        assert pf.on_order_update is not None
        assert pf.on_fill is not None


class TestOnOrderUpdate:
    def test_updates_order_fill_count_monotonically(self):
        engine, _ = _engine_with_pair()
        order = _make_order("TK-A", order_id="ord-a", fill_count=5, remaining_count=5)
        engine._orders_cache = [order]

        msg = UserOrderMessage(
            order_id="ord-a",
            ticker="TK-A",
            side="no",
            status="resting",
            fill_count=8,
            remaining_count=2,
            no_price=45,
        )
        engine._on_order_update(msg)
        assert order.fill_count == 8
        assert order.remaining_count == 2

    def test_does_not_decrease_fill_count(self):
        engine, _ = _engine_with_pair()
        order = _make_order("TK-A", order_id="ord-a", fill_count=10, remaining_count=0)
        engine._orders_cache = [order]

        msg = UserOrderMessage(
            order_id="ord-a",
            ticker="TK-A",
            side="no",
            status="resting",
            fill_count=5,
            remaining_count=5,
            no_price=45,
        )
        engine._on_order_update(msg)
        assert order.fill_count == 10

    def test_notifies_on_new_fills(self):
        engine, _ = _engine_with_pair()
        callback = MagicMock()
        engine.on_notification = callback
        order = _make_order("TK-A", order_id="ord-a", fill_count=5, remaining_count=5)
        engine._orders_cache = [order]

        msg = UserOrderMessage(
            order_id="ord-a",
            ticker="TK-A",
            side="no",
            status="resting",
            fill_count=8,
            remaining_count=2,
            no_price=45,
        )
        engine._on_order_update(msg)
        callback.assert_called_once()
        assert "WS fill: 3" in callback.call_args[0][0]

    def test_no_notification_when_fill_count_unchanged(self):
        engine, _ = _engine_with_pair()
        callback = MagicMock()
        engine.on_notification = callback
        order = _make_order("TK-A", order_id="ord-a", fill_count=5, remaining_count=5)
        engine._orders_cache = [order]

        msg = UserOrderMessage(
            order_id="ord-a",
            ticker="TK-A",
            side="no",
            status="resting",
            fill_count=5,
            remaining_count=5,
            no_price=45,
        )
        engine._on_order_update(msg)
        callback.assert_not_called()

    def test_unknown_order_is_logged_not_error(self):
        engine, _ = _engine_with_pair()
        engine._orders_cache = []

        msg = UserOrderMessage(
            order_id="unknown-ord",
            ticker="TK-A",
            side="no",
            status="resting",
            fill_count=1,
            remaining_count=9,
        )
        engine._on_order_update(msg)  # Should not raise

    def test_resyncs_ledger_on_update(self):
        engine, _ = _engine_with_pair()
        order_a = _make_order("TK-A", order_id="ord-a", fill_count=0, remaining_count=10)
        engine._orders_cache = [order_a]
        ledger = engine._adjuster.get_ledger("EVT-1")

        msg = UserOrderMessage(
            order_id="ord-a",
            ticker="TK-A",
            side="no",
            status="resting",
            fill_count=5,
            remaining_count=5,
            no_price=45,
        )
        engine._on_order_update(msg)
        assert ledger.filled_count(Side.A) == 5


class TestOnFill:
    def test_fill_handler_does_not_crash(self):
        engine, _ = _engine_with_pair()
        msg = FillMessage(
            trade_id="fill-1",
            order_id="ord-a",
            market_ticker="TK-A",
            side="no",
            count=3,
            yes_price=55,
            post_position=-3,
        )
        engine._on_fill(msg)  # Should not raise


class TestLifecycleFiltering:
    """Lifecycle notifications should only fire for tracked markets."""

    @pytest.mark.asyncio
    async def test_settled_notification_only_for_our_markets(self):
        engine, rest = _engine_with_pair()
        rest.get_settlements = AsyncMock(return_value=[])
        notifications: list[str] = []
        engine.on_notification = lambda msg, sev="information": notifications.append(msg)

        # Our market → should notify
        engine._on_market_settled("TK-A")
        await asyncio.sleep(0)
        assert any("TK-A" in n for n in notifications)

        # Random market → should NOT notify
        notifications.clear()
        engine._on_market_settled("UNRELATED-MKT")
        await asyncio.sleep(0)
        assert not notifications

    def test_determined_notification_only_for_our_markets(self):
        engine, _ = _engine_with_pair()
        notifications: list[str] = []
        engine.on_notification = lambda msg, sev="information": notifications.append(msg)

        engine._on_market_determined("TK-B", "yes", 100)
        assert any("TK-B" in n for n in notifications)

        notifications.clear()
        engine._on_market_determined("UNRELATED-MKT", "no", 0)
        assert not notifications

    def test_paused_notification_only_for_our_markets(self):
        engine, _ = _engine_with_pair()
        notifications: list[str] = []
        engine.on_notification = lambda msg, sev="information": notifications.append(msg)

        engine._on_market_paused("TK-A", True)
        assert any("TK-A" in n for n in notifications)

        notifications.clear()
        engine._on_market_paused("UNRELATED-MKT", True)
        assert not notifications

    def test_paused_still_tracks_state_for_unrelated(self):
        """Even untracked markets get added to paused set (for safety)."""
        engine, _ = _engine_with_pair()
        engine._on_market_paused("UNRELATED-MKT", True)
        assert "UNRELATED-MKT" in engine.paused_markets

    @pytest.mark.asyncio
    async def test_settled_fetches_settlement_for_our_market(self):
        engine, rest = _engine_with_pair()
        settlement = Settlement(
            ticker="TK-A",
            event_ticker="EVT-1",
            market_result="no",
            revenue=200,
            fee_cost=10,
            no_count=5,
        )
        rest.get_settlements = AsyncMock(return_value=[settlement])
        notifications: list[str] = []
        engine.on_notification = lambda msg, sev="information": notifications.append(msg)

        engine._on_market_settled("TK-A")
        # Let the fire-and-forget task run
        await asyncio.sleep(0)

        rest.get_settlements.assert_called_once_with(ticker="TK-A")
        assert any("Settlement TK-A" in n for n in notifications)
        assert any("rev $2.00" in n for n in notifications)

    @pytest.mark.asyncio
    async def test_settled_no_fetch_for_unrelated_market(self):
        engine, rest = _engine_with_pair()
        rest.get_settlements = AsyncMock(return_value=[])

        engine._on_market_settled("UNRELATED-MKT")
        await asyncio.sleep(0)

        rest.get_settlements.assert_not_called()


class TestReconcileWithKalshi:
    """Full ledger reconciliation against Kalshi API data."""

    def _notify_collector(self, engine: TradingEngine) -> list[tuple[str, str]]:
        notifications: list[tuple[str, str]] = []
        engine.on_notification = lambda msg, sev="info": notifications.append((msg, sev))
        return notifications

    def test_clean_state_no_alerts(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Kalshi and ledger agree perfectly — no alerts."""
        engine, _ = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 5, 45)
        ledger.record_resting(Side.A, "ord-a", 5, 45)
        notes = self._notify_collector(engine)

        orders = [
            _make_order("TK-A", order_id="ord-a", fill_count=5, remaining_count=5),
        ]
        engine._reconcile_with_kalshi(orders, {})
        assert not notes
        assert "reconcile" not in capsys.readouterr().out

    def test_overcommit_from_double_bid(self) -> None:
        """Double-bid scenario: 20 resting on unit_size=10."""
        engine, _ = _engine_with_pair()
        notes = self._notify_collector(engine)

        # Two separate orders on the same side — the double-bid signature
        orders = [
            _make_order("TK-A", order_id="ord-1", fill_count=0, remaining_count=10),
            _make_order("TK-A", order_id="ord-2", fill_count=0, remaining_count=10),
        ]
        engine._reconcile_with_kalshi(orders, {})
        errors = [msg for msg, sev in notes if sev == "error"]
        assert any("OVERCOMMIT" in msg for msg in errors)
        # Multi-order is logged but not toasted (too noisy)

    def test_multiple_resting_orders_logged_not_toasted(self) -> None:
        """Two resting orders on same side — logged but no toast (too noisy)."""
        engine, _ = _engine_with_pair()
        notes = self._notify_collector(engine)

        # 3 + 3 = 6 ≤ 10, but still two orders
        orders = [
            _make_order("TK-A", order_id="ord-1", fill_count=0, remaining_count=3),
            _make_order("TK-A", order_id="ord-2", fill_count=0, remaining_count=3),
        ]
        engine._reconcile_with_kalshi(orders, {})
        # No toast for multi-order (logged only)
        warnings = [msg for msg, sev in notes if sev == "warning"]
        assert not any("MULTI-ORDER" in msg for msg in warnings)
        # No overcommit (6 ≤ 10)
        errors = [msg for msg, sev in notes if sev == "error"]
        assert not errors

    def test_no_alert_after_unit_complete_reentry(self) -> None:
        """10 filled (unit complete) + 10 resting = valid re-entry."""
        engine, _ = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 10, 45)
        ledger.record_resting(Side.A, "ord-2", 10, 46)
        notes = self._notify_collector(engine)

        orders = [
            _make_order("TK-A", order_id="ord-1", fill_count=10, remaining_count=0, status="executed"),
            _make_order("TK-A", order_id="ord-2", fill_count=0, remaining_count=10),
        ]
        engine._reconcile_with_kalshi(orders, {})
        assert not any("OVERCOMMIT" in msg for msg, _ in notes)

    def test_fill_mismatch_logged(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Ledger fills disagree with Kalshi — log warning."""
        engine, _ = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 10, 45)  # Ledger says 10

        from talos.models.portfolio import Position

        # Kalshi orders say 10, but positions API says 15
        orders = [
            _make_order("TK-A", order_id="ord-a", fill_count=10, remaining_count=0, status="executed"),
        ]
        pos_map = {"TK-A": Position(ticker="TK-A", position=-15, total_traded=675)}
        engine._reconcile_with_kalshi(orders, pos_map)
        out = capsys.readouterr().out
        # Auth fills = max(10, 15) = 15, ledger has 10 → mismatch
        assert "reconcile_fill_mismatch" in out

    def test_resting_mismatch_logged(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Ledger resting disagrees with Kalshi — log warning."""
        engine, _ = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        # Ledger thinks 5 resting, but Kalshi has 10
        ledger.record_resting(Side.A, "ord-a", 5, 45)

        orders = [
            _make_order("TK-A", order_id="ord-a", fill_count=0, remaining_count=10),
        ]
        engine._reconcile_with_kalshi(orders, {})
        out = capsys.readouterr().out
        assert "reconcile_resting_mismatch" in out

    def test_resting_mismatch_skipped_during_optimistic_placement(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """During stale-sync guard, ledger-vs-kalshi resting mismatch is expected."""
        engine, _ = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        # Optimistic placement: ledger has resting, Kalshi doesn't yet
        ledger.record_placement(Side.A, "ord-new", 10, 45)

        orders: list[Order] = []  # Stale — doesn't include the new order
        engine._reconcile_with_kalshi(orders, {})
        out = capsys.readouterr().out
        # Should NOT log resting mismatch (stale-sync guard active)
        assert "reconcile_resting_mismatch" not in out

    def test_both_sides_checked(self) -> None:
        """Overcommit on both sides → two separate errors."""
        engine, _ = _engine_with_pair()
        notes = self._notify_collector(engine)

        orders = [
            _make_order("TK-A", order_id="a1", fill_count=0, remaining_count=10),
            _make_order("TK-A", order_id="a2", fill_count=0, remaining_count=10),
            _make_order("TK-B", order_id="b1", fill_count=0, remaining_count=10, no_price=47),
            _make_order("TK-B", order_id="b2", fill_count=0, remaining_count=10, no_price=47),
        ]
        engine._reconcile_with_kalshi(orders, {})
        errors = [msg for msg, sev in notes if sev == "error"]
        assert len(errors) == 2  # One per side


class TestAutoRebalance:
    @pytest.mark.asyncio
    async def test_check_imbalances_auto_executes(self):
        """check_imbalances detects imbalance and auto-executes catch-up."""
        engine, rest = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 40, 45)
        ledger.record_fill(Side.B, 15, 48)

        # Scanner snapshot needed for price
        engine.scanner._all_snapshots["EVT-1"] = Opportunity(
            event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B",
            no_a=45, no_b=48, qty_a=100, qty_b=100,
            raw_edge=7, fee_edge=0.0, tradeable_qty=100,
            timestamp="2026-03-16T00:00:00Z",
        )

        # Mock for fresh sync in execute_rebalance
        rest.get_all_orders = AsyncMock(return_value=[
            _make_order("TK-A", order_id="oa", fill_count=40, no_price=45, status="canceled"),
            _make_order("TK-B", order_id="ob", fill_count=15, no_price=48, status="canceled"),
        ])
        rest.create_order = AsyncMock(return_value=_make_order("TK-B", order_id="new-b"))
        rest.create_order_group = AsyncMock(return_value="grp-test")

        notifications: list[tuple[str, str]] = []
        engine.on_notification = lambda msg, sev: notifications.append((msg, sev))

        await engine.check_imbalances()

        # Should have auto-placed catch-up, NOT added to proposal queue
        rest.create_order.assert_called_once()
        assert rest.create_order.call_args.kwargs["ticker"] == "TK-B"
        assert rest.create_order.call_args.kwargs["count"] == 25  # full gap
        assert len(engine.proposal_queue.pending()) == 0

    @pytest.mark.asyncio
    async def test_check_imbalances_skips_exit_only(self):
        """Events in exit-only mode are skipped by check_imbalances."""
        engine, rest = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 40, 45)
        ledger.record_fill(Side.B, 15, 48)

        engine._exit_only_events.add("EVT-1")

        rest.create_order = AsyncMock()

        await engine.check_imbalances()

        rest.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_imbalances_double_fire_guard(self):
        """Same event is not rebalanced twice in one check_imbalances call."""
        engine, rest = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 40, 45)
        ledger.record_fill(Side.B, 15, 48)

        engine.scanner._all_snapshots["EVT-1"] = Opportunity(
            event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B",
            no_a=45, no_b=48, qty_a=100, qty_b=100,
            raw_edge=7, fee_edge=0.0, tradeable_qty=100,
            timestamp="2026-03-16T00:00:00Z",
        )

        rest.get_all_orders = AsyncMock(return_value=[
            _make_order("TK-A", order_id="oa", fill_count=40, no_price=45, status="canceled"),
            _make_order("TK-B", order_id="ob", fill_count=15, no_price=48, status="canceled"),
        ])
        rest.create_order = AsyncMock(return_value=_make_order("TK-B", order_id="new-b"))
        rest.create_order_group = AsyncMock(return_value="grp-test")

        await engine.check_imbalances()

        assert rest.create_order.call_count == 1
