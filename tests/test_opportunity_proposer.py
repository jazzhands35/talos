"""Tests for OpportunityProposer — edge, stability, position, cooldown gates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from talos.automation_config import AutomationConfig
from talos.models.proposal import ProposalKey
from talos.models.strategy import ArbPair, Opportunity
from talos.opportunity_proposer import OpportunityProposer
from talos.position_ledger import PositionLedger, Side


def _make_pair(event_ticker: str = "EVT-1") -> ArbPair:
    return ArbPair(event_ticker=event_ticker, ticker_a="TK-A", ticker_b="TK-B")


def _make_opp(
    event_ticker: str = "EVT-1", no_a: int = 48, no_b: int = 50, fee_edge: float = 1.5
) -> Opportunity:
    return Opportunity(
        event_ticker=event_ticker,
        ticker_a="TK-A",
        ticker_b="TK-B",
        no_a=no_a,
        no_b=no_b,
        qty_a=100,
        qty_b=100,
        raw_edge=100 - no_a - no_b,
        fee_edge=fee_edge,
        tradeable_qty=100,
        timestamp=datetime.now(UTC).isoformat(),
    )


# ── Edge Threshold ──────────────────────────────────────────────────


def test_proposer_no_longer_takes_drip_kwarg() -> None:
    """After the insertion-strategy redesign, the proposer no longer
    accepts a `drip` parameter. The signature must reject it."""
    import inspect

    sig = inspect.signature(OpportunityProposer.evaluate)
    assert "drip" not in sig.parameters, (
        "drip parameter should be removed; "
        f"got params: {list(sig.parameters.keys())}"
    )


class TestEdgeThreshold:
    def test_below_threshold_no_proposal(self) -> None:
        cfg = AutomationConfig(edge_threshold_cents=1.5, stability_seconds=0)
        proposer = OpportunityProposer(cfg)
        pair = _make_pair()
        opp = _make_opp(fee_edge=1.0)
        ledger = PositionLedger("EVT-1", unit_size=10)
        now = datetime.now(UTC)

        result = proposer.evaluate(pair, opp, ledger, set(), now=now)
        assert result is None

    def test_above_threshold_with_zero_stability_proposes(self) -> None:
        cfg = AutomationConfig(edge_threshold_cents=1.5, stability_seconds=0)
        proposer = OpportunityProposer(cfg)
        pair = _make_pair()
        opp = _make_opp(fee_edge=2.0, no_a=48, no_b=50)
        ledger = PositionLedger("EVT-1", unit_size=10)
        now = datetime.now(UTC)

        result = proposer.evaluate(pair, opp, ledger, set(), now=now)
        assert result is not None
        assert result.kind == "bid"
        assert result.bid is not None
        assert result.bid.no_a == 48
        assert result.bid.no_b == 50
        assert result.bid.qty == 10
        assert result.bid.edge_cents == 2.0
        assert result.bid.event_ticker == "EVT-1"


# ── Position Gate ───────────────────────────────────────────────────


class TestPositionGate:
    def test_resting_covers_unit_no_proposal(self) -> None:
        """When resting on both sides covers the full unit, no proposal."""
        cfg = AutomationConfig(edge_threshold_cents=1.0, stability_seconds=0)
        proposer = OpportunityProposer(cfg)
        pair = _make_pair()
        opp = _make_opp(fee_edge=2.0)
        ledger = PositionLedger("EVT-1", unit_size=10)
        ledger.record_resting(Side.A, "ord-a", 10, 48)
        ledger.record_resting(Side.B, "ord-b", 10, 50)
        now = datetime.now(UTC)

        result = proposer.evaluate(pair, opp, ledger, set(), now=now)
        assert result is None

    def test_partial_resting_proposes_remainder(self) -> None:
        """When resting doesn't cover the full unit, propose the gap."""
        cfg = AutomationConfig(edge_threshold_cents=1.0, stability_seconds=0)
        proposer = OpportunityProposer(cfg)
        pair = _make_pair()
        opp = _make_opp(fee_edge=2.0)
        ledger = PositionLedger("EVT-1", unit_size=10)
        ledger.record_resting(Side.A, "ord-a", 5, 48)
        ledger.record_resting(Side.B, "ord-b", 5, 50)
        now = datetime.now(UTC)

        result = proposer.evaluate(pair, opp, ledger, set(), now=now)
        assert result is not None
        assert result.bid is not None
        assert result.bid.qty == 5

    def test_both_sides_complete_suggests_reentry(self) -> None:
        """After both sides fill a full unit, suggest re-entry if profitable."""
        cfg = AutomationConfig(edge_threshold_cents=1.0, stability_seconds=0)
        proposer = OpportunityProposer(cfg)
        pair = _make_pair()
        opp = _make_opp(fee_edge=2.0)
        ledger = PositionLedger("EVT-1", unit_size=10)
        ledger.record_resting(Side.A, "ord-a", 10, 48)
        ledger.record_fill(Side.A, 10, 48)
        ledger.record_resting(Side.B, "ord-b", 10, 50)
        ledger.record_fill(Side.B, 10, 50)
        now = datetime.now(UTC)

        result = proposer.evaluate(pair, opp, ledger, set(), now=now)
        assert result is not None
        assert result.kind == "bid"
        assert result.bid is not None
        assert result.bid.qty == 10

    def test_both_sides_complete_with_resting_no_proposal(self) -> None:
        """If next pair's bids are already resting, don't suggest again."""
        cfg = AutomationConfig(edge_threshold_cents=1.0, stability_seconds=0)
        proposer = OpportunityProposer(cfg)
        pair = _make_pair()
        opp = _make_opp(fee_edge=2.0)
        ledger = PositionLedger("EVT-1", unit_size=10)
        # First pair filled
        ledger.record_resting(Side.A, "ord-a1", 10, 48)
        ledger.record_fill(Side.A, 10, 48)
        ledger.record_resting(Side.B, "ord-b1", 10, 50)
        ledger.record_fill(Side.B, 10, 50)
        # Second pair already resting
        ledger.record_resting(Side.A, "ord-a2", 10, 47)
        ledger.record_resting(Side.B, "ord-b2", 10, 49)
        now = datetime.now(UTC)

        result = proposer.evaluate(pair, opp, ledger, set(), now=now)
        assert result is None

    def test_imbalanced_fills_no_reentry(self) -> None:
        """If sides have different fill counts, don't suggest re-entry."""
        cfg = AutomationConfig(edge_threshold_cents=1.0, stability_seconds=0)
        proposer = OpportunityProposer(cfg)
        pair = _make_pair()
        opp = _make_opp(fee_edge=2.0)
        ledger = PositionLedger("EVT-1", unit_size=10)
        ledger.record_resting(Side.A, "ord-a", 10, 48)
        ledger.record_fill(Side.A, 10, 48)
        # Side B only partially filled
        ledger.record_resting(Side.B, "ord-b", 10, 50)
        ledger.record_fill(Side.B, 5, 50)
        now = datetime.now(UTC)

        result = proposer.evaluate(pair, opp, ledger, set(), now=now)
        assert result is None

    def test_one_side_empty_blocked_by_committed_delta(self) -> None:
        """Asymmetric resting (A=5, B=0) creates committed delta — bid blocked.

        Placing equal qty on both sides preserves the delta, and the old
        resting on A becomes orphaned on Kalshi, causing rebalance thrashing.
        """
        cfg = AutomationConfig(edge_threshold_cents=1.0, stability_seconds=0)
        proposer = OpportunityProposer(cfg)
        pair = _make_pair()
        opp = _make_opp(fee_edge=2.0)
        ledger = PositionLedger("EVT-1", unit_size=10)
        ledger.record_resting(Side.A, "ord-a", 5, 48)
        # Side B is empty → committed_a=5, committed_b=0 → blocked
        now = datetime.now(UTC)

        result = proposer.evaluate(pair, opp, ledger, set(), now=now)
        assert result is None


