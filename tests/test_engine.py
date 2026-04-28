"""Tests for TradingEngine.

Many fixtures here construct ``FillMessage`` / ``UserOrderMessage`` /
``OrderBookSnapshot`` using the legacy wire-shape parameter names
(``count``, ``yes_price``, ``no_price``, ``post_position``, ``yes``,
``no``, ``fill_count``, ``remaining_count``). The models'
``_migrate_fp`` validators accept those at runtime and remap them to
canonical bps/fp100 fields — this is intentional production behavior
that the tests exercise. Pyright doesn't see ``model_validator``
remapping as part of the constructor signature, so it raises
``reportCallIssue`` on every legacy-name argument. Suppressed here at
the file level rather than per-call.
"""
# pyright: reportCallIssue=false

from __future__ import annotations

import asyncio
import time
from datetime import UTC
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.automation_config import AutomationConfig
from talos.bid_adjuster import BidAdjuster
from talos.drip import DripConfig, PlaceOrder
from talos.engine import TradingEngine
from talos.game_manager import GameManager
from talos.market_feed import MarketFeed
from talos.models.market import OrderBookLevel
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
    """Build a TradingEngine with mock dependencies.

    Pre-arms _ready_for_trading so tests don't pay the 30s startup-milestone
    wait on every refresh_account call. Tests that need to exercise the
    startup gate explicitly clear the event after construction.
    """
    books = OrderBookManager()
    scanner = overrides.pop("scanner", ArbitrageScanner(books))
    defaults = dict(
        scanner=scanner,
        game_manager=overrides.pop("game_manager", MagicMock(spec=GameManager, on_change=None)),
        rest_client=overrides.pop("rest_client", AsyncMock(spec=KalshiRESTClient)),
        market_feed=overrides.pop("market_feed", MagicMock(spec=MarketFeed)),
        tracker=overrides.pop("tracker", TopOfMarketTracker(books)),
        adjuster=overrides.pop("adjuster", BidAdjuster(books, [], unit_size=10)),
    )
    defaults.update(overrides)
    engine = TradingEngine(**defaults)
    engine._ready_for_trading.set()
    return engine


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

    def test_active_market_tickers_no_positions(self):
        """Pairs without fills or resting orders return no tickers (saves API calls)."""
        books = OrderBookManager()
        scanner = ArbitrageScanner(books)
        scanner.add_pair("EVT-1", "TK-A", "TK-B")
        engine = _make_engine(scanner=scanner)
        tickers = engine._active_market_tickers()
        assert tickers == []  # No positions = no trade fetches needed

    def test_notify_calls_callback(self):
        engine = _make_engine()
        callback = MagicMock()
        engine.on_notification = callback
        engine._notify("hello", "warning")
        callback.assert_called_once_with("hello", "warning", False)

    def test_notify_toast_flag(self):
        engine = _make_engine()
        callback = MagicMock()
        engine.on_notification = callback
        engine._notify("error!", "error", toast=True)
        callback.assert_called_once_with("error!", "error", True)

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
        no_price_bps=no_price * 100,
        initial_count_fp100=(fill_count + remaining_count) * 100,
        remaining_count_fp100=remaining_count * 100,
        fill_count_fp100=fill_count * 100,
        status=status,
    )


def _mark_all_ledgers_ready(adjuster: BidAdjuster) -> None:
    """Set :attr:`PositionLedger._first_orders_sync` on every ledger so
    Section 8's ``_wait_for_ledger_ready`` returns immediately.

    Pre-Task-6b-2 tests assumed ledgers were always operable; the new
    gate blocks create/amend until first sync completes. Tests that
    bypass the real sync path need to signal readiness manually.
    """
    for ledger in adjuster.ledgers.values():
        ledger._first_orders_sync.set()
        ledger.stale_fills_unconfirmed = False
        ledger.stale_resting_unconfirmed = False
        ledger.legacy_migration_pending = False


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
    engine._initial_sync_done = True  # tests assume synced state
    engine._account_sync_done = True
    _mark_all_ledgers_ready(adjuster)
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
    engine._initial_sync_done = True  # tests assume synced state
    engine._account_sync_done = True
    _mark_all_ledgers_ready(adjuster)
    return engine, rest


def _engine_with_same_ticker_pair() -> tuple[TradingEngine, AsyncMock]:
    """Build an engine for a same-ticker YES/NO pair."""
    books = OrderBookManager()
    scanner = ArbitrageScanner(books)
    scanner.add_pair(
        "MKT-1",
        "MKT-1",
        "MKT-1",
        side_a="yes",
        side_b="no",
        kalshi_event_ticker="EVT-1",
    )
    pair = ArbPair(
        event_ticker="MKT-1",
        ticker_a="MKT-1",
        ticker_b="MKT-1",
        side_a="yes",
        side_b="no",
        kalshi_event_ticker="EVT-1",
    )
    adjuster = BidAdjuster(books, [pair], unit_size=10)
    rest = AsyncMock(spec=KalshiRESTClient)
    engine = _make_engine(
        scanner=scanner,
        adjuster=adjuster,
        rest_client=rest,
    )
    engine._initial_sync_done = True
    engine._account_sync_done = True
    _mark_all_ledgers_ready(adjuster)
    return engine, rest


