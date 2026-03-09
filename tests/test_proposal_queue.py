"""Tests for ProposalQueue pure state machine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from talos.models.adjustment import ProposedAdjustment
from talos.models.proposal import Proposal, ProposalKey, ProposedBid
from talos.proposal_queue import ProposalQueue

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_adj_proposal(
    event_ticker: str = "EVT-1",
    side: str = "A",
    cancel_price: int = 47,
    new_price: int = 48,
    order_id: str = "ord-1",
    now: datetime | None = None,
) -> Proposal:
    ts = now or datetime.now(UTC)
    key = ProposalKey(event_ticker=event_ticker, side=side, kind="adjustment")
    adj = ProposedAdjustment(
        event_ticker=event_ticker,
        side=side,  # type: ignore[arg-type]
        action="follow_jump",
        cancel_order_id=order_id,
        cancel_count=5,
        cancel_price=cancel_price,
        new_count=5,
        new_price=new_price,
        reason="test",
        position_before="0",
        position_after="0",
        safety_check="ok",
    )
    return Proposal(
        key=key,
        kind="adjustment",
        summary=f"Adjust {side} {cancel_price}->{new_price}",
        detail="test detail",
        created_at=ts,
        adjustment=adj,
    )


def _make_bid_proposal(
    event_ticker: str = "EVT-1",
    now: datetime | None = None,
) -> Proposal:
    ts = now or datetime.now(UTC)
    key = ProposalKey(event_ticker=event_ticker, side="", kind="bid")
    bid = ProposedBid(
        event_ticker=event_ticker,
        ticker_a=f"{event_ticker}-A",
        ticker_b=f"{event_ticker}-B",
        no_a=45,
        no_b=50,
        qty=10,
        edge_cents=3.0,
        stable_for_seconds=30.0,
        reason="test bid",
    )
    return Proposal(
        key=key,
        kind="bid",
        summary="New arb bid",
        detail="test detail",
        created_at=ts,
        bid=bid,
    )


# ---------------------------------------------------------------------------
# TestAdd
# ---------------------------------------------------------------------------


class TestAdd:
    def test_add_single(self) -> None:
        q = ProposalQueue()
        p = _make_adj_proposal()
        q.add(p)
        assert len(q) == 1
        assert q.has_pending(p.key)

    def test_add_multiple_different_keys(self) -> None:
        q = ProposalQueue()
        p1 = _make_adj_proposal(side="A")
        p2 = _make_adj_proposal(side="B")
        q.add(p1)
        q.add(p2)
        assert len(q) == 2

    def test_supersede_same_key(self) -> None:
        q = ProposalQueue()
        old = _make_adj_proposal(cancel_price=47, new_price=48)
        new = _make_adj_proposal(cancel_price=48, new_price=50)
        q.add(old)
        q.add(new)
        assert len(q) == 1
        # The pending proposal should be the newer one
        pending = q.pending()
        assert len(pending) == 1
        assert pending[0].adjustment is not None
        assert pending[0].adjustment.new_price == 50


# ---------------------------------------------------------------------------
# TestApproveReject
# ---------------------------------------------------------------------------


class TestApproveReject:
    def test_approve_removes_and_returns(self) -> None:
        q = ProposalQueue()
        p = _make_adj_proposal()
        q.add(p)
        result = q.approve(p.key)
        assert result.key == p.key
        assert len(q) == 0

    def test_approve_missing_raises_key_error(self) -> None:
        q = ProposalQueue()
        missing_key = ProposalKey(event_ticker="NOPE", side="A", kind="adjustment")
        with pytest.raises(KeyError):
            q.approve(missing_key)

    def test_reject_removes(self) -> None:
        q = ProposalQueue()
        p = _make_adj_proposal()
        q.add(p)
        q.reject(p.key)
        assert len(q) == 0
        assert not q.has_pending(p.key)

    def test_reject_missing_no_error(self) -> None:
        q = ProposalQueue()
        missing_key = ProposalKey(event_ticker="NOPE", side="A", kind="adjustment")
        q.reject(missing_key)  # should not raise


# ---------------------------------------------------------------------------
# TestStaleness
# ---------------------------------------------------------------------------


class TestStaleness:
    def test_tick_marks_stale_when_order_gone(self) -> None:
        q = ProposalQueue()
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        p = _make_adj_proposal(order_id="ord-1", now=t0)
        q.add(p)

        q.tick(active_order_ids=set(), now=t0)

        pending = q.pending()
        assert len(pending) == 1
        assert pending[0].stale is True
        assert pending[0].stale_since == t0

    def test_tick_does_not_mark_stale_when_order_present(self) -> None:
        q = ProposalQueue()
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        p = _make_adj_proposal(order_id="ord-1", now=t0)
        q.add(p)

        q.tick(active_order_ids={"ord-1"}, now=t0)

        pending = q.pending()
        assert len(pending) == 1
        assert pending[0].stale is False

    def test_stale_removed_after_grace_period(self) -> None:
        q = ProposalQueue(staleness_grace_seconds=5.0)
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        p = _make_adj_proposal(order_id="ord-1", now=t0)
        q.add(p)

        # First tick: mark stale
        q.tick(active_order_ids=set(), now=t0)
        assert len(q) == 1

        # Second tick: still within grace
        t1 = t0 + timedelta(seconds=4)
        q.tick(active_order_ids=set(), now=t1)
        assert len(q) == 1

        # Third tick: past grace period — removed
        t2 = t0 + timedelta(seconds=6)
        q.tick(active_order_ids=set(), now=t2)
        assert len(q) == 0

    def test_stale_cleared_when_order_reappears(self) -> None:
        q = ProposalQueue()
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        p = _make_adj_proposal(order_id="ord-1", now=t0)
        q.add(p)

        # Mark stale
        q.tick(active_order_ids=set(), now=t0)
        assert q.pending()[0].stale is True

        # Order reappears — stale cleared
        t1 = t0 + timedelta(seconds=1)
        q.tick(active_order_ids={"ord-1"}, now=t1)
        pending = q.pending()
        assert pending[0].stale is False
        assert pending[0].stale_since is None

    def test_bid_proposals_not_checked_against_orders(self) -> None:
        q = ProposalQueue()
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        p = _make_bid_proposal(now=t0)
        q.add(p)

        # Tick with empty order set — bid should NOT become stale
        q.tick(active_order_ids=set(), now=t0)
        pending = q.pending()
        assert len(pending) == 1
        assert pending[0].stale is False


# ---------------------------------------------------------------------------
# TestOrdering
# ---------------------------------------------------------------------------


class TestOrdering:
    def test_pending_ordered_oldest_first(self) -> None:
        q = ProposalQueue()
        t1 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        t2 = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
        t3 = datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC)

        # Add in non-chronological order
        p2 = _make_adj_proposal(side="B", now=t2)
        p3 = _make_bid_proposal(now=t3)
        p1 = _make_adj_proposal(side="A", now=t1)

        q.add(p2)
        q.add(p3)
        q.add(p1)

        pending = q.pending()
        assert len(pending) == 3
        assert pending[0].created_at == t1
        assert pending[1].created_at == t2
        assert pending[2].created_at == t3