# ── Stability Filter ───────────────────────────────────────────────


class TestStabilityFilter:
    def test_first_sight_starts_timer_no_proposal(self) -> None:
        cfg = AutomationConfig(edge_threshold_cents=1.0, stability_seconds=5)
        proposer = OpportunityProposer(cfg)
        pair = _make_pair()
        opp = _make_opp(fee_edge=2.0)
        ledger = PositionLedger("EVT-1", unit_size=10)
        now = datetime.now(UTC)

        result = proposer.evaluate(pair, opp, ledger, set(), now=now)
        assert result is None

    def test_stable_long_enough_proposes(self) -> None:
        cfg = AutomationConfig(edge_threshold_cents=1.0, stability_seconds=5)
        proposer = OpportunityProposer(cfg)
        pair = _make_pair()
        opp = _make_opp(fee_edge=2.0)
        ledger = PositionLedger("EVT-1", unit_size=10)
        t0 = datetime.now(UTC)

        # First eval: starts timer
        result1 = proposer.evaluate(pair, opp, ledger, set(), now=t0)
        assert result1 is None

        # Second eval: 6s later, stable long enough
        t1 = t0 + timedelta(seconds=6)
        result2 = proposer.evaluate(pair, opp, ledger, set(), now=t1)
        assert result2 is not None
        assert result2.bid is not None
        assert result2.bid.stable_for_seconds >= 5.0

    def test_edge_drops_resets_timer(self) -> None:
        cfg = AutomationConfig(edge_threshold_cents=1.5, stability_seconds=5)
        proposer = OpportunityProposer(cfg)
        pair = _make_pair()
        ledger = PositionLedger("EVT-1", unit_size=10)
        t0 = datetime.now(UTC)

        # t=0: edge above threshold, starts timer
        opp_above = _make_opp(fee_edge=2.0)
        proposer.evaluate(pair, opp_above, ledger, set(), now=t0)

        # t=3s: edge drops below threshold, resets timer
        t1 = t0 + timedelta(seconds=3)
        opp_below = _make_opp(fee_edge=1.0)
        proposer.evaluate(pair, opp_below, ledger, set(), now=t1)

        # t=6s: edge back above, but only 3s since reset — not stable enough
        t2 = t0 + timedelta(seconds=6)
        opp_above2 = _make_opp(fee_edge=2.0)
        result = proposer.evaluate(pair, opp_above2, ledger, set(), now=t2)
        assert result is None

        # t=12s: 6s since timer restart at t=6 — now stable
        t3 = t0 + timedelta(seconds=12)
        result2 = proposer.evaluate(pair, opp_above2, ledger, set(), now=t3)
        assert result2 is not None


