"""Tests for TradingEngine."""

from __future__ import annotations

from datetime import UTC
from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.automation_config import AutomationConfig
from talos.bid_adjuster import BidAdjuster
from talos.engine import TradingEngine
from talos.game_manager import GameManager
from talos.market_feed import MarketFeed
from talos.models.order import Order
from talos.models.portfolio import Balance, Position
from talos.models.proposal import ProposalKey
from talos.models.strategy import ArbPair
from talos.models.ws import OrderBookSnapshot
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

    @pytest.mark.asyncio
    async def test_refresh_account_augments_fills_from_positions(self):
        """Fills from archived orders are recovered via positions API."""
        engine, rest = _engine_with_pair()
        # Orders API returns nothing (orders archived)
        rest.get_balance.return_value = Balance(balance=1000, portfolio_value=1000)
        rest.get_orders.return_value = []
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
        rest.get_balance.return_value = Balance(balance=1000, portfolio_value=1000)
        rest.get_orders.return_value = []
        rest.get_queue_positions.return_value = {}
        rest.get_positions.side_effect = RuntimeError("API error")

        # Should not raise — positions failure is logged, not fatal
        await engine.refresh_account()
        assert engine.balance == 1000


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
        """When resting order already exists, place_bids blocks (one resting per side)."""
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
    async def test_ledger_not_updated_optimistically(self):
        """Ledger is NOT updated after placement — Kalshi is source of truth."""
        engine, rest = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")

        rest.create_order.return_value = _make_order(
            "TK-A", order_id="new-a", remaining_count=10, no_price=45
        )

        from talos.models.strategy import BidConfirmation

        bid = BidConfirmation(ticker_a="TK-A", ticker_b="TK-B", no_a=45, no_b=47, qty=10)
        await engine.place_bids(bid)

        # Ledger should NOT have resting orders — sync_from_orders handles that
        assert ledger.resting_order_id(Side.A) is None
        assert ledger.resting_order_id(Side.B) is None


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
        rest.get_orders.return_value = []
        rest.get_queue_positions.return_value = {}

        await engine.refresh_account()

        # Should have created a bid proposal during the polling cycle
        assert len(engine.proposal_queue) == 1
        assert engine.proposal_queue.pending()[0].kind == "bid"


# ── Imbalance detection ────────────────────────────────────────────


