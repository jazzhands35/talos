"""Tests for TradingEngine."""

from __future__ import annotations

from datetime import UTC
from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.bid_adjuster import BidAdjuster
from talos.engine import TradingEngine
from talos.game_manager import GameManager
from talos.market_feed import MarketFeed
from talos.models.order import Order
from talos.models.portfolio import Balance
from talos.models.proposal import ProposalKey
from talos.models.strategy import ArbPair
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


class TestPolling:
    @pytest.mark.asyncio
    async def test_refresh_account_updates_balance(self):
        engine, rest = _engine_with_pair()
        rest.get_balance.return_value = Balance(balance=50000, portfolio_value=60000)
        rest.get_orders.return_value = []
        rest.get_queue_positions.return_value = {}

        await engine.refresh_account()

        assert engine.balance == 50000
        assert engine.portfolio_value == 60000

    @pytest.mark.asyncio
    async def test_refresh_account_stores_orders(self):
        engine, rest = _engine_with_pair()
        orders = [
            _make_order("TK-A", order_id="ord-a", fill_count=5, remaining_count=5),
        ]
        rest.get_balance.return_value = Balance(balance=1000, portfolio_value=1000)
        rest.get_orders.return_value = orders
        rest.get_queue_positions.return_value = {}

        await engine.refresh_account()

        assert len(engine.orders) == 1
        assert engine.orders[0].order_id == "ord-a"

    @pytest.mark.asyncio
    async def test_refresh_account_computes_position_summaries(self):
        engine, rest = _engine_with_pair()
        # Pre-seed ledger so sync_from_orders sees consistency (no discrepancy)
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
        rest.get_orders.return_value = orders
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
        rest.get_orders.return_value = orders
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
        rest.get_orders.return_value = orders
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
        rest.get_orders.return_value = [
            _make_order("TK-A", order_id="new-order", remaining_count=10),
        ]
        rest.get_queue_positions.return_value = {}

        await engine.refresh_account()

        assert "old-order" not in engine._queue_cache

    @pytest.mark.asyncio
    async def test_refresh_account_syncs_ledger(self):
        engine, rest = _engine_with_pair()
        # Use resting-only orders — empty ledger matches (no fills = no discrepancy)
        orders = [
            _make_order("TK-A", order_id="ord-a", fill_count=0, remaining_count=10, no_price=45),
        ]
        rest.get_balance.return_value = Balance(balance=1000, portfolio_value=1000)
        rest.get_orders.return_value = orders
        rest.get_queue_positions.return_value = {}

        await engine.refresh_account()

        ledger = engine.adjuster.get_ledger("EVT-1")
        # sync_from_orders updates resting state from Kalshi when consistent
        assert ledger.resting_order_id(Side.A) == "ord-a"
        assert ledger.resting_count(Side.A) == 10


class TestActions:
    @pytest.mark.asyncio
    async def test_place_bids_calls_create_order_twice(self):
        engine, rest = _engine_with_pair()
        rest.create_order.return_value = _make_order("TK-A", order_id="new-1")

        from talos.models.strategy import BidConfirmation
        bid = BidConfirmation(
            ticker_a="TK-A", ticker_b="TK-B", no_a=45, no_b=47, qty=10
        )
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
        bid = BidConfirmation(
            ticker_a="TK-A", ticker_b="TK-B", no_a=45, no_b=47, qty=10
        )
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
    async def test_approve_adjustment_no_proposal(self):
        engine, rest = _engine_with_pair()
        notifications = []
        engine.on_notification = lambda msg, sev: notifications.append((msg, sev))

        await engine.approve_adjustment("EVT-1", "A")

        assert any("No pending proposal" in msg for msg, _ in notifications)

    def test_reject_adjustment_clears_proposal(self):
        engine, _ = _engine_with_pair()
        # Inject a fake proposal
        from talos.models.adjustment import ProposedAdjustment
        proposal = ProposedAdjustment(
            event_ticker="EVT-1", side="A", action="follow_jump",
            cancel_order_id="ord-1", cancel_count=10, cancel_price=45,
            new_count=10, new_price=48,
            reason="test", position_before="", position_after="", safety_check="",
        )
        engine.adjuster._proposals.setdefault("EVT-1", {})[Side.A] = proposal
        assert engine.adjuster.has_pending_proposal("EVT-1", Side.A)

        # Also add to the proposal queue so reject_adjustment (which now delegates
        # to reject_proposal) can find it
        from datetime import datetime

        from talos.models.proposal import Proposal

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

        engine.reject_adjustment("EVT-1", "A")

        assert not engine.adjuster.has_pending_proposal("EVT-1", Side.A)


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