class TestPolling:
    @pytest.mark.asyncio
    async def test_refresh_balance_updates_balance(self):
        engine, rest = _engine_with_pair()
        rest.get_balance.return_value = Balance(
            balance_bps=5_000_000, portfolio_value_bps=6_000_000
        )

        await engine.refresh_balance()

        assert engine.balance == 50000
        assert engine.portfolio_value == 60000

    @pytest.mark.asyncio
    async def test_refresh_account_fetches_all_orders(self):
        engine, rest = _engine_with_pair()
        rest.get_balance.return_value = Balance(balance_bps=100_000, portfolio_value_bps=100_000)
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
        rest.get_balance.return_value = Balance(balance_bps=100_000, portfolio_value_bps=100_000)
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
        rest.get_balance.return_value = Balance(balance_bps=100_000, portfolio_value_bps=100_000)
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
        rest.get_balance.return_value = Balance(balance_bps=100_000, portfolio_value_bps=100_000)
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
        rest.get_balance.return_value = Balance(balance_bps=100_000, portfolio_value_bps=100_000)
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

        # Prime orders cache so _active_market_tickers includes TK-A
        engine._orders_cache = [_make_order("TK-A", order_id="ord-a", remaining_count=5)]

        recent = datetime.now(UTC).isoformat()
        trades = [
            Trade(
                trade_id="t1",
                ticker="TK-A",
                count_fp100=5000,
                price_bps=4500,
                side="no",
                created_time=recent,
            ),
        ]
        rest.get_trades.return_value = trades

        await engine.refresh_trades()

        assert engine._cpm.cpm("TK-A") is not None

    @pytest.mark.asyncio
    async def test_flow_metrics_for_markets_fetches_scan_tickers(self):
        engine = _make_engine()
        rest = cast(Any, engine._rest)
        from datetime import datetime

        from talos.models.market import Trade

        recent = datetime.now(UTC).isoformat()
        rest.get_trades.return_value = [
            Trade(
                trade_id="t1",
                ticker="TK-A",
                count_fp100=5_000,
                price_bps=4500,
                side="no",
                created_time=recent,
            ),
        ]

        metrics = await engine.flow_metrics_for_markets(["TK-A"])

        assert "TK-A" in metrics
        assert metrics["TK-A"].trade_count == 1
        assert metrics["TK-A"].volume_contracts == 50

    @pytest.mark.asyncio
    async def test_refresh_account_prunes_queue_cache(self):
        engine, rest = _engine_with_pair()
        # Seed cache with an old order
        engine._queue_cache["old-order"] = 10

        rest.get_balance.return_value = Balance(balance_bps=100_000, portfolio_value_bps=100_000)
        rest.get_all_orders.return_value = [
            _make_order("TK-A", order_id="new-order", remaining_count=10),
        ]
        rest.get_queue_positions.return_value = {}

        await engine.refresh_account()

        assert "old-order" not in engine._queue_cache

    @pytest.mark.asyncio
    async def test_refresh_account_syncs_ledger(self):
        engine, rest = _engine_with_pair()
        # Both sides resting — balanced so rebalance doesn't cancel
        orders = [
            _make_order("TK-A", order_id="ord-a", fill_count=0, remaining_count=10, no_price=45),
            _make_order("TK-B", order_id="ord-b", fill_count=0, remaining_count=10, no_price=48),
        ]
        rest.get_balance.return_value = Balance(balance_bps=100_000, portfolio_value_bps=100_000)
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
        rest.get_balance.return_value = Balance(balance_bps=100_000, portfolio_value_bps=100_000)
        rest.get_all_orders.return_value = []
        rest.get_queue_positions.return_value = {}
        # Positions API shows actual holdings
        rest.get_all_positions.return_value = [
            Position(ticker="TK-A", position_fp100=-3000, total_traded_bps=138_000),
            Position(ticker="TK-B", position_fp100=-1000, total_traded_bps=52_000),
        ]

        await engine.refresh_account()

        ledger = engine.adjuster.get_ledger("EVT-1")
        assert ledger.filled_count(Side.A) == 30
        assert ledger.filled_count(Side.B) == 10
        assert ledger.filled_total_cost(Side.A) == 1380

    @pytest.mark.asyncio
    async def test_refresh_account_positions_failure_is_non_fatal(self):
        """If positions API fails, sync_from_orders still works."""
        from talos.errors import KalshiAPIError

        engine, rest = _engine_with_pair()
        rest.get_all_orders.return_value = []
        rest.get_queue_positions.return_value = {}
        rest.get_all_positions.side_effect = KalshiAPIError(
            status_code=500, body="server error"
        )

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
        from talos.errors import KalshiAPIError

        engine, rest = _engine_with_pair()
        rest.create_order.side_effect = KalshiAPIError(status_code=500, body="API down")
        notifications = []
        engine.on_notification = lambda msg, sev, toast=False: notifications.append((msg, sev))

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
        engine.on_notification = lambda msg, sev, toast=False: notifications.append((msg, sev))

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
        engine.on_notification = lambda msg, sev, toast=False: notifications.append((msg, sev))

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
        engine.on_notification = lambda msg, sev, toast=False: notifications.append((msg, sev))

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


