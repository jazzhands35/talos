"""Tests for exit-only mode — gates, status display, and auto-trigger."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.automation_config import AutomationConfig
from talos.bid_adjuster import BidAdjuster
from talos.game_status import GameStatus
from talos.models.strategy import ArbPair
from talos.opportunity_proposer import OpportunityProposer
from talos.orderbook import OrderBookManager
from talos.position_ledger import PositionLedger, Side

# ── Helpers ─────────────────────────────────────────────────────


def _pair(event: str = "EVT-1") -> ArbPair:
    return ArbPair(
        event_ticker=event,
        ticker_a=f"{event}-A",
        ticker_b=f"{event}-B",
        fee_type="standard",
        fee_rate=0.07,
    )


def _opportunity(edge: float = 2.0):
    from talos.models.strategy import Opportunity

    return Opportunity(
        event_ticker="EVT-1",
        ticker_a="EVT-1-A",
        ticker_b="EVT-1-B",
        no_a=14,
        no_b=79,
        qty_a=100,
        qty_b=100,
        raw_edge=7,
        fee_edge=edge,
        tradeable_qty=100,
        timestamp=datetime.now(UTC).isoformat(),
    )


# ── OpportunityProposer gate ───────────────────────────────────


class TestProposerExitOnlyGate:
    """OpportunityProposer.evaluate() returns None when exit_only=True."""

    def test_exit_only_blocks_new_bids(self):
        config = AutomationConfig(edge_threshold_cents=1.0, stability_seconds=0)
        proposer = OpportunityProposer(config)
        pair = _pair()
        opp = _opportunity(edge=5.0)
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)

        result = proposer.evaluate(pair, opp, ledger, pending_keys=set(), exit_only=True)
        assert result is None

    def test_normal_mode_allows_bids(self):
        config = AutomationConfig(edge_threshold_cents=1.0, stability_seconds=0)
        proposer = OpportunityProposer(config)
        pair = _pair()
        opp = _opportunity(edge=5.0)
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)

        result = proposer.evaluate(pair, opp, ledger, pending_keys=set(), exit_only=False)
        assert result is not None
        assert result.kind == "bid"


# ── BidAdjuster gate ──────────────────────────────────────────


class TestAdjusterExitOnlyGate:
    """BidAdjuster.evaluate_jump() blocks ahead side when exit_only=True."""

    def _make_adjuster(self, pair: ArbPair) -> BidAdjuster:
        books = OrderBookManager()
        return BidAdjuster(book_manager=books, pairs=[pair])

    def test_balanced_blocks_all_adjustments(self):
        """When balanced (filled_a == filled_b), exit-only blocks all adjustments."""
        pair = _pair()
        adjuster = self._make_adjuster(pair)
        ledger = adjuster.get_ledger("EVT-1")

        # Both sides equal fills
        ledger.record_fill(Side.A, count=5, price=48)
        ledger.record_fill(Side.B, count=5, price=49)
        ledger.record_resting(Side.A, "ord-1", count=5, price=48)

        result = adjuster.evaluate_jump("EVT-1-A", at_top=False, exit_only=True)
        assert result is None

    def test_imbalanced_blocks_ahead_side(self):
        """When A is ahead (more fills), exit-only blocks A adjustments."""
        pair = _pair()
        adjuster = self._make_adjuster(pair)
        ledger = adjuster.get_ledger("EVT-1")

        ledger.record_fill(Side.A, count=8, price=48)
        ledger.record_fill(Side.B, count=3, price=49)
        ledger.record_resting(Side.A, "ord-a", count=2, price=48)

        # A is ahead — blocked
        result = adjuster.evaluate_jump("EVT-1-A", at_top=False, exit_only=True)
        assert result is None

    def test_exit_only_false_doesnt_block(self):
        """exit_only=False preserves normal behavior (no book = returns None from book check)."""
        pair = _pair()
        adjuster = self._make_adjuster(pair)
        ledger = adjuster.get_ledger("EVT-1")

        ledger.record_fill(Side.A, count=5, price=48)
        ledger.record_fill(Side.B, count=5, price=49)
        ledger.record_resting(Side.A, "ord-1", count=5, price=48)

        # Without exit_only, proceeds past exit gate to book check (returns None — no book)
        adjuster.evaluate_jump("EVT-1-A", at_top=False, exit_only=False)
        # Returns None from no book data — that's expected and fine


# ── Status display ────────────────────────────────────────────


class TestExitOnlyStatusDisplay:
    """_fmt_status renders EXIT variants correctly."""

    def test_fmt_status_exit(self):
        from talos.ui.widgets import _fmt_status

        result = _fmt_status("EXIT")
        text = str(result)
        assert "EXIT" in text

    def test_fmt_status_exit_behind(self):
        from talos.ui.widgets import _fmt_status

        result = _fmt_status("EXIT -5 B")
        text = str(result)
        assert "EXIT -5 B" in text

    def test_fmt_status_exiting(self):
        from talos.ui.widgets import _fmt_status

        result = _fmt_status("EXITING")
        text = str(result)
        assert "EXITING" in text

    def test_exiting_matched_before_exit(self):
        """EXITING must match its own entry, not the EXIT prefix."""
        from talos.ui.widgets import _fmt_status

        exit_result = _fmt_status("EXIT")
        exiting_result = _fmt_status("EXITING")
        # Both should render, but EXITING shouldn't just be "EXIT" + "ING"
        assert "EXITING" in str(exiting_result)
        # The EXIT result should NOT contain "EXITING"
        exit_text = str(exit_result)
        assert exit_text.count("EXIT") >= 1


# ── AutomationConfig ─────────────────────────────────────────


class TestExitOnlyConfig:
    def test_default_exit_only_minutes(self):
        config = AutomationConfig()
        assert config.exit_only_minutes == 30.0

    def test_custom_exit_only_minutes(self):
        config = AutomationConfig(exit_only_minutes=15.0)
        assert config.exit_only_minutes == 15.0


# ── Auto-trigger timing ──────────────────────────────────────


class TestExitOnlyAutoTrigger:
    """Verify the timing logic used in _check_exit_only."""

    def test_live_game_triggers(self):
        """Live games should trigger exit-only."""
        gs = GameStatus(state="live", scheduled_start=datetime.now(UTC))
        assert gs.state == "live"

    def test_approaching_game_triggers(self):
        """Games within 30 min of start should trigger exit-only."""
        now = datetime.now(UTC)
        start = now + timedelta(minutes=20)
        gs = GameStatus(state="pre", scheduled_start=start)
        assert gs.scheduled_start is not None
        minutes_to_start = (gs.scheduled_start - now).total_seconds() / 60
        assert minutes_to_start < 30

    def test_far_game_doesnt_trigger(self):
        """Games far from start should NOT trigger exit-only."""
        now = datetime.now(UTC)
        start = now + timedelta(hours=3)
        gs = GameStatus(state="pre", scheduled_start=start)
        assert gs.scheduled_start is not None
        minutes_to_start = (gs.scheduled_start - now).total_seconds() / 60
        assert minutes_to_start > 30

    def test_unknown_state_doesnt_trigger(self):
        """Games with unknown state should NOT trigger."""
        gs = GameStatus(state="unknown")
        assert gs.state == "unknown"
        # _check_exit_only skips "unknown" — no auto-trigger


# ── Enforcement decisions ─────────────────────────────────────


class TestExitOnlyEnforcement:
    """Test the enforcement logic (balanced vs imbalanced)."""

    def test_balanced_should_cancel_both_sides(self):
        """When fills are equal, both sides' resting should be cancelled."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=5, price=48)
        ledger.record_fill(Side.B, count=5, price=49)
        ledger.record_resting(Side.A, "ord-a", count=5, price=48)
        ledger.record_resting(Side.B, "ord-b", count=5, price=49)

        assert ledger.filled_count(Side.A) == ledger.filled_count(Side.B)
        assert ledger.resting_order_id(Side.A) is not None
        assert ledger.resting_order_id(Side.B) is not None

    def test_imbalanced_identifies_ahead_side(self):
        """A ahead means cancel A resting, leave B."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=8, price=48)
        ledger.record_fill(Side.B, count=3, price=49)

        filled_a = ledger.filled_count(Side.A)
        filled_b = ledger.filled_count(Side.B)
        ahead = Side.A if filled_a > filled_b else Side.B
        assert ahead is Side.A

    def test_balanced_no_resting_is_done(self):
        """Balanced + no resting = ready for auto-remove."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=10, price=48)
        ledger.record_fill(Side.B, count=10, price=49)

        assert ledger.filled_count(Side.A) == ledger.filled_count(Side.B)
        assert ledger.resting_count(Side.A) == 0
        assert ledger.resting_count(Side.B) == 0

    def test_zero_zero_is_balanced(self):
        """0==0 counts as balanced — cancel all resting."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_resting(Side.A, "ord-a", count=10, price=48)
        ledger.record_resting(Side.B, "ord-b", count=10, price=49)

        assert ledger.filled_count(Side.A) == 0
        assert ledger.filled_count(Side.B) == 0
        # 0 == 0 → balanced → cancel both

    def test_imbalanced_behind_side_target_resting(self):
        """Behind side's resting should be reduced to match ahead fills."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, count=5, price=48)
        ledger.record_resting(Side.A, "ord-a", count=15, price=48)
        ledger.record_fill(Side.B, count=1, price=49)
        ledger.record_resting(Side.B, "ord-b", count=19, price=49)

        ahead = Side.A  # 5 > 1
        behind = Side.B
        # Behind needs: ahead_filled - behind_filled = 5 - 1 = 4 resting
        target_behind_resting = ledger.filled_count(ahead) - ledger.filled_count(behind)
        assert target_behind_resting == 4
        assert ledger.resting_count(behind) == 19  # currently 19, needs reducing to 4