class TestCheckImbalances:
    def test_no_imbalance_no_proposal(self):
        """Balanced positions produce no rebalance proposal."""
        engine, _ = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_resting(Side.A, "ord-a", 10, 45)
        ledger.record_resting(Side.B, "ord-b", 10, 47)

        engine.check_imbalances()
        assert len(engine.proposal_queue) == 0

    def test_imbalance_within_unit_no_proposal(self):
        """Delta < unit_size is tolerated (normal fill asymmetry)."""
        engine, _ = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 10, 45)
        ledger.record_resting(Side.A, "ord-a", 5, 45)
        # Side B: 10 filled, no resting — delta = 5 < unit_size
        ledger.record_fill(Side.B, 10, 47)

        engine.check_imbalances()
        assert len(engine.proposal_queue) == 0

    def test_imbalance_at_exactly_unit_size_proposes(self):
        """Delta == unit_size is flagged (a full unit of imbalance)."""
        engine, _ = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 10, 45)
        ledger.record_resting(Side.A, "ord-a", 10, 45)
        # Side B: 10 filled, no resting — delta = 10 = unit_size
        ledger.record_fill(Side.B, 10, 47)

        engine.check_imbalances()
        assert len(engine.proposal_queue) == 1
        p = engine.proposal_queue.pending()[0]
        assert p.kind == "rebalance"

    def test_imbalance_exceeds_unit_proposes_rebalance(self):
        """Delta > unit_size produces a rebalance proposal."""
        engine, _ = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        # Side A: 50 filled + 60 resting = 110 committed
        ledger.record_fill(Side.A, 50, 45)
        ledger.record_resting(Side.A, "ord-a", 60, 45)
        # Side B: 50 filled + 10 resting = 60 committed
        ledger.record_fill(Side.B, 50, 47)
        ledger.record_resting(Side.B, "ord-b", 10, 47)

        engine.check_imbalances()

        assert len(engine.proposal_queue) == 1
        p = engine.proposal_queue.pending()[0]
        assert p.kind == "rebalance"
        assert p.key.side == "A"  # over-extended side
        assert "Reduce" in p.detail
        assert "110" in p.detail  # committed_A
        assert "60" in p.detail  # committed_B

    def test_no_duplicate_rebalance_proposals(self):
        """Second check doesn't add another rebalance for the same side."""
        engine, _ = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 50, 45)
        ledger.record_resting(Side.A, "ord-a", 60, 45)
        ledger.record_fill(Side.B, 50, 47)
        ledger.record_resting(Side.B, "ord-b", 10, 47)

        engine.check_imbalances()
        engine.check_imbalances()

        assert len(engine.proposal_queue) == 1

    def test_fill_imbalance_no_books_manual_fallback(self):
        """Without book data, fill imbalance falls back to manual action."""
        engine, _ = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        # Side A: 30 filled, 0 resting = 30 committed
        ledger.record_fill(Side.A, 30, 45)
        # Side B: 10 filled, 0 resting = 10 committed
        ledger.record_fill(Side.B, 10, 47)

        engine.check_imbalances()

        assert len(engine.proposal_queue) == 1
        p = engine.proposal_queue.pending()[0]
        assert p.kind == "rebalance"
        assert p.key.side == "A"  # over side
        assert p.rebalance is None  # no executable step — manual fallback

    def test_fill_imbalance_with_books_proposes_catchup(self):
        """With book data, fill imbalance proposes catch-up bid on under-side."""
        engine, _ = _engine_with_pair_and_books()
        ledger = engine.adjuster.get_ledger("EVT-1")
        # Side A: 30 filled, 0 resting = 30 committed
        ledger.record_fill(Side.A, 30, 45)
        # Side B: 20 filled, 0 resting = 20 committed
        ledger.record_fill(Side.B, 20, 48)

        engine.check_imbalances()

        assert len(engine.proposal_queue) == 1
        p = engine.proposal_queue.pending()[0]
        assert p.kind == "rebalance"
        assert p.rebalance is not None
        # No step 1 (no resting to cancel)
        assert p.rebalance.order_id is None
        # Step 2: catch-up 10 on B at current book price
        assert p.rebalance.catchup_ticker == "TK-B"
        assert p.rebalance.catchup_qty == 10
        assert p.rebalance.catchup_price == 48

    def test_two_step_cancel_then_catchup(self):
        """30f+10r / 20f → cancel A resting, then catch up on B."""
        engine, _ = _engine_with_pair_and_books()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 30, 45)
        ledger.record_resting(Side.A, "ord-a", 10, 45)
        ledger.record_fill(Side.B, 20, 48)

        engine.check_imbalances()

        p = engine.proposal_queue.pending()[0]
        assert p.rebalance is not None
        # Step 1: cancel all resting on A
        assert p.rebalance.order_id == "ord-a"
        assert p.rebalance.current_resting == 10
        assert p.rebalance.target_resting == 0
        # Step 2: catch-up 10 on B
        assert p.rebalance.catchup_ticker == "TK-B"
        assert p.rebalance.catchup_qty == 10
        assert p.rebalance.catchup_price == 48
        assert "Cancel" in p.detail
        assert "Place 10" in p.detail

    def test_reduce_only_when_under_has_resting(self):
        """If under-side already has resting, only reduce over-side."""
        engine, _ = _engine_with_pair_and_books()
        ledger = engine.adjuster.get_ledger("EVT-1")
        # A: 40f + 10r = 50, B: 20f + 10r = 30, delta = 20
        ledger.record_fill(Side.A, 40, 45)
        ledger.record_resting(Side.A, "ord-a", 10, 45)
        ledger.record_fill(Side.B, 20, 48)
        ledger.record_resting(Side.B, "ord-b", 10, 48)

        engine.check_imbalances()

        p = engine.proposal_queue.pending()[0]
        assert p.rebalance is not None
        # Step 1: reduce A resting to 0 (target = max(40, 30) = 40, need 0 resting)
        assert p.rebalance.order_id == "ord-a"
        assert p.rebalance.target_resting == 0
        # Step 2: no catch-up (B already has 10 resting)
        assert p.rebalance.catchup_qty == 0

    def test_partial_reduce_when_under_committed_exceeds_over_filled(self):
        """Reduce over resting partially when under-committed > over-filled."""
        engine, _ = _engine_with_pair_and_books()
        ledger = engine.adjuster.get_ledger("EVT-1")
        # A: 30f + 20r = 50, B: 40f + 0r = 40, delta = 10
        ledger.record_fill(Side.A, 30, 45)
        ledger.record_resting(Side.A, "ord-a", 20, 45)
        ledger.record_fill(Side.B, 40, 48)

        engine.check_imbalances()

        p = engine.proposal_queue.pending()[0]
        assert p.rebalance is not None
        # target = max(30, 40) = 40, target_resting = 40 - 30 = 10
        assert p.rebalance.target_resting == 10
        assert p.rebalance.current_resting == 20
        # No catch-up needed (B committed = 40 = target)
        assert p.rebalance.catchup_qty == 0

    def test_catchup_capped_at_unit_size(self):
        """Catch-up quantity is capped at one unit even if gap is larger."""
        engine, _ = _engine_with_pair_and_books()
        ledger = engine.adjuster.get_ledger("EVT-1")
        # A: 50f, B: 20f → gap = 30 but catchup capped at 10
        ledger.record_fill(Side.A, 50, 45)
        ledger.record_fill(Side.B, 20, 48)

        engine.check_imbalances()

        p = engine.proposal_queue.pending()[0]
        assert p.rebalance is not None
        assert p.rebalance.catchup_qty == 10  # capped at unit_size

    @pytest.mark.asyncio
    async def test_execute_rebalance_cancel_and_catchup(self):
        """Executing two-step rebalance cancels first, then places catch-up."""
        engine, rest = _engine_with_pair_and_books()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 30, 45)
        ledger.record_resting(Side.A, "ord-a", 10, 45)
        ledger.record_fill(Side.B, 20, 48)

        engine.check_imbalances()
        key = engine.proposal_queue.pending()[0].key

        rest.cancel_order = AsyncMock()
        rest.create_order = AsyncMock(return_value=_make_order("TK-B", order_id="new-b"))
        # Fresh sync before catch-up — return orders maintaining imbalance
        rest.get_orders = AsyncMock(
            return_value=[
                _make_order(
                    "TK-A",
                    order_id="ord-a-done",
                    fill_count=30,
                    no_price=45,
                    status="canceled",
                ),
                _make_order(
                    "TK-B",
                    order_id="ord-b-done",
                    fill_count=20,
                    no_price=48,
                    status="canceled",
                ),
            ]
        )
        await engine.approve_proposal(key)

        rest.cancel_order.assert_called_once_with("ord-a")
        rest.create_order.assert_called_once_with(
            ticker="TK-B",
            action="buy",
            side="no",
            no_price=48,
            count=10,
        )

    @pytest.mark.asyncio
    async def test_execute_rebalance_amend_passes_price(self):
        """Partial reduce uses amend_order with the resting price."""
        engine, rest = _engine_with_pair_and_books()
        ledger = engine.adjuster.get_ledger("EVT-1")
        # A: 30f + 20r @ 45c = 50, B: 40f = 40, delta = 10 → reduce A to 10 resting
        ledger.record_fill(Side.A, 30, 45)
        ledger.record_resting(Side.A, "ord-a", 20, 45)
        ledger.record_fill(Side.B, 40, 48)

        engine.check_imbalances()
        key = engine.proposal_queue.pending()[0].key

        rest.amend_order = AsyncMock(
            return_value=(
                _make_order("TK-A", order_id="ord-a"),
                _make_order("TK-A", order_id="ord-a-amended"),
            )
        )
        await engine.approve_proposal(key)

        rest.amend_order.assert_called_once_with(
            "ord-a",
            ticker="TK-A",
            no_price=45,
            count=40,  # 30 filled + 10 target resting
        )

    @pytest.mark.asyncio
    async def test_execute_rebalance_amend_no_op_is_not_error(self):
        """AMEND_ORDER_NO_OP is treated as success, not error."""
        from talos.errors import KalshiAPIError

        engine, rest = _engine_with_pair_and_books()
        ledger = engine.adjuster.get_ledger("EVT-1")
        # A: 20f + 30r @ 45c = 50, B: 25f = 25, delta = 25
        # target = max(20, 25) = 25, target_over_resting = 5 → partial amend
        ledger.record_fill(Side.A, 20, 45)
        ledger.record_resting(Side.A, "ord-a", 30, 45)
        ledger.record_fill(Side.B, 25, 48)

        engine.check_imbalances()
        key = engine.proposal_queue.pending()[0].key

        # Amend returns AMEND_ORDER_NO_OP (order already at target)
        rest.amend_order = AsyncMock(
            side_effect=KalshiAPIError(
                400,
                {
                    "error": {
                        "code": "AMEND_ORDER_NO_OP",
                        "message": "AMEND_ORDER_NO_OP",
                    }
                },
            )
        )
        notifications: list[tuple[str, str]] = []
        engine.on_notification = lambda msg, sev: notifications.append((msg, sev))

        await engine.approve_proposal(key)

        # No-op treated as success — info notification, not error
        assert any("no-op" in msg.lower() for msg, _ in notifications)
        assert not any(sev == "error" for _, sev in notifications)

    @pytest.mark.asyncio
    async def test_execute_rebalance_catchup_blocked_by_safety(self):
        """Catch-up blocked by safety gate doesn't place order."""
        engine, rest = _engine_with_pair_and_books()
        ledger = engine.adjuster.get_ledger("EVT-1")
        # Create larger imbalance so fresh sync still shows delta >= unit_size
        # even after B picks up 5 resting
        ledger.record_fill(Side.A, 40, 45)
        ledger.record_fill(Side.B, 20, 48)

        engine.check_imbalances()
        assert len(engine.proposal_queue) == 1

        key = engine.proposal_queue.pending()[0].key
        rest.create_order = AsyncMock()
        # Fresh sync: A=40f, B=20f+5r=25 committed → delta=15 >= 10,
        # but B has resting → safety gate blocks placement
        rest.get_orders = AsyncMock(
            return_value=[
                _make_order(
                    "TK-A",
                    order_id="ord-a-done",
                    fill_count=40,
                    no_price=45,
                    status="canceled",
                ),
                _make_order(
                    "TK-B",
                    order_id="ord-b-done",
                    fill_count=20,
                    no_price=48,
                    status="canceled",
                ),
                _make_order(
                    "TK-B",
                    order_id="ord-b-late",
                    remaining_count=5,
                    no_price=48,
                    status="resting",
                ),
            ]
        )
        notifications: list[tuple[str, str]] = []
        engine.on_notification = lambda msg, sev: notifications.append((msg, sev))
        await engine.approve_proposal(key)

        # Catch-up should be blocked (B already has resting after fresh sync)
        rest.create_order.assert_not_called()
        assert any("BLOCKED" in msg for msg, _ in notifications)