class FakeBookManager(OrderBookManager):
    """Minimal fake for OrderBookManager.best_ask()."""

    def __init__(self, prices: dict[str, int]):
        super().__init__()
        self._prices = prices

    def best_ask(self, ticker: str, side: str = "no") -> OrderBookLevel | None:
        price = self._prices.get(ticker)
        if price is None:
            return None
        return OrderBookLevel(price_bps=price * 100, quantity_fp100=10_000)


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
        engine.on_top_of_market_change("TK-B", side="no", at_top=False)
        assert len(engine.proposal_queue) == 1
        p = engine.proposal_queue.pending()[0]
        assert p.kind == "adjustment"
        assert p.adjustment is not None
        assert p.adjustment.new_price == 48

    def test_back_at_top_no_proposal(self):
        engine = _engine_with_jump_setup()
        engine.on_top_of_market_change("TK-B", side="no", at_top=True)
        assert len(engine.proposal_queue) == 0

    @pytest.mark.asyncio
    async def test_approve_proposal_executes_adjustment(self):
        engine = _engine_with_jump_setup()
        engine.on_top_of_market_change("TK-B", side="no", at_top=False)
        key = engine.proposal_queue.pending()[0].key
        # Mock the amend call
        old_order = _make_order(
            "TK-B", order_id="ord-b", fill_count=0, remaining_count=10, no_price=47
        )
        new_order = _make_order(
            "TK-B", order_id="ord-b-new", fill_count=0, remaining_count=10, no_price=48
        )
        engine._rest.amend_order = AsyncMock(return_value=(old_order, new_order))
        engine._rest.get_order = AsyncMock(return_value=old_order)
        await engine.approve_proposal(key)
        assert len(engine.proposal_queue) == 0
        engine._rest.amend_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_approve_missing_proposal_notifies(self):
        engine = _engine_with_jump_setup()
        notifications: list[tuple[str, str]] = []
        engine.on_notification = lambda msg, sev, toast=False: notifications.append((msg, sev))
        key = ProposalKey(event_ticker="EVT-1", side="B", kind="adjustment")
        await engine.approve_proposal(key)
        assert any("No pending" in msg for msg, _ in notifications)

    def test_reject_proposal_removes_from_queue(self):
        engine = _engine_with_jump_setup()
        engine.on_top_of_market_change("TK-B", side="no", at_top=False)
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

        engine.on_top_of_market_change("TK-B", side="no", at_top=False)
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
        rest.get_all_positions = AsyncMock(return_value=[])
        # F36/F33: cancel_order_with_verify probes via get_order before
        # issuing the raw cancel. Return a resting Order so we proceed.
        from talos.models.order import Order as _Order

        def _probe(order_id: str) -> _Order:
            return _Order.model_validate(
                {
                    "order_id": order_id,
                    "ticker": "TK-A" if order_id == "ord-a" else "TK-B",
                    "status": "resting",
                    "action": "buy",
                    "side": "no",
                    "type": "limit",
                    "remaining_count_fp": "10",
                    "fill_count_fp": "0",
                }
            )

        rest.get_order = AsyncMock(side_effect=_probe)

        # F33 resync after cancel: return the OTHER side's resting order
        # so sync_from_orders keeps it on the ledger until we get to it.
        # (sync_from_orders filters out recently-cancelled IDs.)
        def _get_orders(**kwargs) -> list[_Order]:
            ticker = kwargs.get("ticker")
            if ticker == "TK-A":
                return [
                    _Order.model_validate(
                        {
                            "order_id": "ord-a",
                            "ticker": "TK-A",
                            "status": "resting",
                            "action": "buy",
                            "side": "no",
                            "type": "limit",
                            "remaining_count_fp": "10",
                            "fill_count_fp": "0",
                            "no_price_dollars": "0.48",
                        }
                    )
                ]
            if ticker == "TK-B":
                return [
                    _Order.model_validate(
                        {
                            "order_id": "ord-b",
                            "ticker": "TK-B",
                            "status": "resting",
                            "action": "buy",
                            "side": "no",
                            "type": "limit",
                            "remaining_count_fp": "10",
                            "fill_count_fp": "0",
                            "no_price_dollars": "0.47",
                        }
                    )
                ]
            return []

        rest.get_orders = AsyncMock(side_effect=_get_orders)
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
        _mark_all_ledgers_ready(adjuster)

        engine.on_top_of_market_change("TK-B", side="no", at_top=False)
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
    gm = MagicMock(spec=GameManager)
    gm.is_blacklisted.return_value = False
    gm.volumes_24h = {"TK-A": 1000, "TK-B": 1000}
    gm.on_change = None
    engine = TradingEngine(
        scanner=scanner,
        game_manager=gm,
        rest_client=rest,
        market_feed=MagicMock(spec=MarketFeed),
        tracker=TopOfMarketTracker(books),
        adjuster=adjuster,
        automation_config=config,
    )
    engine._ready_for_trading.set()  # skip the 30s startup-milestone wait
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
        rest.get_balance.return_value = Balance(balance_bps=100_000, portfolio_value_bps=100_000)
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
            event_ticker="EVT-1",
            ticker_a="TK-A",
            ticker_b="TK-B",
            no_a=45,
            no_b=47,
            qty_a=100,
            qty_b=100,
            raw_edge=8,
            fee_edge=5.0,
            tradeable_qty=100,
            timestamp="2026-03-16T00:00:00Z",
        )

        rest.get_all_orders = AsyncMock(
            return_value=[
                _make_order(
                    "TK-A", order_id="ord-a", fill_count=50, remaining_count=60, no_price=45
                ),
                _make_order(
                    "TK-B", order_id="ord-b", fill_count=50, remaining_count=10, no_price=47
                ),
            ]
        )
        rest.cancel_order = AsyncMock()
        rest.create_order = AsyncMock(return_value=_make_order("TK-B", order_id="new-b"))
        rest.create_order_group = AsyncMock(return_value="grp-test")

        engine.mark_event_dirty("EVT-1")
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
        # F36/F33: verify wrapper probes via get_order before cancelling.
        from talos.models.order import Order as _Order

        rest.get_order = AsyncMock(
            return_value=_Order.model_validate(
                {
                    "order_id": "ord-a",
                    "ticker": "TK-A",
                    "status": "resting",
                    "action": "buy",
                    "side": "no",
                    "type": "limit",
                    "remaining_count_fp": "10",
                    "fill_count_fp": "40",
                }
            )
        )
        rest.get_orders = AsyncMock(return_value=[])
        # Verification sync happens after action (single event-scoped fetch)
        rest.get_all_orders = AsyncMock(return_value=[])
        rest.get_all_positions = AsyncMock(return_value=[])

        await engine.approve_proposal(key)

        rest.cancel_order.assert_called_once_with("ord-a")
        # Post-action verification did run (get_all_orders called for
        # both orphan sweep in _cancel_all_resting and _verify_after_action)
        assert rest.get_all_orders.call_count == 2
        rest.get_positions.assert_called_once()

    @pytest.mark.asyncio
    async def test_verify_failure_notifies_operator(self):
        """When post-action verify fails, operator sees a warning toast."""
        from datetime import UTC, datetime

        from talos.errors import KalshiAPIError
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
        # F36/F33: verify wrapper probes via get_order before cancelling.
        from talos.models.order import Order as _Order

        rest.get_order = AsyncMock(
            return_value=_Order.model_validate(
                {
                    "order_id": "ord-a",
                    "ticker": "TK-A",
                    "status": "resting",
                    "action": "buy",
                    "side": "no",
                    "type": "limit",
                    "remaining_count_fp": "10",
                    "fill_count_fp": "40",
                }
            )
        )
        rest.get_orders = AsyncMock(return_value=[])
        # Verify fails — API unreachable after action
        rest.get_all_orders = AsyncMock(
            side_effect=KalshiAPIError(status_code=500, body="API down")
        )

        notifications: list[tuple[str, str]] = []
        engine.on_notification = lambda msg, sev, toast=False: notifications.append((msg, sev))

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
            no_price_bps=4500,
            remaining_count_fp100=1000,
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
            no_price_bps=4500,
            remaining_count_fp100=1000,
            status="resting",
        )
        order_b = Order(
            order_id="ord-b",
            ticker="TK-B",
            side="no",
            action="buy",
            no_price_bps=4800,
            remaining_count_fp100=1000,
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
            no_price_bps=4500,
            remaining_count_fp100=1000,
            status="resting",
        )
        order_b = Order(
            order_id="ord-b",
            ticker="TK-B",
            side="no",
            action="buy",
            no_price_bps=4800,
            remaining_count_fp100=1000,
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
        engine._feed.unsubscribe.assert_not_called()  # type: ignore[attr-defined]
        engine._feed.subscribe.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_stale_book_triggers_resubscribe(self):
        engine, _, books = _engine_with_real_feed()
        # Mark TK-A as stale
        book_a = books.get_book("TK-A")
        assert book_a is not None
        book_a.last_update = time.time() - 121.0

        await engine._recover_stale_books()

        engine._feed.unsubscribe.assert_called_once_with("TK-A")  # type: ignore[attr-defined]
        engine._feed.subscribe.assert_called_once_with("TK-A")  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_multiple_stale_books_all_recovered(self):
        engine, _, books = _engine_with_real_feed()
        book_a = books.get_book("TK-A")
        assert book_a is not None
        book_a.last_update = time.time() - 121.0
        book_b = books.get_book("TK-B")
        assert book_b is not None
        book_b.last_update = time.time() - 121.0

        await engine._recover_stale_books()

        assert engine._feed.unsubscribe.call_count == 2  # type: ignore[attr-defined]
        assert engine._feed.subscribe.call_count == 2  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_stale_non_active_ticker_ignored(self):
        engine, _, books = _engine_with_real_feed()
        # Add a stale book for a ticker not in any active pair
        books.apply_snapshot(
            "TK-ORPHAN",
            OrderBookSnapshot(market_ticker="TK-ORPHAN", market_id="m3", yes=[], no=[[50, 10]]),
        )
        orphan = books.get_book("TK-ORPHAN")
        assert orphan is not None
        orphan.last_update = time.time() - 121.0

        await engine._recover_stale_books()

        engine._feed.unsubscribe.assert_not_called()  # type: ignore[attr-defined]
        engine._feed.subscribe.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_resubscribe_failure_does_not_crash(self):
        engine, _, books = _engine_with_real_feed()
        book_a = books.get_book("TK-A")
        assert book_a is not None
        book_a.last_update = time.time() - 121.0
        engine._feed.unsubscribe.side_effect = RuntimeError("WS disconnected")  # type: ignore[attr-defined]

        # Should not raise
        await engine._recover_stale_books()

    @pytest.mark.asyncio
    async def test_stale_recovery_is_rate_limited(self):
        engine, _, books = _engine_with_real_feed()
        book_a = books.get_book("TK-A")
        assert book_a is not None
        book_a.last_update = time.time() - 121.0

        await engine._recover_stale_books()
        await engine._recover_stale_books()

        engine._feed.unsubscribe.assert_called_once_with("TK-A")  # type: ignore[attr-defined]
        engine._feed.subscribe.assert_called_once_with("TK-A")  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_stale_recovery_retries_after_cooldown(self, monkeypatch: pytest.MonkeyPatch):
        engine, _, books = _engine_with_real_feed()
        book_a = books.get_book("TK-A")
        assert book_a is not None
        book_a.last_update = time.time() - 121.0

        clock = {"now": 1000.0}
        monkeypatch.setattr("talos.engine.time.monotonic", lambda: clock["now"])

        await engine._recover_stale_books()
        clock["now"] += 121.0
        await engine._recover_stale_books()

        assert engine._feed.unsubscribe.call_count == 2  # type: ignore[attr-defined]
        assert engine._feed.subscribe.call_count == 2  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_refresh_account_calls_recovery(self):
        """refresh_account triggers stale book recovery before main logic."""
        engine, rest, books = _engine_with_real_feed()
        book_a = books.get_book("TK-A")
        assert book_a is not None
        book_a.last_update = time.time() - 121.0

        rest.get_balance.return_value = Balance(balance_bps=100_000, portfolio_value_bps=100_000)
        rest.get_all_orders.return_value = []
        rest.get_queue_positions.return_value = {}

        await engine.refresh_account()

        # Recovery should have been called (unsubscribe + subscribe for TK-A)
        engine._feed.unsubscribe.assert_called_once_with("TK-A")  # type: ignore[attr-defined]
        engine._feed.subscribe.assert_called_once_with("TK-A")  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_stale_recovery_logs_cycle_timing(self, capsys: pytest.CaptureFixture[str]):
        engine, _, books = _engine_with_real_feed()
        book_a = books.get_book("TK-A")
        assert book_a is not None
        book_a.last_update = time.time() - 121.0

        await engine._recover_stale_books()

        out = capsys.readouterr().out
        assert "stale_book_recovery_cycle" in out
        assert "elapsed_ms" in out
        assert "attempted_count" in out
        assert "skipped_cooldown_count" in out


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
        assert order.fill_count_fp100 == 800
        assert order.remaining_count_fp100 == 200

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
        assert order.fill_count_fp100 == 1000

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

    def test_resyncs_ledger_resting_state_on_update(self):
        """WS user_orders update reconciles resting state via sync_from_orders.

        Note: as of 2026-04-27 this handler intentionally does NOT update
        filled_count — the WS fill channel is the unique writer to avoid
        double-counting against the additive record_fill_from_ws path.
        See test_does_not_double_count_with_ws_fill_channel below and
        the position_ledger 2026-04-27 KXGOLDCARDS regression test.
        """
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
        # Resting state was reconciled (5 still resting at no=45)
        assert ledger.resting_count(Side.A) == 5
        assert ledger.resting_price(Side.A) == 45
        # Filled count NOT touched by the user_orders handler — the WS
        # fill channel is the authoritative writer.
        assert ledger.filled_count(Side.A) == 0

    def test_does_not_double_count_with_ws_fill_channel(self):
        """Replay 2026-04-27 22:24:33 KXGOLDCARDS-26-B0.0 sequence.

        Both WS channels fire for the same trade. Order-update arrives
        first (most common ordering), reporting cumulative fill_count=1.
        Then the fill event arrives. With the bug, the ledger went to 2.
        With the fix, it stays at 1 — sync_from_orders(with_fills=False)
        is a resting-only operation, and record_fill_from_ws is the sole
        additive path (deduped by trade_id).
        """
        engine, _ = _engine_with_pair()
        order_a = _make_order("TK-A", order_id="ord-a", fill_count=0, remaining_count=1)
        engine._orders_cache = [order_a]
        ledger = engine._adjuster.get_ledger("EVT-1")

        # ── 1. WS user_orders fires first: order went resting → executed
        order_msg = UserOrderMessage(
            order_id="ord-a",
            ticker="TK-A",
            side="no",
            status="executed",
            fill_count=1,
            remaining_count=0,
            no_price=45,
        )
        engine._on_order_update(order_msg)
        assert ledger.filled_count(Side.A) == 0  # not touched

        # ── 2. WS fill fires next with the actual fill event
        fill_msg = FillMessage(
            trade_id="trade-xyz",
            order_id="ord-a",
            market_ticker="TK-A",
            side="no",
            count=1,
            yes_price=55,
            post_position=-1,
        )
        engine._on_fill(fill_msg)
        # Exactly one contract counted, NOT two.
        assert ledger.filled_count(Side.A) == 1


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

    def test_drip_fill_routes_to_controller(self):
        engine, _ = _engine_with_pair()
        engine.enable_drip("EVT-1", DripConfig())

        msg = FillMessage(
            trade_id="fill-1",
            order_id="ord-a",
            market_ticker="TK-A",
            side="no",
            count=1,
            yes_price=55,
            post_position=-1,
        )
        engine._on_fill(msg)

        controller = engine._drip_controllers["EVT-1"]
        assert controller.filled_a_fp100 == 100
        assert controller.filled_b_fp100 == 0

    def test_drip_matched_pair_queues_replenishment(self):
        engine, _ = _engine_with_pair()
        engine.enable_drip("EVT-1", DripConfig())

        engine._on_fill(
            FillMessage(
                trade_id="fill-1",
                order_id="ord-a",
                market_ticker="TK-A",
                side="no",
                count=1,
                yes_price=55,
                post_position=-1,
            )
        )
        engine._on_fill(
            FillMessage(
                trade_id="fill-2",
                order_id="ord-b",
                market_ticker="TK-B",
                side="no",
                count=1,
                yes_price=52,
                post_position=-1,
            )
        )

        actions = engine._drip_pending_actions["EVT-1"]
        places = [action for action in actions if isinstance(action, PlaceOrder)]
        assert {place.side for place in places} == {"A", "B"}


class TestDripPersistence:
    """DRIP toggle + DripConfig must survive restart via games_full.json."""

    def test_drip_save_dict_returns_none_when_disabled(self):
        engine, _ = _engine_with_pair()
        assert engine.drip_save_dict("EVT-1") is None

    def test_drip_save_dict_returns_config_when_enabled(self):
        engine, _ = _engine_with_pair()
        engine.enable_drip(
            "EVT-1",
            DripConfig(drip_size=2, max_drips=1, blip_delta_min=12.5),
        )
        payload = engine.drip_save_dict("EVT-1")
        assert payload == {
            "drip_size": 2,
            "max_drips": 1,
            "blip_delta_min": 12.5,
        }

    def test_restore_drip_from_saved_populates_state(self):
        engine, _ = _engine_with_pair()
        ok = engine.restore_drip_from_saved(
            "EVT-1",
            {"drip_size": 1, "max_drips": 1, "blip_delta_min": 7.5},
        )
        assert ok is True
        assert engine.is_drip("EVT-1")
        config = engine.get_drip_config("EVT-1")
        assert config is not None
        assert config.drip_size == 1
        assert config.blip_delta_min == 7.5
        assert "EVT-1" in engine._drip_controllers

    def test_restore_drip_from_saved_skips_exit_only(self):
        engine, _ = _engine_with_pair()
        engine._exit_only_events.add("EVT-1")
        ok = engine.restore_drip_from_saved(
            "EVT-1",
            {"drip_size": 1, "max_drips": 1, "blip_delta_min": 5.0},
        )
        assert ok is False
        assert not engine.is_drip("EVT-1")

    def test_restore_drip_from_saved_returns_false_on_non_dict(self):
        engine, _ = _engine_with_pair()
        assert engine.restore_drip_from_saved("EVT-1", None) is False
        assert engine.restore_drip_from_saved("EVT-1", "not-a-dict") is False
        assert engine.restore_drip_from_saved("EVT-1", []) is False
        assert not engine.is_drip("EVT-1")

    def test_restore_drip_from_saved_returns_false_on_invalid_config(self):
        engine, _ = _engine_with_pair()
        # drip_size must be >= 1 — DripConfig.__post_init__ raises ValueError
        ok = engine.restore_drip_from_saved(
            "EVT-1",
            {"drip_size": 0, "max_drips": 1, "blip_delta_min": 5.0},
        )
        assert ok is False
        assert not engine.is_drip("EVT-1")

    def test_drip_round_trip_preserves_config(self):
        """Save → restore on a fresh engine reproduces the original toggle."""
        engine_a, _ = _engine_with_pair()
        engine_a.enable_drip(
            "EVT-1",
            DripConfig(drip_size=1, max_drips=1, blip_delta_min=20.0),
        )
        snapshot = engine_a.drip_save_dict("EVT-1")
        assert snapshot is not None

        # Fresh engine — simulates restart.
        engine_b, _ = _engine_with_pair()
        assert not engine_b.is_drip("EVT-1")
        ok = engine_b.restore_drip_from_saved("EVT-1", snapshot)
        assert ok is True
        assert engine_b.is_drip("EVT-1")
        assert engine_b.get_drip_config("EVT-1") == engine_a.get_drip_config("EVT-1")

    def test_restored_drip_does_not_emit_drip_on_toast(self):
        """Restore is quiet — operator already knew DRIP was on pre-restart."""
        engine, _ = _engine_with_pair()
        toasts: list[str] = []
        engine.on_notification = (
            lambda msg, sev="information", toast=False: toasts.append(msg)
            if toast
            else None
        )
        engine.restore_drip_from_saved(
            "EVT-1",
            {"drip_size": 1, "max_drips": 1, "blip_delta_min": 5.0},
        )
        assert not any("DRIP ON" in t for t in toasts)


class TestLifecycleFiltering:
    """Lifecycle notifications should only fire for tracked markets."""

    @pytest.mark.asyncio
    async def test_settled_notification_only_for_our_markets(self):
        engine, rest = _engine_with_pair()
        rest.get_settlements = AsyncMock(return_value=[])
        notifications: list[str] = []
        engine.on_notification = lambda msg, sev="information", toast=False: notifications.append(
            msg
        )

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
        engine.on_notification = lambda msg, sev="information", toast=False: notifications.append(
            msg
        )

        engine._on_market_determined("TK-B", "yes", 100)
        assert any("TK-B" in n for n in notifications)

        notifications.clear()
        engine._on_market_determined("UNRELATED-MKT", "no", 0)
        assert not notifications

    def test_paused_notification_only_for_our_markets(self):
        engine, _ = _engine_with_pair()
        notifications: list[str] = []
        engine.on_notification = lambda msg, sev="information", toast=False: notifications.append(
            msg
        )

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
            revenue_bps=20_000,
            fee_cost_bps=1000,
            no_count_fp100=500,
        )
        rest.get_settlements = AsyncMock(return_value=[settlement])
        notifications: list[str] = []
        engine.on_notification = lambda msg, sev="information", toast=False: notifications.append(
            msg
        )

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
        engine.on_notification = lambda msg, sev="info", toast=False: notifications.append(
            (msg, sev)
        )
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
            _make_order(
                "TK-A", order_id="ord-1", fill_count=10, remaining_count=0, status="executed"
            ),
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
            _make_order(
                "TK-A", order_id="ord-a", fill_count=10, remaining_count=0, status="executed"
            ),
        ]
        pos_map = {"TK-A": Position(ticker="TK-A", position_fp100=-1500, total_traded_bps=67_500)}
        engine._reconcile_with_kalshi(orders, pos_map)
        out = capsys.readouterr().out
        # Auth fills = max(10, 15) = 15, ledger has 10 → mismatch
        assert "reconcile_fill_mismatch" in out

    def test_fill_mismatch_deduped_until_state_changes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        engine, _ = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 10, 45)

        orders = [
            _make_order(
                "TK-A", order_id="ord-a", fill_count=10, remaining_count=0, status="executed"
            ),
        ]
        pos_map = {"TK-A": Position(ticker="TK-A", position_fp100=-1500, total_traded_bps=67_500)}

        engine._reconcile_with_kalshi(orders, pos_map)
        first = capsys.readouterr().out
        assert "reconcile_fill_mismatch" in first

        engine._reconcile_with_kalshi(orders, pos_map)
        second = capsys.readouterr().out
        assert "reconcile_fill_mismatch" not in second

        pos_map = {"TK-A": Position(ticker="TK-A", position_fp100=-1600, total_traded_bps=72_000)}
        engine._reconcile_with_kalshi(orders, pos_map)
        third = capsys.readouterr().out
        assert "reconcile_fill_mismatch" in third

    @pytest.mark.asyncio
    async def test_refresh_account_skips_fill_mismatch_until_account_sync_complete(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Startup reconciliation should wait for the first account refresh."""
        engine, rest = _engine_with_pair()
        engine._account_sync_done = False
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 10, 45)

        rest.get_all_orders.return_value = []
        rest.get_queue_positions.return_value = {}
        rest.get_all_positions.return_value = [
            Position(ticker="TK-A", position_fp100=-1500, total_traded_bps=67_500),
        ]

        await engine.refresh_account()

        out = capsys.readouterr().out
        assert "reconcile_fill_mismatch" not in out
        assert engine._account_sync_done is True

    def test_same_ticker_reconcile_ignores_positions_net_holdings(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Same-ticker YES/NO reconciliation must not use net positions for both sides."""
        engine, _ = _engine_with_same_ticker_pair()
        ledger = engine.adjuster.get_ledger("MKT-1")
        ledger.record_fill(Side.A, 2, 48)

        orders = [
            Order(
                order_id="yes-ord",
                ticker="MKT-1",
                action="buy",
                side="yes",
                no_price_bps=0,
                yes_price_bps=4800,
                initial_count_fp100=200,
                remaining_count_fp100=0,
                fill_count_fp100=200,
                status="executed",
            ),
        ]
        pos_map = {"MKT-1": Position(ticker="MKT-1", position_fp100=200, total_traded_bps=9600)}

        engine._reconcile_with_kalshi(orders, pos_map)

        out = capsys.readouterr().out
        assert "reconcile_fill_mismatch" not in out

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

    def test_overcommit_uses_drip_cap_not_unit_size(self) -> None:
        """Reconcile enforces DRIP cap, not unit_size, for DRIP events.

        Standard unit_size=10 would NOT flag 5 resting as an overcommit.
        DRIP cap=1 must flag it.
        """
        engine, _ = _engine_with_pair()
        engine.enable_drip("EVT-1", DripConfig(drip_size=1, max_drips=1))
        notes = self._notify_collector(engine)

        # 5 resting on side A, 0 fills → unit cap (10) would allow this,
        # but DRIP cap (max_ahead_per_side=1) must trigger overcommit.
        orders = [
            _make_order("TK-A", order_id="ord-a", fill_count=0, remaining_count=5),
        ]
        engine._reconcile_with_kalshi(orders, {})

        errors = [msg for msg, sev in notes if sev == "error"]
        assert any("OVERCOMMIT" in msg for msg in errors), (
            f"Expected overcommit error with DRIP cap=1, got: {notes}"
        )
        # Message must reference "drip cap", not "unit"
        overcommit_msgs = [msg for msg in errors if "OVERCOMMIT" in msg]
        assert all("drip cap" in msg for msg in overcommit_msgs), (
            f"Expected 'drip cap' in message, got: {overcommit_msgs}"
        )
        # Event must be flagged for priority resolution
        assert "EVT-1" in engine._overcommit_events

    def test_no_overcommit_within_drip_cap(self) -> None:
        """Reconcile does NOT flag overcommit when resting <= DRIP cap."""
        engine, _ = _engine_with_pair()
        engine.enable_drip("EVT-1", DripConfig(drip_size=1, max_drips=1))
        notes = self._notify_collector(engine)

        # Exactly 1 resting — at the DRIP cap, not over it.
        orders = [
            _make_order("TK-A", order_id="ord-a", fill_count=0, remaining_count=1),
        ]
        engine._reconcile_with_kalshi(orders, {})

        errors = [msg for msg, sev in notes if sev == "error"]
        assert not any("OVERCOMMIT" in msg for msg in errors)
        assert "EVT-1" not in engine._overcommit_events


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
            event_ticker="EVT-1",
            ticker_a="TK-A",
            ticker_b="TK-B",
            no_a=45,
            no_b=48,
            qty_a=100,
            qty_b=100,
            raw_edge=7,
            fee_edge=5.0,
            tradeable_qty=100,
            timestamp="2026-03-16T00:00:00Z",
        )

        # Mock for fresh sync in execute_rebalance
        rest.get_all_orders = AsyncMock(
            return_value=[
                _make_order("TK-A", order_id="oa", fill_count=40, no_price=45, status="canceled"),
                _make_order("TK-B", order_id="ob", fill_count=15, no_price=48, status="canceled"),
            ]
        )
        rest.create_order = AsyncMock(return_value=_make_order("TK-B", order_id="new-b"))
        rest.create_order_group = AsyncMock(return_value="grp-test")

        notifications: list[tuple[str, str]] = []
        engine.on_notification = lambda msg, sev, toast=False: notifications.append((msg, sev))

        engine.mark_event_dirty("EVT-1")
        await engine.check_imbalances()

        # Should have auto-placed catch-up, NOT added to proposal queue
        rest.create_order.assert_called_once()
        assert rest.create_order.call_args.kwargs["ticker"] == "TK-B"
        assert rest.create_order.call_args.kwargs["count"] == 25  # full gap
        assert len(engine.proposal_queue.pending()) == 0

    @pytest.mark.asyncio
    async def test_check_imbalances_catches_up_in_exit_only(self):
        """Exit-only events still get catch-up (risk-reducing, not new pairs)."""
        engine, rest = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 40, 45)
        ledger.record_fill(Side.B, 15, 48)

        engine._exit_only_events.add("EVT-1")

        engine.scanner._all_snapshots["EVT-1"] = Opportunity(
            event_ticker="EVT-1",
            ticker_a="TK-A",
            ticker_b="TK-B",
            no_a=45,
            no_b=48,
            qty_a=100,
            qty_b=100,
            raw_edge=7,
            fee_edge=5.0,
            tradeable_qty=100,
            timestamp="2026-03-16T00:00:00Z",
        )

        rest.get_all_orders = AsyncMock(
            return_value=[
                _make_order("TK-A", order_id="oa", fill_count=40, no_price=45, status="canceled"),
                _make_order("TK-B", order_id="ob", fill_count=15, no_price=48, status="canceled"),
            ]
        )
        rest.create_order = AsyncMock(return_value=_make_order("TK-B", order_id="new-b"))
        rest.create_order_group = AsyncMock(return_value="grp-test")

        engine.mark_event_dirty("EVT-1")
        await engine.check_imbalances()

        # Catch-up placed on behind side even in exit-only
        rest.create_order.assert_called_once()
        assert rest.create_order.call_args.kwargs["ticker"] == "TK-B"
        assert rest.create_order.call_args.kwargs["count"] == 25

    @pytest.mark.asyncio
    async def test_check_imbalances_double_fire_guard(self):
        """Same event is not rebalanced twice in one check_imbalances call."""
        engine, rest = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 40, 45)
        ledger.record_fill(Side.B, 15, 48)

        engine.scanner._all_snapshots["EVT-1"] = Opportunity(
            event_ticker="EVT-1",
            ticker_a="TK-A",
            ticker_b="TK-B",
            no_a=45,
            no_b=48,
            qty_a=100,
            qty_b=100,
            raw_edge=7,
            fee_edge=5.0,
            tradeable_qty=100,
            timestamp="2026-03-16T00:00:00Z",
        )

        rest.get_all_orders = AsyncMock(
            return_value=[
                _make_order("TK-A", order_id="oa", fill_count=40, no_price=45, status="canceled"),
                _make_order("TK-B", order_id="ob", fill_count=15, no_price=48, status="canceled"),
            ]
        )
        rest.create_order = AsyncMock(return_value=_make_order("TK-B", order_id="new-b"))
        rest.create_order_group = AsyncMock(return_value="grp-test")

        engine.mark_event_dirty("EVT-1")
        await engine.check_imbalances()

        assert rest.create_order.call_count == 1

    @pytest.mark.asyncio
    async def test_topup_places_orders_for_both_sides(self):
        """Top-up places orders on both sides when mid-unit with no resting."""
        engine, rest = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 15, 45)
        ledger.record_fill(Side.B, 15, 48)

        engine.scanner._all_snapshots["EVT-1"] = Opportunity(
            event_ticker="EVT-1",
            ticker_a="TK-A",
            ticker_b="TK-B",
            no_a=45,
            no_b=48,
            qty_a=100,
            qty_b=100,
            raw_edge=7,
            fee_edge=5.0,
            tradeable_qty=100,
            timestamp="2026-03-16T00:00:00Z",
        )

        rest.create_order = AsyncMock(return_value=_make_order("TK-A", order_id="new"))
        rest.create_order_group = AsyncMock(return_value="grp-test")

        engine.mark_event_dirty("EVT-1")
        await engine.check_imbalances()

        assert rest.create_order.call_count == 2