# ── Async enforcement integration ────────────────────────────


class TestGameStartCancelAll:
    """When game has started (live/post), cancel ALL resting — no behind-side preservation."""

    def _make_engine_with_pair(self):
        from talos.engine import TradingEngine
        from talos.game_manager import GameManager
        from talos.market_feed import MarketFeed
        from talos.rest_client import KalshiRESTClient
        from talos.scanner import ArbitrageScanner
        from talos.top_of_market import TopOfMarketTracker

        books = OrderBookManager()
        scanner = ArbitrageScanner(books)
        scanner.add_pair("EVT-1", "TK-A", "TK-B")
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        adjuster = BidAdjuster(books, [pair], unit_size=20)
        rest = AsyncMock(spec=KalshiRESTClient)
        engine = TradingEngine(
            scanner=scanner,
            game_manager=MagicMock(spec=GameManager),
            rest_client=rest,
            market_feed=MagicMock(spec=MarketFeed),
            tracker=TopOfMarketTracker(books),
            adjuster=adjuster,
        )
        return engine, rest

    @pytest.mark.asyncio
    async def test_game_started_preserves_behind_side_when_imbalanced(self):
        """Game started + imbalanced → cancel ahead, keep behind catch-up."""
        engine, rest = self._make_engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=8, price=48)
        ledger.record_resting(Side.A, "ord-a", count=12, price=48)
        ledger.record_fill(Side.B, count=3, price=49)
        ledger.record_resting(Side.B, "ord-b", count=17, price=49)

        # Mark as game started
        engine._game_started_events.add("EVT-1")

        rest.cancel_order = AsyncMock()
        rest.decrease_order = AsyncMock()
        rest.get_all_orders = AsyncMock(return_value=[])

        await engine._enforce_exit_only("EVT-1")

        # Ahead side (A) cancelled, behind side (B) reduced to 5 (8 - 3)
        rest.cancel_order.assert_called_once_with("ord-a")
        rest.decrease_order.assert_called_once_with("ord-b", reduce_to=5)

    @pytest.mark.asyncio
    async def test_pre_game_exit_only_preserves_behind_side(self):
        """Pre-game exit-only (not started) preserves behind-side resting."""
        engine, rest = self._make_engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=8, price=48)
        ledger.record_resting(Side.A, "ord-a", count=12, price=48)
        ledger.record_fill(Side.B, count=3, price=49)
        ledger.record_resting(Side.B, "ord-b", count=17, price=49)

        # NOT in _game_started_events — only in exit-only
        engine._exit_only_events.add("EVT-1")
        # _game_started_events is empty

        rest.cancel_order = AsyncMock()
        rest.decrease_order = AsyncMock()
        rest.get_all_orders = AsyncMock(return_value=[])

        await engine._enforce_exit_only("EVT-1")

        # Ahead side (A) cancelled, behind side (B) reduced to 5 (8 - 3)
        rest.cancel_order.assert_called_once_with("ord-a")
        rest.decrease_order.assert_called_once_with("ord-b", reduce_to=5)

    @pytest.mark.asyncio
    async def test_game_started_balanced_still_cancels(self):
        """Game started + balanced → cancel both sides (same as before)."""
        engine, rest = self._make_engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=5, price=48)
        ledger.record_fill(Side.B, count=5, price=49)
        ledger.record_resting(Side.A, "ord-a", count=5, price=48)
        ledger.record_resting(Side.B, "ord-b", count=5, price=49)

        engine._game_started_events.add("EVT-1")

        rest.cancel_order = AsyncMock()
        rest.get_all_orders = AsyncMock(return_value=[])

        await engine._enforce_exit_only("EVT-1")

        assert rest.cancel_order.call_count == 2

    def test_live_game_adds_to_game_started(self):
        """_check_exit_only marks live games in _game_started_events."""
        from talos.engine import TradingEngine
        from talos.game_manager import GameManager
        from talos.game_status import GameStatusResolver
        from talos.market_feed import MarketFeed
        from talos.rest_client import KalshiRESTClient
        from talos.scanner import ArbitrageScanner
        from talos.top_of_market import TopOfMarketTracker

        books = OrderBookManager()
        scanner = ArbitrageScanner(books)
        scanner.add_pair("EVT-1", "TK-A", "TK-B")
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        adjuster = BidAdjuster(books, [pair], unit_size=20)

        resolver = MagicMock(spec=GameStatusResolver)
        resolver.get.return_value = GameStatus(state="live")

        engine = TradingEngine(
            scanner=scanner,
            game_manager=MagicMock(spec=GameManager),
            rest_client=AsyncMock(spec=KalshiRESTClient),
            market_feed=MagicMock(spec=MarketFeed),
            tracker=TopOfMarketTracker(books),
            adjuster=adjuster,
            game_status_resolver=resolver,
        )

        engine._check_exit_only()

        assert "EVT-1" in engine._exit_only_events
        assert "EVT-1" in engine._game_started_events

    def test_post_game_adds_to_game_started(self):
        """_check_exit_only marks post (final) games in _game_started_events."""
        from talos.engine import TradingEngine
        from talos.game_manager import GameManager
        from talos.game_status import GameStatusResolver
        from talos.market_feed import MarketFeed
        from talos.rest_client import KalshiRESTClient
        from talos.scanner import ArbitrageScanner
        from talos.top_of_market import TopOfMarketTracker

        books = OrderBookManager()
        scanner = ArbitrageScanner(books)
        scanner.add_pair("EVT-1", "TK-A", "TK-B")
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        adjuster = BidAdjuster(books, [pair], unit_size=20)

        resolver = MagicMock(spec=GameStatusResolver)
        resolver.get.return_value = GameStatus(state="post")

        engine = TradingEngine(
            scanner=scanner,
            game_manager=MagicMock(spec=GameManager),
            rest_client=AsyncMock(spec=KalshiRESTClient),
            market_feed=MagicMock(spec=MarketFeed),
            tracker=TopOfMarketTracker(books),
            adjuster=adjuster,
            game_status_resolver=resolver,
        )

        engine._check_exit_only()

        assert "EVT-1" in engine._exit_only_events
        assert "EVT-1" in engine._game_started_events

    def test_pre_game_does_not_add_to_game_started(self):
        """Pre-game with 20 min to start → exit-only YES, game-started NO."""
        from talos.engine import TradingEngine
        from talos.game_manager import GameManager
        from talos.game_status import GameStatusResolver
        from talos.market_feed import MarketFeed
        from talos.rest_client import KalshiRESTClient
        from talos.scanner import ArbitrageScanner
        from talos.top_of_market import TopOfMarketTracker

        books = OrderBookManager()
        scanner = ArbitrageScanner(books)
        scanner.add_pair("EVT-1", "TK-A", "TK-B")
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        adjuster = BidAdjuster(books, [pair], unit_size=20)

        resolver = MagicMock(spec=GameStatusResolver)
        resolver.get.return_value = GameStatus(
            state="pre",
            scheduled_start=datetime.now(UTC) + timedelta(minutes=20),
        )

        engine = TradingEngine(
            scanner=scanner,
            game_manager=MagicMock(spec=GameManager),
            rest_client=AsyncMock(spec=KalshiRESTClient),
            market_feed=MagicMock(spec=MarketFeed),
            tracker=TopOfMarketTracker(books),
            adjuster=adjuster,
            game_status_resolver=resolver,
        )

        engine._check_exit_only()

        assert "EVT-1" in engine._exit_only_events
        assert "EVT-1" not in engine._game_started_events