# ── Fresh-sync-before-catchup tests ──────────────────────────────────


class TestFreshSyncBeforeCatchup:
    """Tests for the fresh Kalshi sync guard before catch-up placement (P7/P21)."""

    @pytest.mark.asyncio
    async def test_catchup_skipped_when_fresh_sync_resolves_imbalance(self):
        """If fresh sync shows imbalance < unit_size, catch-up is skipped."""
        engine, rest = _engine_with_pair_and_books()
        ledger = engine.adjuster.get_ledger("EVT-1")
        # Stale view: A=30f, B=20f → delta=10 → proposes catch-up
        ledger.record_fill(Side.A, 30, 45)
        ledger.record_fill(Side.B, 20, 48)

        engine.check_imbalances()
        assert len(engine.proposal_queue) == 1
        key = engine.proposal_queue.pending()[0].key

        # Fresh sync reveals B filled up to 25 between polls → delta=5 < 10
        rest.get_orders = AsyncMock(
            return_value=[
                _make_order(
                    "TK-A",
                    order_id="oa",
                    fill_count=30,
                    no_price=45,
                    status="canceled",
                ),
                _make_order(
                    "TK-B",
                    order_id="ob",
                    fill_count=25,
                    no_price=48,
                    status="canceled",
                ),
            ]
        )
        rest.create_order = AsyncMock()
        notifications: list[tuple[str, str]] = []
        engine.on_notification = lambda msg, sev: notifications.append((msg, sev))

        await engine.approve_proposal(key)

        rest.create_order.assert_not_called()
        assert any("skipped" in msg.lower() for msg, _ in notifications)

    @pytest.mark.asyncio
    async def test_catchup_blocked_when_fresh_sync_fails(self):
        """If fresh sync raises, catch-up is blocked — never trust stale data."""
        engine, rest = _engine_with_pair_and_books()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 30, 45)
        ledger.record_fill(Side.B, 20, 48)

        engine.check_imbalances()
        key = engine.proposal_queue.pending()[0].key

        rest.get_orders = AsyncMock(side_effect=RuntimeError("API timeout"))
        rest.create_order = AsyncMock()
        notifications: list[tuple[str, str]] = []
        engine.on_notification = lambda msg, sev: notifications.append((msg, sev))

        await engine.approve_proposal(key)

        rest.create_order.assert_not_called()
        assert any("fresh sync failed" in msg.lower() for msg, _ in notifications)

    @pytest.mark.asyncio
    async def test_catchup_blocked_when_pair_not_found(self):
        """If pair is missing from scanner, catch-up is blocked."""
        engine, rest = _engine_with_pair_and_books()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 30, 45)
        ledger.record_fill(Side.B, 20, 48)

        engine.check_imbalances()
        key = engine.proposal_queue.pending()[0].key

        # Remove the pair from scanner so _find_pair returns None
        engine._scanner._pairs.clear()

        rest.create_order = AsyncMock()
        notifications: list[tuple[str, str]] = []
        engine.on_notification = lambda msg, sev: notifications.append((msg, sev))

        await engine.approve_proposal(key)

        rest.create_order.assert_not_called()
        assert any("pair not found" in msg.lower() for msg, _ in notifications)

    @pytest.mark.asyncio
    async def test_fresh_sync_confirms_imbalance_catchup_proceeds(self):
        """When fresh sync confirms imbalance still exists, catch-up proceeds."""
        engine, rest = _engine_with_pair_and_books()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 30, 45)
        ledger.record_fill(Side.B, 20, 48)

        engine.check_imbalances()
        key = engine.proposal_queue.pending()[0].key

        # Fresh sync confirms same state — imbalance still exists
        rest.get_orders = AsyncMock(
            return_value=[
                _make_order(
                    "TK-A",
                    order_id="oa",
                    fill_count=30,
                    no_price=45,
                    status="canceled",
                ),
                _make_order(
                    "TK-B",
                    order_id="ob",
                    fill_count=20,
                    no_price=48,
                    status="canceled",
                ),
            ]
        )
        rest.create_order = AsyncMock(return_value=_make_order("TK-B", order_id="new-b"))

        await engine.approve_proposal(key)

        rest.create_order.assert_called_once_with(
            ticker="TK-B",
            action="buy",
            side="no",
            no_price=48,
            count=10,
        )

    @pytest.mark.asyncio
    async def test_reduce_only_rebalance_no_catchup_but_verifies(self):
        """Rebalance with only step 1 (reduce) still verifies after action."""
        engine, rest = _engine_with_pair_and_books()
        ledger = engine.adjuster.get_ledger("EVT-1")
        # A: 40f+10r=50, B: 20f+10r=30 → reduce A only (B has resting)
        ledger.record_fill(Side.A, 40, 45)
        ledger.record_resting(Side.A, "ord-a", 10, 45)
        ledger.record_fill(Side.B, 20, 48)
        ledger.record_resting(Side.B, "ord-b", 10, 48)

        engine.check_imbalances()
        key = engine.proposal_queue.pending()[0].key

        rest.cancel_order = AsyncMock()
        # Verification sync happens after step 1
        rest.get_orders = AsyncMock(return_value=[])
        rest.get_positions = AsyncMock(return_value=[])

        await engine.approve_proposal(key)

        rest.cancel_order.assert_called_once_with("ord-a")
        # Post-action verification did run
        rest.get_orders.assert_called_once()
        rest.get_positions.assert_called_once()


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

    def test_discrepancy_takes_priority_over_jumped(self):
        """Discrepancy status should fire before Jumped check."""
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
        ledger._discrepancy = "test"

        status = engine._compute_event_status("EVT-1")
        assert status == "Discrepancy"


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
        rest.get_orders.return_value = []
        rest.get_queue_positions.return_value = {}

        await engine.refresh_account()

        # Recovery should have been called (unsubscribe + subscribe for TK-A)
        engine._feed.unsubscribe.assert_called_once_with("TK-A")
        engine._feed.subscribe.assert_called_once_with("TK-A")