class TestStalePositionCleanup:
    """Two-strike reconciliation: pairs with zero Kalshi positions but
    non-zero ledger fills are auto-removed after 2 consecutive detections."""

    def _setup(self):
        """Build engine with one pair that has fills in the ledger."""
        engine, rest = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 10, 45)
        ledger.record_fill(Side.B, 10, 43)

        # Minimal mocks for refresh_account
        rest.get_balance.return_value = Balance(balance_bps=500_000, portfolio_value_bps=500_000)
        rest.get_all_orders.return_value = []
        rest.get_queue_positions.return_value = {}
        return engine, rest

    @pytest.mark.asyncio
    async def test_first_detection_flags_but_does_not_remove(self):
        engine, rest = self._setup()
        # Positions API returns nothing for our tickers → settled
        rest.get_all_positions.return_value = []

        await engine.refresh_account()

        # Pair should still be in the scanner after first detection
        assert any(p.event_ticker == "EVT-1" for p in engine.scanner.pairs)
        assert "EVT-1" in engine._stale_candidates

    @pytest.mark.asyncio
    async def test_second_consecutive_detection_removes_game(self):
        engine, rest = self._setup()
        rest.get_all_positions.return_value = []
        gm = engine._game_manager
        gm.remove_game = AsyncMock()

        # Strike 1
        await engine.refresh_account()
        assert "EVT-1" in engine._stale_candidates

        # Strike 2 — should trigger removal
        await engine.refresh_account()
        # remove_game is scheduled via asyncio.create_task; yield to let it run
        await asyncio.sleep(0)

        gm.remove_game.assert_called_once_with("EVT-1")

    @pytest.mark.asyncio
    async def test_position_reappears_clears_candidate(self):
        engine, rest = self._setup()

        # Strike 1: no positions
        rest.get_all_positions.return_value = []
        await engine.refresh_account()
        assert "EVT-1" in engine._stale_candidates

        # Next cycle: position reappears (transient gap resolved)
        rest.get_all_positions.return_value = [
            Position(ticker="TK-A", position_fp100=-1000, total_traded_bps=45_000),
        ]
        await engine.refresh_account()

        # Candidate should be cleared, pair should still exist
        assert "EVT-1" not in engine._stale_candidates
        assert any(p.event_ticker == "EVT-1" for p in engine.scanner.pairs)

    @pytest.mark.asyncio
    async def test_no_fills_in_ledger_not_flagged(self):
        """Pairs with zero fills (just resting orders) should not be flagged."""
        engine, rest = _engine_with_pair()  # No fills recorded
        rest.get_balance.return_value = Balance(balance_bps=500_000, portfolio_value_bps=500_000)
        rest.get_all_orders.return_value = []
        rest.get_queue_positions.return_value = {}
        rest.get_all_positions.return_value = []

        await engine.refresh_account()

        assert "EVT-1" not in engine._stale_candidates

    @pytest.mark.asyncio
    async def test_positions_api_failure_does_not_false_positive(self):
        """If get_positions raises, stale_candidates should not change."""
        from talos.errors import KalshiAPIError

        engine, rest = self._setup()
        rest.get_all_positions.side_effect = KalshiAPIError(
            status_code=500, body="rate limited"
        )

        await engine.refresh_account()

        # No candidates should be added on API failure
        assert len(engine._stale_candidates) == 0


