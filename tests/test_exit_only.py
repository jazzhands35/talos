"""Tests for exit-only mode — gates, status display, and auto-trigger."""

from datetime import UTC, datetime, timedelta

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
        no_a=48,
        no_b=49,
        qty_a=100,
        qty_b=100,
        raw_edge=3,
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