# ── Duplicate Prevention ────────────────────────────────────────────


class TestDuplicatePrevention:
    def test_no_proposal_when_pending_exists(self) -> None:
        cfg = AutomationConfig(edge_threshold_cents=1.0, stability_seconds=0)
        proposer = OpportunityProposer(cfg)
        pair = _make_pair()
        opp = _make_opp(fee_edge=2.0)
        ledger = PositionLedger("EVT-1", unit_size=10)
        now = datetime.now(UTC)

        pending = {ProposalKey(event_ticker="EVT-1", side="", kind="bid")}
        result = proposer.evaluate(pair, opp, ledger, pending, now=now)
        assert result is None


# ── Cooldown ────────────────────────────────────────────────────────


class TestCooldown:
    def test_cooldown_after_rejection(self) -> None:
        cfg = AutomationConfig(
            edge_threshold_cents=1.0,
            stability_seconds=0,
            rejection_cooldown_seconds=30,
        )
        proposer = OpportunityProposer(cfg)
        pair = _make_pair()
        opp = _make_opp(fee_edge=2.0)
        ledger = PositionLedger("EVT-1", unit_size=10)
        t0 = datetime.now(UTC)

        # Record a rejection
        proposer.record_rejection("EVT-1", now=t0)

        # Within cooldown (t0 + 10s) — no proposal
        t1 = t0 + timedelta(seconds=10)
        result = proposer.evaluate(pair, opp, ledger, set(), now=t1)
        assert result is None

        # After cooldown (t0 + 31s) — proposal
        t2 = t0 + timedelta(seconds=31)
        result2 = proposer.evaluate(pair, opp, ledger, set(), now=t2)
        assert result2 is not None

    def test_approval_resets_stability_timer(self) -> None:
        """After approving a bid, proposer must re-observe stable edge before re-proposing."""
        cfg = AutomationConfig(
            edge_threshold_cents=1.0,
            stability_seconds=10,
        )
        proposer = OpportunityProposer(cfg)
        pair = _make_pair()
        opp = _make_opp(fee_edge=2.0)
        ledger = PositionLedger("EVT-1", unit_size=10)
        t0 = datetime.now(UTC)

        # First call: starts stability timer
        proposer.evaluate(pair, opp, ledger, set(), now=t0)
        # After stability window: should propose
        t1 = t0 + timedelta(seconds=11)
        result = proposer.evaluate(pair, opp, ledger, set(), now=t1)
        assert result is not None

        # Simulate approval — resets stability timer
        proposer.record_approval("EVT-1")

        # Immediately after approval: stability timer was reset, must re-accumulate
        t2 = t1 + timedelta(seconds=1)
        result2 = proposer.evaluate(pair, opp, ledger, set(), now=t2)
        assert result2 is None  # blocked by stability gate

        # After stability window again: should propose
        t3 = t2 + timedelta(seconds=11)
        result3 = proposer.evaluate(pair, opp, ledger, set(), now=t3)
        assert result3 is not None