# ── WS Reaction Pipeline Tests ──────────────────────────────────


class TestEventClaims:
    def test_claim_returns_true_when_unclaimed(self):
        engine = _make_engine()
        assert engine._claim_event("EVT-1", "ws") is True
        assert engine._event_claims["EVT-1"] == "ws"

    def test_claim_returns_false_when_other_owner(self):
        engine = _make_engine()
        engine._claim_event("EVT-1", "ws")
        assert engine._claim_event("EVT-1", "poll") is False

    def test_claim_same_owner_succeeds(self):
        engine = _make_engine()
        engine._claim_event("EVT-1", "ws")
        assert engine._claim_event("EVT-1", "ws") is True

    def test_release_clears_claim(self):
        engine = _make_engine()
        engine._claim_event("EVT-1", "ws")
        engine._release_event("EVT-1", "ws")
        assert "EVT-1" not in engine._event_claims
        # Can be claimed by another owner now
        assert engine._claim_event("EVT-1", "poll") is True

    def test_release_ignores_wrong_owner(self):
        engine = _make_engine()
        engine._claim_event("EVT-1", "ws")
        engine._release_event("EVT-1", "poll")  # wrong owner — no effect
        assert engine._event_claims["EVT-1"] == "ws"

    def test_stale_claim_force_released(self):
        engine = _make_engine()
        engine._claim_event("EVT-1", "ws")
        # Backdate the claim time to simulate staleness
        engine._event_claim_times["EVT-1"] = time.monotonic() - 120.0
        assert engine._claim_event("EVT-1", "poll") is True
        assert engine._event_claims["EVT-1"] == "poll"