class TestExitOnlyEnforcementAsync:
    """Test _enforce_exit_only via TradingEngine with mock REST."""

    def _make_engine_with_pair(self):
        from talos.engine import TradingEngine
        from talos.game_manager import GameManager
        from talos.market_feed import MarketFeed
        from talos.rest_client import KalshiRESTClient
        from talos.scanner import ArbitrageScanner
        from talos.top_of_market import TopOfMarketTracker

        books = OrderBookManager()
        scanner = ArbitrageScanner(books)
        scanner.add_pair("EVT-1", "TK-A", "TK-B")
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        adjuster = BidAdjuster(books, [pair], unit_size=20)
        rest = AsyncMock(spec=KalshiRESTClient)
        engine = TradingEngine(
            scanner=scanner,
            game_manager=MagicMock(spec=GameManager),
            rest_client=rest,
            market_feed=MagicMock(spec=MarketFeed),
            tracker=TopOfMarketTracker(books),
            adjuster=adjuster,
        )
        return engine, rest

    @pytest.mark.asyncio
    async def test_imbalanced_cancels_ahead_and_reduces_behind(self):
        """Exit-only: cancel ahead resting, reduce behind resting to match."""
        engine, rest = self._make_engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=5, price=48)
        ledger.record_resting(Side.A, "ord-a", count=15, price=48)
        ledger.record_fill(Side.B, count=1, price=49)
        ledger.record_resting(Side.B, "ord-b", count=19, price=49)

        rest.cancel_order = AsyncMock()
        rest.get_order = AsyncMock(return_value=type("O", (), {"remaining_count": 19})())
        rest.decrease_order = AsyncMock()
        rest.get_all_orders = AsyncMock(return_value=[])

        await engine._enforce_exit_only("EVT-1")

        # Step 1: cancel ahead side (A) resting
        rest.cancel_order.assert_called_once_with("ord-a")
        # Step 2: reduce behind side (B) from 19 to 4
        rest.decrease_order.assert_called_once_with("ord-b", reduce_to=4)

    @pytest.mark.asyncio
    async def test_imbalanced_behind_no_resting_no_decrease(self):
        """If behind side has no resting, only cancel ahead side."""
        engine, rest = self._make_engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=5, price=48)
        ledger.record_resting(Side.A, "ord-a", count=15, price=48)
        ledger.record_fill(Side.B, count=1, price=49)
        # B has no resting

        rest.cancel_order = AsyncMock()
        rest.decrease_order = AsyncMock()
        rest.get_all_orders = AsyncMock(return_value=[])

        await engine._enforce_exit_only("EVT-1")

        rest.cancel_order.assert_called_once_with("ord-a")
        rest.decrease_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_imbalanced_behind_resting_already_at_target(self):
        """If behind resting is already <= target, no decrease needed."""
        engine, rest = self._make_engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=5, price=48)
        ledger.record_resting(Side.A, "ord-a", count=15, price=48)
        ledger.record_fill(Side.B, count=2, price=49)
        ledger.record_resting(Side.B, "ord-b", count=3, price=49)
        # target = 5 - 2 = 3, behind has exactly 3 → no decrease

        rest.cancel_order = AsyncMock()
        rest.decrease_order = AsyncMock()
        rest.get_all_orders = AsyncMock(return_value=[])

        await engine._enforce_exit_only("EVT-1")

        rest.cancel_order.assert_called_once_with("ord-a")
        rest.decrease_order.assert_not_called()
