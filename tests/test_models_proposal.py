"""Tests for Proposal, ProposalKey, and ProposedBid models."""

from datetime import UTC, datetime

from talos.models.adjustment import ProposedAdjustment
from talos.models.proposal import Proposal, ProposalKey, ProposedBid


def test_proposal_key_equality():
    k1 = ProposalKey(event_ticker="EVT-1", side="A", kind="adjustment")
    k2 = ProposalKey(event_ticker="EVT-1", side="A", kind="adjustment")
    assert k1 == k2


def test_proposal_key_inequality_different_kind():
    k1 = ProposalKey(event_ticker="EVT-1", side="A", kind="adjustment")
    k2 = ProposalKey(event_ticker="EVT-1", side="A", kind="bid")
    assert k1 != k2


def test_proposal_key_hashable():
    k1 = ProposalKey(event_ticker="EVT-1", side="A", kind="adjustment")
    k2 = ProposalKey(event_ticker="EVT-1", side="A", kind="adjustment")
    d: dict[ProposalKey, str] = {k1: "first"}
    d[k2] = "second"
    assert len(d) == 1
    assert d[k1] == "second"


def test_proposed_bid_creation():
    bid = ProposedBid(
        event_ticker="EVT-2",
        ticker_a="MKT-A",
        ticker_b="MKT-B",
        no_a=52,
        no_b=47,
        qty=10,
        edge_cents=1.0,
        stable_for_seconds=30.5,
        reason="arb edge 1c stable 30s",
    )
    assert bid.event_ticker == "EVT-2"
    assert bid.ticker_a == "MKT-A"
    assert bid.ticker_b == "MKT-B"
    assert bid.no_a == 52
    assert bid.no_b == 47
    assert bid.qty == 10
    assert bid.edge_cents == 1.0
    assert bid.stable_for_seconds == 30.5
    assert bid.reason == "arb edge 1c stable 30s"


def test_proposal_with_adjustment_payload():
    now = datetime.now(UTC)
    adj = ProposedAdjustment(
        event_ticker="EVT-1",
        side="A",
        action="follow_jump",
        cancel_order_id="order-123",
        cancel_count=10,
        cancel_price=48,
        new_count=10,
        new_price=49,
        reason="jumped 48c->49c",
        position_before="A: 10 filled @ 50c | B: 0 filled, 10 resting @ 48c",
        position_after="A: 10 filled @ 50c | B: 0 filled, 10 resting @ 49c",
        safety_check="resting+filled=10 <= unit(10), arb=99c < 100",
    )
    key = ProposalKey(event_ticker="EVT-1", side="A", kind="adjustment")
    proposal = Proposal(
        key=key,
        kind="adjustment",
        summary="Adjust A bid 48->49c",
        detail="Side A jumped from 48c to 49c, following.",
        created_at=now,
        adjustment=adj,
    )
    assert proposal.adjustment is not None
    assert proposal.bid is None
    assert proposal.stale is False
    assert proposal.stale_since is None
    assert proposal.kind == "adjustment"
    assert proposal.key == key
    assert proposal.created_at == now