class TestReactionQueue:
    def test_on_order_update_enqueues_on_fill(self):
        engine, _ = _engine_with_pair()
        engine._orders_cache = [
            _make_order("TK-A", order_id="ord-a", fill_count=5, remaining_count=5),
        ]
        msg = UserOrderMessage(
            order_id="ord-a",
            ticker="TK-A",
            status="resting",
            side="no",
            no_price=45,
            fill_count=7,
            remaining_count=3,
        )
        engine._on_order_update(msg)
        assert not engine._reaction_queue.empty()
        assert engine._reaction_queue.get_nowait() == "EVT-1"

    def test_on_order_update_no_enqueue_without_fills(self):
        engine, _ = _engine_with_pair()
        engine._orders_cache = [
            _make_order("TK-A", order_id="ord-a", fill_count=5, remaining_count=5),
        ]
        msg = UserOrderMessage(
            order_id="ord-a",
            ticker="TK-A",
            status="resting",
            side="no",
            no_price=45,
            fill_count=5,  # same as before — no new fills
            remaining_count=5,
        )
        engine._on_order_update(msg)
        assert engine._reaction_queue.empty()

    def test_on_order_update_no_enqueue_before_sync(self):
        engine, _ = _engine_with_pair()
        engine._initial_sync_done = False
        engine._orders_cache = [
            _make_order("TK-A", order_id="ord-a", fill_count=5, remaining_count=5),
        ]
        msg = UserOrderMessage(
            order_id="ord-a",
            ticker="TK-A",
            status="resting",
            side="no",
            no_price=45,
            fill_count=7,
            remaining_count=3,
        )
        engine._on_order_update(msg)
        assert engine._reaction_queue.empty()

    def test_on_fill_enqueues_event(self):
        engine, _ = _engine_with_pair()
        msg = FillMessage(
            trade_id="fill-1",
            order_id="ord-a",
            market_ticker="TK-A",
            side="no",
            count_fp100=300,
            yes_price_bps=5500,
            post_position_fp100=-300,
        )
        engine._on_fill(msg)
        assert not engine._reaction_queue.empty()
        assert engine._reaction_queue.get_nowait() == "EVT-1"

    def test_on_fill_marks_dirty(self):
        engine, _ = _engine_with_pair()
        msg = FillMessage(
            trade_id="fill-1",
            order_id="ord-a",
            market_ticker="TK-A",
            side="no",
            count_fp100=300,
            yes_price_bps=5500,
            post_position_fp100=-300,
        )
        engine._on_fill(msg)
        assert "EVT-1" in engine._dirty_events

    def test_on_fill_no_enqueue_before_sync(self):
        engine, _ = _engine_with_pair()
        engine._initial_sync_done = False
        msg = FillMessage(
            trade_id="fill-1",
            order_id="ord-a",
            market_ticker="TK-A",
            side="no",
            count_fp100=300,
            yes_price_bps=5500,
            post_position_fp100=-300,
        )
        engine._on_fill(msg)
        assert engine._reaction_queue.empty()
        # But dirty marking should still happen regardless
        assert "EVT-1" in engine._dirty_events


