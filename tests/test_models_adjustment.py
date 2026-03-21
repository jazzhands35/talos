"""Tests for ProposedAdjustment model."""

from talos.models.adjustment import ProposedAdjustment


def test_proposed_adjustment_round_trips():
    pa = ProposedAdjustment(
        event_ticker="EVT-1",
        side="A",
        action="follow_jump",
        cancel_order_id="order-123",
        cancel_count=10,
        cancel_price=48,
        new_count=10,
        new_price=49,
        reason="jumped 48c->49c, arb profitable (49+50=99 < 100)",
        position_before="A: 10 filled @ 50c | B: 0 filled, 10 resting @ 48c",
        position_after="A: 10 filled @ 50c | B: 0 filled, 10 resting @ 49c",
        safety_check="resting+filled=10 <= unit(10), arb=99c < 100",
    )
    assert pa.side == "A"
    assert pa.new_price == 49
    assert pa.cancel_order_id == "order-123"


def test_proposed_adjustment_rejects_invalid_side():
    import pytest

    with pytest.raises(ValueError):
        ProposedAdjustment(
            event_ticker="EVT-1",
            side="C",  # type: ignore[arg-type]  # intentionally invalid
            action="follow_jump",
            cancel_order_id="order-123",
            cancel_count=10,
            cancel_price=48,
            new_count=10,
            new_price=49,
            reason="test",
            position_before="test",
            position_after="test",
            safety_check="test",
        )