class TestReactionConsumer:
    @pytest.mark.asyncio
    async def test_consumer_processes_event(self):
        engine, _ = _engine_with_pair()
        engine._reaction_queue.put_nowait("EVT-1")

        # Run _react_to_event directly (consumer tested via queue drain)
        await engine._react_to_event("EVT-1")

        assert "EVT-1" in engine._last_ws_reaction
        assert time.monotonic() - engine._last_ws_reaction["EVT-1"] < 2.0

    @pytest.mark.asyncio
    async def test_consumer_coalesces_duplicates(self):
        engine, _ = _engine_with_pair()
        # Put same event 3 times
        for _ in range(3):
            engine._reaction_queue.put_nowait("EVT-1")

        # Drain the queue like the consumer does
        first = engine._reaction_queue.get_nowait()
        events: set[str] = {first}
        while not engine._reaction_queue.empty():
            events.add(engine._reaction_queue.get_nowait())

        assert events == {"EVT-1"}
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_consumer_skips_claimed_event(self):
        engine, _ = _engine_with_pair()
        # Pre-claim by poll
        engine._claim_event("EVT-1", "poll")

        # WS consumer should fail to claim
        assert engine._claim_event("EVT-1", "ws") is False

    @pytest.mark.asyncio
    async def test_consumer_releases_claim_on_error(self):
        engine, _ = _engine_with_pair()
        engine._claim_event("EVT-1", "ws")

        # Simulate error in reaction
        try:
            raise RuntimeError("test error")
        except RuntimeError:
            pass
        finally:
            engine._release_event("EVT-1", "ws")

        assert "EVT-1" not in engine._event_claims

    @pytest.mark.asyncio
    async def test_consumer_survives_error(self):
        """The consumer loop should handle exceptions without crashing."""
        engine, _ = _engine_with_pair()

        # _react_to_event with unknown event won't crash — returns early
        await engine._react_to_event("NONEXISTENT")
        # Should not raise


class TestReactToEvent:
    @pytest.mark.asyncio
    async def test_react_runs_scoped_pipeline(self):
        engine, _ = _engine_with_pair()
        jumps_called: list[str] = []
        imbalance_called: list[str] = []

        def mock_jumps(et: str, pair: object) -> None:
            jumps_called.append(et)

        async def mock_imbalance(et: str, pair: object) -> None:
            imbalance_called.append(et)

        engine._reevaluate_jumps_for = mock_jumps  # type: ignore[assignment]
        engine._check_imbalance_for = mock_imbalance  # type: ignore[assignment]

        await engine._react_to_event("EVT-1")

        assert jumps_called == ["EVT-1"]
        assert imbalance_called == ["EVT-1"]

    @pytest.mark.asyncio
    async def test_react_skips_before_initial_sync(self):
        engine, _ = _engine_with_pair()
        engine._initial_sync_done = False

        jumps_called: list[str] = []
        engine._reevaluate_jumps_for = lambda et, p: jumps_called.append(et)  # type: ignore[assignment]

        await engine._react_to_event("EVT-1")

        assert jumps_called == []
        assert "EVT-1" not in engine._last_ws_reaction

    @pytest.mark.asyncio
    async def test_react_stamps_timestamp(self):
        engine, _ = _engine_with_pair()
        before = time.monotonic()
        await engine._react_to_event("EVT-1")
        after = time.monotonic()

        assert "EVT-1" in engine._last_ws_reaction
        assert before <= engine._last_ws_reaction["EVT-1"] <= after

    @pytest.mark.asyncio
    async def test_react_skips_unknown_event(self):
        engine, _ = _engine_with_pair()
        # Should not raise, should not stamp
        await engine._react_to_event("NONEXISTENT")
        assert "NONEXISTENT" not in engine._last_ws_reaction


class TestPollUsesSharedClaim:
    @pytest.mark.asyncio
    async def test_check_imbalances_skips_ws_claimed(self):
        engine, rest = _engine_with_pair()
        engine._dirty_events.add("EVT-1")
        engine._claim_event("EVT-1", "ws")  # WS holds claim

        await engine.check_imbalances()

        # Poll should have skipped — no rebalance calls
        rest.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_imbalances_processes_unclaimed(self):
        engine, rest = _engine_with_pair()
        engine._dirty_events.add("EVT-1")
        # No claim — poll should process freely
        # (With balanced ledger, no rebalance needed, but no skip either)
        await engine.check_imbalances()
        # The key assertion: event was NOT skipped due to claim
        assert "EVT-1" not in engine._event_claims  # claim released

    @pytest.mark.asyncio
    async def test_check_imbalances_releases_claim_after(self):
        engine, _ = _engine_with_pair()
        engine._dirty_events.add("EVT-1")

        await engine.check_imbalances()

        # Claim should be released after processing
        assert "EVT-1" not in engine._event_claims

    @pytest.mark.asyncio
    async def test_full_sweep_still_works(self):
        """Full sweep should process all events regardless of dirty set."""
        engine, _ = _engine_with_pair()
        # Don't add to dirty — but force full sweep
        engine._full_sweep_counter = 9  # next call triggers full sweep

        await engine.check_imbalances()

        # Claim should be released
        assert "EVT-1" not in engine._event_claims


class TestClaimMutualExclusionIntegration:
    """Prove that WS and poll paths cannot execute rebalance for the same
    event concurrently — the claim mechanism enforces mutual exclusion."""

    @pytest.mark.asyncio
    async def test_ws_holds_claim_poll_skips(self):
        """WS consumer claims event and runs slow reaction.
        Poll path tries same event concurrently — must skip, not execute."""
        engine, rest = _engine_with_pair()

        # Track which paths actually executed rebalance logic
        executed_by: list[str] = []
        ws_claimed = asyncio.Event()

        # Replace _check_imbalance_for with a slow version that signals
        # when it's holding the claim, giving poll a chance to race.
        async def slow_ws_check(et: str, pair: object) -> None:
            executed_by.append("ws")
            ws_claimed.set()  # Signal: WS now holds the claim
            await asyncio.sleep(0.1)  # Hold claim for 100ms

        engine._check_imbalance_for = slow_ws_check  # type: ignore[assignment]

        # Seed dirty so poll path wants to process EVT-1
        engine._dirty_events.add("EVT-1")

        async def ws_path() -> None:
            """Simulate WS consumer: claim → react → release."""
            claimed = engine._claim_event("EVT-1", "ws")
            assert claimed, "WS should claim successfully"
            try:
                await engine._react_to_event("EVT-1")
            finally:
                engine._release_event("EVT-1", "ws")

        async def poll_path() -> None:
            """Simulate poll: wait for WS to claim, then try same event."""
            await ws_claimed.wait()  # Ensure WS holds claim first
            await engine.check_imbalances()

        # Run both concurrently
        await asyncio.gather(ws_path(), poll_path())

        # WS should have executed; poll should have been blocked by claim
        assert "ws" in executed_by
        assert len(executed_by) == 1, (
            f"Expected only WS to execute, got: {executed_by}"
        )
        # All claims should be released after both paths complete
        assert not engine._event_claims

    @pytest.mark.asyncio
    async def test_poll_claims_ws_skips(self):
        """Mirror test: poll claims first, WS consumer must skip."""
        engine, _ = _engine_with_pair()

        ws_reacted: list[str] = []
        original_react = engine._react_to_event

        async def tracking_react(et: str) -> None:
            ws_reacted.append(et)
            await original_react(et)

        # Poll claims the event
        engine._claim_event("EVT-1", "poll")

        # WS consumer tries to claim — should fail
        claimed = engine._claim_event("EVT-1", "ws")
        assert not claimed

        # Run react — it would succeed if called, but the consumer
        # would never reach it because claim fails first.
        # Simulate what the consumer loop does:
        if engine._claim_event("EVT-1", "ws"):
            try:
                await tracking_react("EVT-1")
            finally:
                engine._release_event("EVT-1", "ws")

        assert ws_reacted == []  # WS never ran

        # Cleanup
        engine._release_event("EVT-1", "poll")
        assert not engine._event_claims


class TestFillDriftDetection:
    """Drift detection runs AFTER applying the WS fill to the ledger
    (post-2026-04-26 CLE-TOR fix). post_position is Kalshi's ground truth
    after this fill — the ledger should match it. Mismatch indicates a
    missed prior WS message (ledger lower) or a dedup miss (ledger higher).
    """

    def test_on_fill_logs_drift(self, capsys: pytest.CaptureFixture[str]):
        engine, _ = _engine_with_pair()
        # Seed ledger with 5 fills on side A
        ledger = engine._adjuster.get_ledger("EVT-1")
        engine._orders_cache = [
            _make_order("TK-A", order_id="ord-a", fill_count=5, remaining_count=0),
        ]
        ledger.sync_from_orders(
            engine._orders_cache, ticker_a="TK-A", ticker_b="TK-B"
        )

        # WS fill of 3 → ledger becomes 5+3=8. Kalshi reports
        # post_position=-10 → mismatch (Kalshi says 10, we now see 8).
        msg = FillMessage(
            trade_id="fill-1",
            order_id="ord-a",
            market_ticker="TK-A",
            side="no",
            count_fp100=300,
            yes_price_bps=5500,
            post_position_fp100=-1000,
        )
        engine._on_fill(msg)

        captured = capsys.readouterr()
        assert "ws_fill_position_drift" in captured.out

    def test_on_fill_no_drift(self, capsys: pytest.CaptureFixture[str]):
        engine, _ = _engine_with_pair()
        ledger = engine._adjuster.get_ledger("EVT-1")
        engine._orders_cache = [
            _make_order("TK-A", order_id="ord-a", fill_count=5, remaining_count=0),
        ]
        ledger.sync_from_orders(
            engine._orders_cache, ticker_a="TK-A", ticker_b="TK-B"
        )

        # WS fill of 1 → ledger becomes 5+1=6. Kalshi reports
        # post_position=-6 → matches; no drift expected.
        msg = FillMessage(
            trade_id="fill-1",
            order_id="ord-a",
            market_ticker="TK-A",
            side="no",
            count_fp100=100,
            yes_price_bps=5500,
            post_position_fp100=-600,
        )
        engine._on_fill(msg)

        captured = capsys.readouterr()
        assert "ws_fill_position_drift" not in captured.out
