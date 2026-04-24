# Position-Aware Bid Adjustment — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a position-aware bid adjustment system that tracks positions per event, enforces safety invariants structurally, and proposes bid adjustments when resting orders get jumped.

**Architecture:** Pure state machine (`PositionLedger`) + async orchestrator (`BidAdjuster`) following the established Principle 13 split. PositionLedger replaces `compute_event_positions()` as the single source of truth for both UI and safety gates. BidAdjuster receives jump events from `TopOfMarketTracker`, queries the ledger, and proposes cancel-then-place adjustments for human approval.

**Tech Stack:** Python 3.12+, Pydantic v2, structlog, pytest, httpx (async REST client)

**Design Doc:** `docs/plans/2026-03-07-position-aware-bid-adjustment-design.md`

**Safety Principles:** Read `brain/principles.md` Principles 15–19 before touching any code. These are non-negotiable.

**Verification Skills:** After completing each task that touches position/order code:
- Run `safety-audit` skill to verify structural invariants (D1–D6)
- Run `position-scenarios` skill after Tasks 1–4 are complete to verify behavioral correctness (S1–S8)

---

## Task 1: ProposedAdjustment Model

**Files:**
- Create: `src/talos/models/adjustment.py`
- Test: `tests/test_models_adjustment.py`

**Step 1: Write the test**

```python
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
            side="C",  # invalid
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
```

**Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_models_adjustment.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'talos.models.adjustment'`

**Step 3: Write minimal implementation**

```python
"""Pydantic model for proposed bid adjustments."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ProposedAdjustment(BaseModel):
    """A proposed bid adjustment for operator approval.

    Contains all context needed for an informed approve/reject decision.
    """

    event_ticker: str
    side: Literal["A", "B"]
    action: Literal["follow_jump"]
    cancel_order_id: str
    cancel_count: int
    cancel_price: int
    new_count: int
    new_price: int
    reason: str
    position_before: str
    position_after: str
    safety_check: str
```

**Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_models_adjustment.py -v`
Expected: 2 passed

**Step 5: Commit**

```bash
git add src/talos/models/adjustment.py tests/test_models_adjustment.py
git commit -m "feat: add ProposedAdjustment model for bid adjustment proposals"
```

---

## Task 2: PositionLedger — Core State Tracking

The heart of the system. Pure state machine, no I/O, no async.

**Files:**
- Create: `src/talos/position_ledger.py`
- Create: `tests/test_position_ledger.py`

**Reference:** `src/talos/fees.py` for `fee_adjusted_cost()`, `scenario_pnl()`, `fee_adjusted_edge()`

**Step 1: Write failing tests for per-side state tracking**

```python
"""Tests for PositionLedger — pure state machine for position tracking."""

import pytest

from talos.position_ledger import PositionLedger, Side


class TestBasicTracking:
    def test_initial_state_is_empty(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        assert ledger.filled_count(Side.A) == 0
        assert ledger.filled_count(Side.B) == 0
        assert ledger.resting_count(Side.A) == 0
        assert ledger.resting_count(Side.B) == 0
        assert ledger.resting_order_id(Side.A) is None
        assert ledger.resting_order_id(Side.B) is None

    def test_record_fill(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=5, price=50)
        assert ledger.filled_count(Side.A) == 5
        assert ledger.filled_total_cost(Side.A) == 250  # 5 * 50
        assert ledger.avg_filled_price(Side.A) == 50.0

    def test_record_multiple_fills_accumulate(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=5, price=50)
        ledger.record_fill(Side.A, count=5, price=52)
        assert ledger.filled_count(Side.A) == 10
        assert ledger.filled_total_cost(Side.A) == 510  # 250 + 260
        assert ledger.avg_filled_price(Side.A) == 51.0

    def test_record_resting(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_resting(Side.A, order_id="ord-1", count=10, price=48)
        assert ledger.resting_count(Side.A) == 10
        assert ledger.resting_order_id(Side.A) == "ord-1"
        assert ledger.resting_price(Side.A) == 48

    def test_record_cancel(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_resting(Side.A, order_id="ord-1", count=10, price=48)
        ledger.record_cancel(Side.A, order_id="ord-1")
        assert ledger.resting_count(Side.A) == 0
        assert ledger.resting_order_id(Side.A) is None

    def test_cancel_wrong_order_id_raises(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_resting(Side.A, order_id="ord-1", count=10, price=48)
        with pytest.raises(ValueError, match="order_id mismatch"):
            ledger.record_cancel(Side.A, order_id="ord-999")


class TestDerivedQueries:
    def test_total_committed(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=6, price=50)
        ledger.record_resting(Side.A, order_id="ord-1", count=4, price=48)
        assert ledger.total_committed(Side.A) == 10

    def test_current_delta(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=10, price=50)
        ledger.record_fill(Side.B, count=6, price=48)
        assert ledger.current_delta() == 4  # abs(10 - 6)

    def test_unit_remaining_no_fills(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        assert ledger.unit_remaining(Side.A) == 10

    def test_unit_remaining_partial_fill(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=6, price=50)
        assert ledger.unit_remaining(Side.A) == 4

    def test_is_unit_complete(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=9, price=50)
        assert not ledger.is_unit_complete(Side.A)
        ledger.record_fill(Side.A, count=1, price=51)
        assert ledger.is_unit_complete(Side.A)

    def test_both_sides_complete(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=10, price=50)
        assert not ledger.both_sides_complete()
        ledger.record_fill(Side.B, count=10, price=48)
        assert ledger.both_sides_complete()

    def test_avg_filled_price_no_fills_returns_zero(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        assert ledger.avg_filled_price(Side.A) == 0.0

    def test_reset_pair(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=10, price=50)
        ledger.record_fill(Side.B, count=10, price=48)
        ledger.reset_pair()
        assert ledger.filled_count(Side.A) == 0
        assert ledger.filled_count(Side.B) == 0
        assert ledger.resting_order_id(Side.A) is None
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Write implementation**

```python
"""PositionLedger — pure state machine for per-event position tracking.

Single source of truth for filled counts, resting orders, avg prices,
and safety gates. One instance per active event. No I/O, no async.

See brain/principles.md Principles 15-19 for safety invariants.
"""

from __future__ import annotations

from enum import Enum

import structlog

from talos.fees import fee_adjusted_cost

logger = structlog.get_logger()


class Side(Enum):
    A = "A"
    B = "B"

    @property
    def other(self) -> Side:
        return Side.B if self is Side.A else Side.A


class _SideState:
    """Mutable per-side position state."""

    __slots__ = (
        "filled_count",
        "filled_total_cost",
        "resting_order_id",
        "resting_count",
        "resting_price",
    )

    def __init__(self) -> None:
        self.filled_count: int = 0
        self.filled_total_cost: int = 0
        self.resting_order_id: str | None = None
        self.resting_count: int = 0
        self.resting_price: int = 0

    def reset(self) -> None:
        self.filled_count = 0
        self.filled_total_cost = 0
        self.resting_order_id = None
        self.resting_count = 0
        self.resting_price = 0


class PositionLedger:
    """Per-event position ledger — the single source of truth.

    Tracks filled and resting state per side, enforces safety gates,
    and provides position projections. Replaces compute_event_positions()
    for both UI display and bid adjustment safety.
    """

    def __init__(self, event_ticker: str, unit_size: int = 10) -> None:
        self.event_ticker = event_ticker
        self.unit_size = unit_size
        self._sides: dict[Side, _SideState] = {
            Side.A: _SideState(),
            Side.B: _SideState(),
        }
        self._discrepancy: str | None = None

    # ── Per-side accessors ──────────────────────────────────────────

    def filled_count(self, side: Side) -> int:
        return self._sides[side].filled_count

    def filled_total_cost(self, side: Side) -> int:
        return self._sides[side].filled_total_cost

    def resting_order_id(self, side: Side) -> str | None:
        return self._sides[side].resting_order_id

    def resting_count(self, side: Side) -> int:
        return self._sides[side].resting_count

    def resting_price(self, side: Side) -> int:
        return self._sides[side].resting_price

    # ── Derived queries ─────────────────────────────────────────────

    def avg_filled_price(self, side: Side) -> float:
        s = self._sides[side]
        if s.filled_count == 0:
            return 0.0
        return s.filled_total_cost / s.filled_count

    def total_committed(self, side: Side) -> int:
        s = self._sides[side]
        return s.filled_count + s.resting_count

    def current_delta(self) -> int:
        return abs(self.total_committed(Side.A) - self.total_committed(Side.B))

    def unit_remaining(self, side: Side) -> int:
        s = self._sides[side]
        filled_in_unit = s.filled_count % self.unit_size
        if filled_in_unit == 0 and s.filled_count > 0:
            return 0  # unit is complete
        return self.unit_size - filled_in_unit

    def is_unit_complete(self, side: Side) -> bool:
        s = self._sides[side]
        return s.filled_count > 0 and s.filled_count % self.unit_size == 0

    def both_sides_complete(self) -> bool:
        return self.is_unit_complete(Side.A) and self.is_unit_complete(Side.B)

    @property
    def has_discrepancy(self) -> bool:
        return self._discrepancy is not None

    @property
    def discrepancy(self) -> str | None:
        return self._discrepancy

    # ── Safety gate ─────────────────────────────────────────────────

    def is_placement_safe(
        self, side: Side, count: int, price: int
    ) -> tuple[bool, str]:
        """Check if placing an order is safe. Returns (ok, reason).

        Enforces Principles 16 (unit gating), 18 (profitability gate).
        """
        if self._discrepancy is not None:
            return False, f"ledger has unresolved discrepancy: {self._discrepancy}"

        s = self._sides[side]

        # P16: resting + filled + new must not exceed unit
        if s.filled_count + s.resting_count + count > self.unit_size:
            return (
                False,
                f"would exceed unit: filled={s.filled_count} + "
                f"resting={s.resting_count} + new={count} > {self.unit_size}",
            )

        # P16: only one resting order per side
        if s.resting_order_id is not None:
            return False, f"order already resting on side {side.value}: {s.resting_order_id}"

        # P18: fee-adjusted profitability
        other = self._sides[side.other]
        if other.filled_count > 0:
            other_price = other.filled_total_cost / other.filled_count
        elif other.resting_count > 0:
            # Use top-of-market (worst case) — but we only have resting_price here.
            # The BidAdjuster is responsible for passing the book top price
            # when the other side has no fills. For now, use resting price.
            other_price = other.resting_price
        else:
            # No position on the other side — can't check arb yet, allow placement
            return True, ""

        # Fee-adjusted: effective cost = price + (100 - price) * fee_rate
        effective_this = fee_adjusted_cost(price)
        effective_other = fee_adjusted_cost(int(round(other_price)))
        if effective_this + effective_other >= 100:
            return (
                False,
                f"arb not profitable after fees: "
                f"{effective_this:.2f} + {effective_other:.2f} = "
                f"{effective_this + effective_other:.2f} >= 100",
            )

        return True, ""

    # ── State mutations ─────────────────────────────────────────────

    def record_fill(self, side: Side, count: int, price: int) -> None:
        """Record a fill. Called when polling detects new fills."""
        s = self._sides[side]
        s.filled_count += count
        s.filled_total_cost += price * count
        # If resting order filled partially/fully, reduce resting count
        if s.resting_count > 0:
            filled_from_resting = min(count, s.resting_count)
            s.resting_count -= filled_from_resting
            if s.resting_count == 0:
                s.resting_order_id = None

    def record_resting(
        self, side: Side, order_id: str, count: int, price: int
    ) -> None:
        """Record a new resting order. Called after order placement confirmed."""
        s = self._sides[side]
        s.resting_order_id = order_id
        s.resting_count = count
        s.resting_price = price

    def record_cancel(self, side: Side, order_id: str) -> None:
        """Record an order cancellation."""
        s = self._sides[side]
        if s.resting_order_id != order_id:
            raise ValueError(
                f"order_id mismatch: expected {s.resting_order_id}, got {order_id}"
            )
        s.resting_order_id = None
        s.resting_count = 0
        s.resting_price = 0

    def reset_pair(self) -> None:
        """Clear state after both sides complete. Ready for next pair."""
        self._sides[Side.A].reset()
        self._sides[Side.B].reset()
        self._discrepancy = None

    def sync_from_orders(
        self, orders: list, ticker_a: str, ticker_b: str
    ) -> None:
        """Reconcile ledger against polled order state from Kalshi.

        This is the safety net (Principle 15). Called every polling cycle.
        On mismatch: sets discrepancy flag, halting all proposals.
        Does NOT silently correct — flags and waits for operator.

        Args:
            orders: list of Order objects from REST polling
            ticker_a: the ticker for side A of this event's pair
            ticker_b: the ticker for side B of this event's pair
        """
        from talos.models.order import ACTIVE_STATUSES

        ticker_to_side = {ticker_a: Side.A, ticker_b: Side.B}
        # Accumulate what Kalshi reports
        kalshi_filled: dict[Side, int] = {Side.A: 0, Side.B: 0}
        kalshi_fill_cost: dict[Side, int] = {Side.A: 0, Side.B: 0}
        kalshi_resting: dict[Side, list[tuple[str, int, int]]] = {
            Side.A: [],
            Side.B: [],
        }

        for order in orders:
            if order.side != "no" or order.action != "buy":
                continue
            if order.status not in ACTIVE_STATUSES:
                continue
            side = ticker_to_side.get(order.ticker)
            if side is None:
                continue
            kalshi_filled[side] += order.fill_count
            kalshi_fill_cost[side] += order.no_price * order.fill_count
            if order.remaining_count > 0:
                kalshi_resting[side].append(
                    (order.order_id, order.remaining_count, order.no_price)
                )

        # Check for discrepancies
        problems: list[str] = []
        for side in (Side.A, Side.B):
            s = self._sides[side]

            # Check filled count
            if kalshi_filled[side] != s.filled_count:
                problems.append(
                    f"side {side.value} filled: ledger={s.filled_count}, "
                    f"kalshi={kalshi_filled[side]}"
                )

            # Check resting orders — should be 0 or 1
            resting_list = kalshi_resting[side]
            if len(resting_list) > 1:
                problems.append(
                    f"side {side.value} has {len(resting_list)} resting orders "
                    f"(expected 0 or 1)"
                )
            elif len(resting_list) == 1:
                oid, cnt, price = resting_list[0]
                if s.resting_order_id is not None and s.resting_order_id != oid:
                    problems.append(
                        f"side {side.value} resting order_id: "
                        f"ledger={s.resting_order_id}, kalshi={oid}"
                    )
            elif len(resting_list) == 0 and s.resting_order_id is not None:
                # Ledger thinks there's a resting order, Kalshi says none
                # This could be a fill that hasn't been processed yet
                problems.append(
                    f"side {side.value}: ledger has resting order "
                    f"{s.resting_order_id}, kalshi shows none"
                )

        if problems:
            msg = "; ".join(problems)
            self._discrepancy = msg
            logger.warning(
                "position_ledger_discrepancy",
                event=self.event_ticker,
                problems=problems,
            )
        else:
            # Clear any previous discrepancy — state is consistent
            self._discrepancy = None

            # Sync resting state from Kalshi (authoritative) when consistent
            for side in (Side.A, Side.B):
                s = self._sides[side]
                resting_list = kalshi_resting[side]
                s.filled_count = kalshi_filled[side]
                s.filled_total_cost = kalshi_fill_cost[side]
                if resting_list:
                    oid, cnt, price = resting_list[0]
                    s.resting_order_id = oid
                    s.resting_count = cnt
                    s.resting_price = price
                else:
                    s.resting_order_id = None
                    s.resting_count = 0
                    s.resting_price = 0

    def format_position(self, side: Side) -> str:
        """Human-readable position string for proposals."""
        s = self._sides[side]
        parts: list[str] = []
        if s.filled_count > 0:
            avg = self.avg_filled_price(side)
            parts.append(f"{s.filled_count} filled @ {avg:.1f}c")
        if s.resting_count > 0:
            parts.append(f"{s.resting_count} resting @ {s.resting_price}c")
        return ", ".join(parts) if parts else "empty"
```

**Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py -v`
Expected: All passed

**Step 5: Commit**

```bash
git add src/talos/position_ledger.py tests/test_position_ledger.py
git commit -m "feat: add PositionLedger pure state machine for per-event position tracking"
```

---

## Task 3: PositionLedger — Safety Gate Tests

**Files:**
- Modify: `tests/test_position_ledger.py`

**Step 1: Add safety gate tests**

```python
class TestSafetyGate:
    def test_rejects_exceeding_unit(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=6, price=50)
        ledger.record_resting(Side.A, order_id="ord-1", count=4, price=48)
        ok, reason = ledger.is_placement_safe(Side.A, count=1, price=47)
        assert not ok
        assert "exceed unit" in reason

    def test_rejects_duplicate_resting(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_resting(Side.A, order_id="ord-1", count=10, price=48)
        ok, reason = ledger.is_placement_safe(Side.A, count=10, price=49)
        assert not ok
        assert "already resting" in reason

    def test_rejects_unprofitable_arb_with_fills(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=10, price=50)
        # At 50c each side: fee_adjusted_cost(50) = 50 + 50*0.0175 = 50.875
        # 50.875 + 50.875 = 101.75 >= 100 → unprofitable
        ok, reason = ledger.is_placement_safe(Side.B, count=10, price=50)
        assert not ok
        assert "not profitable" in reason

    def test_accepts_profitable_arb(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=10, price=50)
        # At 48c: fee_adjusted_cost(48) = 48 + 52*0.0175 = 48.91
        # 50.875 + 48.91 = 99.785 < 100 → profitable
        ok, reason = ledger.is_placement_safe(Side.B, count=10, price=48)
        assert ok
        assert reason == ""

    def test_allows_placement_when_other_side_empty(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ok, reason = ledger.is_placement_safe(Side.A, count=10, price=50)
        assert ok

    def test_fractional_completion_within_unit(self):
        """6 filled + 4 new = 10 = unit_size → allowed."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=10, price=50)  # other side
        ledger.record_fill(Side.B, count=6, price=48)
        ok, reason = ledger.is_placement_safe(Side.B, count=4, price=49)
        assert ok

    def test_fractional_completion_exceeds_unit(self):
        """6 filled + 5 new = 11 > unit_size → rejected."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=10, price=50)
        ledger.record_fill(Side.B, count=6, price=48)
        ok, reason = ledger.is_placement_safe(Side.B, count=5, price=49)
        assert not ok
        assert "exceed unit" in reason

    def test_rejects_when_discrepancy_exists(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger._discrepancy = "test mismatch"
        ok, reason = ledger.is_placement_safe(Side.A, count=10, price=50)
        assert not ok
        assert "discrepancy" in reason
```

**Step 2: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py -v`
Expected: All passed (implementation already handles these)

**Step 3: Commit**

```bash
git add tests/test_position_ledger.py
git commit -m "test: add safety gate tests for PositionLedger invariants"
```

---

## Task 4: PositionLedger — Reconciliation (sync_from_orders)

**Files:**
- Modify: `tests/test_position_ledger.py`

**Step 1: Add reconciliation tests**

```python
from talos.models.order import Order


def _make_order(
    ticker: str,
    fill_count: int = 0,
    remaining_count: int = 0,
    no_price: int = 50,
    order_id: str = "ord-1",
    status: str = "resting",
) -> Order:
    return Order(
        order_id=order_id,
        ticker=ticker,
        action="buy",
        side="no",
        no_price=no_price,
        fill_count=fill_count,
        remaining_count=remaining_count,
        initial_count=fill_count + remaining_count,
        status=status,
    )


class TestReconciliation:
    def test_sync_matching_state_no_discrepancy(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=5, price=50)
        ledger.record_resting(Side.A, order_id="ord-a", count=5, price=50)
        orders = [
            _make_order("TK-A", fill_count=5, remaining_count=5, order_id="ord-a"),
        ]
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        assert not ledger.has_discrepancy

    def test_sync_fill_count_mismatch_flags(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=5, price=50)
        # Kalshi says 8 filled — mismatch
        orders = [
            _make_order("TK-A", fill_count=8, remaining_count=2, order_id="ord-a"),
        ]
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        assert ledger.has_discrepancy
        assert "filled" in ledger.discrepancy

    def test_sync_multiple_resting_orders_flags(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        orders = [
            _make_order("TK-A", fill_count=0, remaining_count=10, order_id="ord-1"),
            _make_order("TK-A", fill_count=0, remaining_count=10, order_id="ord-2"),
        ]
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        assert ledger.has_discrepancy
        assert "2 resting orders" in ledger.discrepancy

    def test_discrepancy_blocks_placement(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        orders = [
            _make_order("TK-A", fill_count=0, remaining_count=10, order_id="ord-1"),
            _make_order("TK-A", fill_count=0, remaining_count=10, order_id="ord-2"),
        ]
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        ok, reason = ledger.is_placement_safe(Side.B, count=10, price=48)
        assert not ok
        assert "discrepancy" in reason

    def test_sync_clears_discrepancy_when_state_matches(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger._discrepancy = "old problem"
        orders = []  # no orders = no fills, no resting = matches empty ledger
        ledger.sync_from_orders(orders, ticker_a="TK-A", ticker_b="TK-B")
        assert not ledger.has_discrepancy
```

**Step 2: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py -v`
Expected: All passed

**Step 3: Commit**

```bash
git add tests/test_position_ledger.py
git commit -m "test: add reconciliation tests for PositionLedger sync_from_orders"
```

---

## Task 5: BidAdjuster — Core Decision Logic

The async orchestrator that receives jump events and proposes adjustments.

**Files:**
- Create: `src/talos/bid_adjuster.py`
- Create: `tests/test_bid_adjuster.py`

**Reference:**
- `src/talos/top_of_market.py` — `TopOfMarketTracker.on_change` callback signature: `(ticker: str, at_top: bool)`
- `src/talos/orderbook.py` — `OrderBookManager.best_ask(ticker)` returns `OrderBookLevel | None` with `.price` attr
- `src/talos/rest_client.py` — `create_order`, `cancel_order` signatures
- `src/talos/scanner.py` — `ArbitrageScanner._pairs_by_ticker` for ticker→pair lookup

**Step 1: Write failing tests for decision logic**

```python
"""Tests for BidAdjuster — async orchestrator for bid adjustment."""

import pytest

from talos.bid_adjuster import BidAdjuster
from talos.position_ledger import PositionLedger, Side
from talos.models.adjustment import ProposedAdjustment
from talos.models.strategy import ArbPair


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


class TestDecisionLogic:
    def setup_method(self):
        self.pair = ArbPair(
            event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B"
        )
        self.books = FakeBookManager({"TK-A": 50, "TK-B": 49})
        self.adjuster = BidAdjuster(
            book_manager=self.books,
            pairs=[self.pair],
            unit_size=10,
        )

    def test_jump_on_profitable_side_emits_proposal(self):
        ledger = self.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=10, price=50)
        ledger.record_resting(Side.B, order_id="ord-b", count=10, price=47)
        # Side B jumped from 47 to 49 — still profitable: 49 + 50 < 100
        proposal = self.adjuster.evaluate_jump("TK-B", at_top=False)
        assert proposal is not None
        assert proposal.side == "B"
        assert proposal.new_price == 49
        assert proposal.cancel_order_id == "ord-b"
        assert proposal.new_count == 10

    def test_jump_on_unprofitable_side_no_proposal(self):
        ledger = self.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=10, price=50)
        ledger.record_resting(Side.B, order_id="ord-b", count=10, price=47)
        # Top of market moved to 51 — unprofitable: 51 + 50 > 100
        self.books._prices["TK-B"] = 51
        proposal = self.adjuster.evaluate_jump("TK-B", at_top=False)
        assert proposal is None

    def test_back_at_top_no_proposal(self):
        ledger = self.adjuster.get_ledger("EVT-1")
        ledger.record_resting(Side.B, order_id="ord-b", count=10, price=49)
        proposal = self.adjuster.evaluate_jump("TK-B", at_top=True)
        assert proposal is None

    def test_no_resting_order_no_proposal(self):
        # No resting order on side B — nothing to adjust
        proposal = self.adjuster.evaluate_jump("TK-B", at_top=False)
        assert proposal is None

    def test_fractional_completion_proposal(self):
        ledger = self.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=10, price=50)
        ledger.record_fill(Side.B, count=6, price=32)
        ledger.record_resting(Side.B, order_id="ord-b", count=4, price=32)
        # Jumped to 33 — propose 4 contracts at 33
        self.books._prices["TK-B"] = 33
        proposal = self.adjuster.evaluate_jump("TK-B", at_top=False)
        assert proposal is not None
        assert proposal.new_count == 4
        assert proposal.new_price == 33

    def test_unknown_ticker_no_proposal(self):
        proposal = self.adjuster.evaluate_jump("UNKNOWN", at_top=False)
        assert proposal is None


class TestDualJumpTiebreaker:
    def setup_method(self):
        self.pair = ArbPair(
            event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B"
        )
        self.books = FakeBookManager({"TK-A": 48, "TK-B": 33})
        self.adjuster = BidAdjuster(
            book_manager=self.books,
            pairs=[self.pair],
            unit_size=10,
        )

    def test_most_behind_side_goes_first(self):
        ledger = self.adjuster.get_ledger("EVT-1")
        # A: 3 filled, 7 resting → needs 7 more
        # B: 6 filled, 4 resting → needs 4 more
        ledger.record_fill(Side.A, count=3, price=47)
        ledger.record_resting(Side.A, order_id="ord-a", count=7, price=47)
        ledger.record_fill(Side.B, count=6, price=32)
        ledger.record_resting(Side.B, order_id="ord-b", count=4, price=32)

        # Both jumped — A should go first (needs 7 > B's 4)
        self.adjuster.evaluate_jump("TK-A", at_top=False)
        self.adjuster.evaluate_jump("TK-B", at_top=False)

        # Only A's proposal should be active, B should be deferred
        assert self.adjuster.has_pending_proposal("EVT-1", Side.A)
        assert not self.adjuster.has_pending_proposal("EVT-1", Side.B)
        assert self.adjuster.has_deferred("EVT-1", Side.B)

    def test_deferred_side_reevaluated_on_completion(self):
        ledger = self.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=3, price=47)
        ledger.record_resting(Side.A, order_id="ord-a", count=7, price=47)
        ledger.record_fill(Side.B, count=6, price=32)
        ledger.record_resting(Side.B, order_id="ord-b", count=4, price=32)

        # Both jumped
        self.adjuster.evaluate_jump("TK-A", at_top=False)
        self.adjuster.evaluate_jump("TK-B", at_top=False)

        # Simulate A completing — clear A's resting and fill it
        ledger.record_cancel(Side.A, "ord-a")
        ledger.record_fill(Side.A, count=7, price=48)

        # Notify adjuster that A is complete — should re-evaluate B
        proposal = self.adjuster.on_side_complete("EVT-1", Side.A)
        assert proposal is not None
        assert proposal.side == "B"
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_bid_adjuster.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Write implementation**

```python
"""BidAdjuster — async orchestrator for bid adjustment on jumps.

Receives jump events from TopOfMarketTracker, queries PositionLedger
for current state, and proposes cancel-then-place adjustments.

See brain/principles.md Principles 15-19 for safety invariants.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog

from talos.fees import fee_adjusted_cost
from talos.models.adjustment import ProposedAdjustment
from talos.models.strategy import ArbPair
from talos.orderbook import OrderBookManager
from talos.position_ledger import PositionLedger, Side

logger = structlog.get_logger()


class BidAdjuster:
    """Proposes bid adjustments when resting orders get jumped.

    Pure decision logic (evaluate_jump) is synchronous and testable.
    Async execution (execute) is separated for the orchestrator layer.
    """

    def __init__(
        self,
        book_manager: OrderBookManager,
        pairs: list[ArbPair],
        unit_size: int = 10,
    ) -> None:
        self._books = book_manager
        self._unit_size = unit_size

        # Ticker → (pair, side) lookup
        self._ticker_map: dict[str, tuple[ArbPair, Side]] = {}
        for pair in pairs:
            self._ticker_map[pair.ticker_a] = (pair, Side.A)
            self._ticker_map[pair.ticker_b] = (pair, Side.B)

        # Per-event ledgers
        self._ledgers: dict[str, PositionLedger] = {}
        for pair in pairs:
            self._ledgers[pair.event_ticker] = PositionLedger(
                event_ticker=pair.event_ticker, unit_size=unit_size
            )

        # Pending proposals: event_ticker → {side → proposal}
        self._proposals: dict[str, dict[Side, ProposedAdjustment]] = {}

        # Deferred jumps: event_ticker → set of deferred sides
        self._deferred: dict[str, set[Side]] = {}

        # Callback for emitting proposals to the UI
        self.on_proposal: Callable[[ProposedAdjustment], None] | None = None

    def get_ledger(self, event_ticker: str) -> PositionLedger:
        """Get the position ledger for an event."""
        return self._ledgers[event_ticker]

    def add_event(self, pair: ArbPair) -> None:
        """Register a new event pair."""
        self._ticker_map[pair.ticker_a] = (pair, Side.A)
        self._ticker_map[pair.ticker_b] = (pair, Side.B)
        self._ledgers[pair.event_ticker] = PositionLedger(
            event_ticker=pair.event_ticker, unit_size=self._unit_size
        )

    def remove_event(self, event_ticker: str) -> None:
        """Unregister an event pair."""
        self._ledgers.pop(event_ticker, None)
        self._proposals.pop(event_ticker, None)
        self._deferred.pop(event_ticker, None)
        # Clean ticker map
        to_remove = [
            t for t, (p, _) in self._ticker_map.items()
            if p.event_ticker == event_ticker
        ]
        for t in to_remove:
            del self._ticker_map[t]

    # ── Decision logic (synchronous, testable) ──────────────────────

    def evaluate_jump(
        self, ticker: str, at_top: bool
    ) -> ProposedAdjustment | None:
        """Evaluate a jump event and return a proposal if appropriate.

        Called by TopOfMarketTracker.on_change callback.
        Returns None if no action needed.
        """
        lookup = self._ticker_map.get(ticker)
        if lookup is None:
            return None

        pair, side = lookup

        # Back at top — nothing to do
        if at_top:
            # Clear any deferred for this side
            deferred = self._deferred.get(pair.event_ticker, set())
            deferred.discard(side)
            return None

        ledger = self._ledgers[pair.event_ticker]

        # No resting order on this side — nothing to adjust
        if ledger.resting_order_id(side) is None:
            return None

        # Get new top-of-market price
        best = self._books.best_ask(ticker)
        if best is None:
            return None
        new_price = best.price

        # If new price equals current resting price, no action needed
        if new_price <= ledger.resting_price(side):
            return None

        # Profitability check (Principle 18)
        other_side = side.other
        if ledger.filled_count(other_side) > 0:
            other_effective = fee_adjusted_cost(
                int(round(ledger.avg_filled_price(other_side)))
            )
        elif ledger.resting_count(other_side) > 0:
            # Use top-of-market for other side (worst case / most conservative)
            other_ticker = (
                pair.ticker_a if other_side is Side.A else pair.ticker_b
            )
            other_best = self._books.best_ask(other_ticker)
            other_book_price = other_best.price if other_best else ledger.resting_price(other_side)
            other_effective = fee_adjusted_cost(other_book_price)
        else:
            other_effective = 0.0

        this_effective = fee_adjusted_cost(new_price)
        if other_effective > 0 and this_effective + other_effective >= 100:
            logger.info(
                "jump_not_profitable",
                ticker=ticker,
                new_price=new_price,
                effective_sum=this_effective + other_effective,
            )
            return None

        # Dual-jump tiebreaker (Principle 19)
        other_ticker = pair.ticker_a if other_side is Side.A else pair.ticker_b
        other_jumped = self._is_jumped(other_ticker, ledger, other_side)
        if other_jumped:
            this_remaining = ledger.unit_remaining(side)
            other_remaining = ledger.unit_remaining(other_side)
            if this_remaining == 0:
                this_remaining = ledger.resting_count(side)
            if other_remaining == 0:
                other_remaining = ledger.resting_count(other_side)

            if this_remaining < other_remaining:
                # Other side is more behind — defer this side
                self._deferred.setdefault(pair.event_ticker, set()).add(side)
                logger.info(
                    "jump_deferred",
                    ticker=ticker,
                    side=side.value,
                    reason=f"other side needs {other_remaining} vs this side {this_remaining}",
                )
                return None

        # Build proposal
        cancel_id = ledger.resting_order_id(side)
        cancel_count = ledger.resting_count(side)
        cancel_price = ledger.resting_price(side)
        new_count = cancel_count  # same quantity at new price

        # Safety gate check (simulating the post-cancel state)
        # After cancel: resting will be 0. Then placing new_count.
        test_ok, test_reason = self._check_post_cancel_safety(
            ledger, side, new_count, new_price
        )
        if not test_ok:
            logger.info("jump_blocked_by_safety", ticker=ticker, reason=test_reason)
            return None

        proposal = ProposedAdjustment(
            event_ticker=pair.event_ticker,
            side=side.value,
            action="follow_jump",
            cancel_order_id=cancel_id,
            cancel_count=cancel_count,
            cancel_price=cancel_price,
            new_count=new_count,
            new_price=new_price,
            reason=(
                f"jumped {cancel_price}c->{new_price}c, "
                f"arb: {this_effective:.1f}+{other_effective:.1f}"
                f"={this_effective + other_effective:.1f} < 100"
            ),
            position_before=f"A: {ledger.format_position(Side.A)} | B: {ledger.format_position(Side.B)}",
            position_after=self._format_position_after(ledger, side, new_count, new_price),
            safety_check=(
                f"filled+new={ledger.filled_count(side)+new_count} <= "
                f"unit({ledger.unit_size}), "
                f"arb={this_effective + other_effective:.1f}c < 100"
            ),
        )

        # Store as pending (supersedes any existing proposal on this side)
        evt_proposals = self._proposals.setdefault(pair.event_ticker, {})
        old = evt_proposals.get(side)
        if old is not None:
            logger.info("proposal_superseded", event=pair.event_ticker, side=side.value)
        evt_proposals[side] = proposal

        # Clear deferred flag for this side
        deferred = self._deferred.get(pair.event_ticker, set())
        deferred.discard(side)

        if self.on_proposal:
            self.on_proposal(proposal)

        return proposal

    def on_side_complete(
        self, event_ticker: str, completed_side: Side
    ) -> ProposedAdjustment | None:
        """Called when a side's unit completes. Re-evaluates deferred jumps.

        Returns a proposal for the deferred side if still appropriate.
        """
        deferred = self._deferred.get(event_ticker, set())
        other = completed_side.other
        if other not in deferred:
            return None

        deferred.discard(other)

        # Find the ticker for the deferred side
        for ticker, (pair, side) in self._ticker_map.items():
            if pair.event_ticker == event_ticker and side is other:
                # Re-evaluate the jump
                return self.evaluate_jump(ticker, at_top=False)
        return None

    # ── Query methods ───────────────────────────────────────────────

    def has_pending_proposal(self, event_ticker: str, side: Side) -> bool:
        return side in self._proposals.get(event_ticker, {})

    def has_deferred(self, event_ticker: str, side: Side) -> bool:
        return side in self._deferred.get(event_ticker, set())

    def get_proposal(
        self, event_ticker: str, side: Side
    ) -> ProposedAdjustment | None:
        return self._proposals.get(event_ticker, {}).get(side)

    def clear_proposal(self, event_ticker: str, side: Side) -> None:
        """Clear a proposal after execution or rejection."""
        evt = self._proposals.get(event_ticker)
        if evt:
            evt.pop(side, None)

    # ── Internal helpers ────────────────────────────────────────────

    def _is_jumped(
        self, ticker: str, ledger: PositionLedger, side: Side
    ) -> bool:
        """Check if a side has been jumped (book price > resting price)."""
        if ledger.resting_order_id(side) is None:
            return False
        best = self._books.best_ask(ticker)
        if best is None:
            return False
        return best.price > ledger.resting_price(side)

    def _check_post_cancel_safety(
        self,
        ledger: PositionLedger,
        side: Side,
        new_count: int,
        new_price: int,
    ) -> tuple[bool, str]:
        """Check safety as if the existing resting order were already cancelled."""
        s = ledger._sides[side]
        # Simulate post-cancel state
        if s.filled_count + new_count > ledger.unit_size:
            return (
                False,
                f"would exceed unit after cancel: filled={s.filled_count} + "
                f"new={new_count} > {ledger.unit_size}",
            )
        # Check profitability (reuse the gate logic without resting check)
        other = ledger._sides[side.other]
        if other.filled_count > 0:
            other_price = other.filled_total_cost / other.filled_count
        elif other.resting_count > 0:
            other_price = other.resting_price
        else:
            return True, ""

        effective_this = fee_adjusted_cost(new_price)
        effective_other = fee_adjusted_cost(int(round(other_price)))
        if effective_this + effective_other >= 100:
            return (
                False,
                f"arb not profitable: {effective_this:.2f}+{effective_other:.2f} >= 100",
            )
        return True, ""

    def _format_position_after(
        self,
        ledger: PositionLedger,
        side: Side,
        new_count: int,
        new_price: int,
    ) -> str:
        """Format projected position string for proposals."""
        other = side.other
        this_label = side.value
        other_label = other.value
        # After cancel+place: resting changes, filled stays
        s = ledger._sides[side]
        this_parts: list[str] = []
        if s.filled_count > 0:
            avg = ledger.avg_filled_price(side)
            this_parts.append(f"{s.filled_count} filled @ {avg:.1f}c")
        this_parts.append(f"{new_count} resting @ {new_price}c")

        return (
            f"{this_label}: {', '.join(this_parts)} | "
            f"{other_label}: {ledger.format_position(other)}"
        )
```

**Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_bid_adjuster.py -v`
Expected: All passed

**Step 5: Commit**

```bash
git add src/talos/bid_adjuster.py tests/test_bid_adjuster.py
git commit -m "feat: add BidAdjuster orchestrator for jump-based bid adjustment proposals"
```

---

## Task 6: BidAdjuster — Async Execution via Amend

Add the async `execute()` method that uses the Kalshi amend API (Principle 17).

**Files:**
- Modify: `src/talos/bid_adjuster.py`
- Modify: `tests/test_bid_adjuster.py`

**Step 1: Write failing tests**

```python
import pytest
from unittest.mock import AsyncMock

from talos.models.order import Order


def _make_order(
    order_id: str, price: int, fill_count: int, remaining_count: int
) -> Order:
    return Order(
        order_id=order_id,
        ticker="TK-B",
        side="no",
        action="buy",
        no_price=price,
        status="resting",
        remaining_count=remaining_count,
        fill_count=fill_count,
        initial_count=fill_count + remaining_count,
    )


class TestAsyncExecution:
    @pytest.mark.asyncio
    async def test_execute_amends_order(self):
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        books = FakeBookManager({"TK-A": 50, "TK-B": 49})
        adjuster = BidAdjuster(book_manager=books, pairs=[pair], unit_size=10)

        ledger = adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=10, price=50)
        ledger.record_resting(Side.B, order_id="ord-b", count=10, price=47)

        proposal = adjuster.evaluate_jump("TK-B", at_top=False)
        assert proposal is not None

        old_order = _make_order("ord-b", price=47, fill_count=0, remaining_count=10)
        amended_order = _make_order("ord-b", price=49, fill_count=0, remaining_count=10)

        rest_client = AsyncMock()
        rest_client.amend_order.return_value = (old_order, amended_order)

        await adjuster.execute(proposal, rest_client)

        rest_client.amend_order.assert_called_once_with(
            "ord-b",
            ticker="TK-B",
            side="no",
            action="buy",
            no_price=49,
            count=10,
        )
        # Ledger should reflect amended state
        assert ledger.resting_order_id(Side.B) == "ord-b"
        assert ledger.resting_price(Side.B) == 49
        assert ledger.resting_count(Side.B) == 10

    @pytest.mark.asyncio
    async def test_execute_amend_with_partial_fill(self):
        """Amend a partially filled order — only unfilled portion moves."""
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        books = FakeBookManager({"TK-A": 50, "TK-B": 33})
        adjuster = BidAdjuster(book_manager=books, pairs=[pair], unit_size=10)

        ledger = adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=10, price=50)
        ledger.record_fill(Side.B, count=6, price=32)
        ledger.record_resting(Side.B, order_id="ord-b", count=4, price=32)

        proposal = adjuster.evaluate_jump("TK-B", at_top=False)
        assert proposal is not None
        assert proposal.new_count == 4

        old_order = _make_order("ord-b", price=32, fill_count=6, remaining_count=4)
        amended_order = _make_order("ord-b", price=33, fill_count=6, remaining_count=4)

        rest_client = AsyncMock()
        rest_client.amend_order.return_value = (old_order, amended_order)

        await adjuster.execute(proposal, rest_client)

        # count passed to amend = fill_count + remaining_count (total)
        rest_client.amend_order.assert_called_once_with(
            "ord-b",
            ticker="TK-B",
            side="no",
            action="buy",
            no_price=33,
            count=10,  # 6 filled + 4 remaining = 10 total
        )
        assert ledger.resting_price(Side.B) == 33
        assert ledger.resting_count(Side.B) == 4

    @pytest.mark.asyncio
    async def test_execute_amend_fails_halts(self):
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        books = FakeBookManager({"TK-A": 50, "TK-B": 49})
        adjuster = BidAdjuster(book_manager=books, pairs=[pair], unit_size=10)

        ledger = adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=10, price=50)
        ledger.record_resting(Side.B, order_id="ord-b", count=10, price=47)

        proposal = adjuster.evaluate_jump("TK-B", at_top=False)
        rest_client = AsyncMock()
        rest_client.amend_order.side_effect = Exception("API error")

        with pytest.raises(Exception, match="API error"):
            await adjuster.execute(proposal, rest_client)

        # Original order should still be in ledger (amend is atomic — failure = no change)
        assert ledger.resting_order_id(Side.B) == "ord-b"
        assert ledger.resting_price(Side.B) == 47
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_bid_adjuster.py::TestAsyncExecution -v`
Expected: FAIL — `AttributeError: 'BidAdjuster' object has no attribute 'execute'`

**Step 3: Add execute method to BidAdjuster**

Add this method to the `BidAdjuster` class in `src/talos/bid_adjuster.py`:

```python
    async def execute(
        self, proposal: ProposedAdjustment, rest_client: object
    ) -> None:
        """Execute a proposed adjustment via amend (Principle 17).

        Single atomic API call — changes price on existing order.
        On failure: halt immediately, flag operator. Do NOT fall back
        to cancel-then-place.

        Args:
            proposal: the approved ProposedAdjustment
            rest_client: KalshiRESTClient instance (typed as object for testability)
        """
        side = Side(proposal.side)
        ledger = self._ledgers[proposal.event_ticker]

        # Find the ticker for this side
        ticker = self._side_ticker(proposal.event_ticker, side)

        # Compute total count for amend API (fill_count + remaining_count)
        s = ledger._sides[side]
        total_count = s.filled_count + s.resting_count

        logger.info(
            "adjustment_amend",
            event_ticker=proposal.event_ticker,
            side=side.value,
            order_id=proposal.cancel_order_id,
            old_price=proposal.cancel_price,
            new_price=proposal.new_price,
            total_count=total_count,
        )

        # Single atomic amend call
        _old_order, amended_order = await rest_client.amend_order(  # type: ignore[attr-defined]
            proposal.cancel_order_id,
            ticker=ticker,
            side="no",
            action="buy",
            no_price=proposal.new_price,
            count=total_count,
        )

        # Update ledger from amend response
        ledger.record_resting(
            side,
            order_id=amended_order.order_id,
            count=amended_order.remaining_count,
            price=amended_order.no_price,
        )

        # Clear the proposal
        self.clear_proposal(proposal.event_ticker, side)

        logger.info(
            "adjustment_complete",
            event_ticker=proposal.event_ticker,
            side=side.value,
            order_id=amended_order.order_id,
            new_price=proposal.new_price,
        )

    def _side_ticker(self, event_ticker: str, side: Side) -> str:
        """Look up the market ticker for a given event + side."""
        for ticker, (pair, s) in self._ticker_map.items():
            if pair.event_ticker == event_ticker and s is side:
                return ticker
        raise ValueError(f"No ticker found for {event_ticker} side {side.value}")
```

**Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_bid_adjuster.py -v`
Expected: All passed

**Step 5: Commit**

```bash
git add src/talos/bid_adjuster.py tests/test_bid_adjuster.py
git commit -m "feat: add async execute() to BidAdjuster with cancel-then-place sequencing"
```

---

## Task 7: Wire Into TalosApp

Connect the BidAdjuster to the existing TUI — wire callbacks, feed polling data to ledgers, show proposals for approval.

**Files:**
- Modify: `src/talos/ui/app.py`

**This task is NOT TDD** — it's UI integration that requires manual testing with the running app. Follow existing patterns in `app.py`.

**Step 1: Add BidAdjuster as optional dependency**

In `TalosApp.__init__`, add `adjuster: BidAdjuster | None = None` parameter. Wire `TopOfMarketTracker.on_change` to route through both the toast handler AND the adjuster.

**Step 2: Feed polling data to ledgers**

In `refresh_account()`, after `compute_event_positions`, call `adjuster.get_ledger(evt).sync_from_orders(orders, ticker_a, ticker_b)` for each active pair.

**Step 3: Replace compute_event_positions with ledger queries**

Modify `refresh_account()` and `refresh_queue_positions()` to build `EventPositionSummary` from ledger state instead of calling `compute_event_positions()`. The ledger is now the source of truth.

**Step 4: Show proposals**

When `BidAdjuster.on_proposal` fires, show a modal or notification with the `ProposedAdjustment` details. Approval calls `adjuster.execute(proposal, rest_client)`. Rejection calls `adjuster.clear_proposal()`.

**Step 5: Commit**

```bash
git add src/talos/ui/app.py
git commit -m "feat: wire BidAdjuster into TalosApp for semi-auto bid adjustment"
```

---

## Task 8: Run Safety Verification

**No new code — verification only.**

**Step 1: Run all tests**

Run: `.venv/Scripts/python -m pytest -v`
Expected: All passed

**Step 2: Run lint + type check**

Run: `.venv/Scripts/python -m ruff check src/ tests/ && .venv/Scripts/python -m pyright`
Expected: Clean

**Step 3: Run safety-audit skill**

Invoke the `safety-audit` skill — it will check all 6 position ledger invariants (D1–D6) against the new code.

**Step 4: Run position-scenarios skill**

Invoke the `position-scenarios` skill — it will trace through all 8 failure scenarios (S1–S8) against the actual implementation.

**Step 5: Commit any fixes**

If either skill surfaces issues, fix them, re-run tests, and commit.

---

## Summary

| Task | What | Tests | Commit |
|------|------|-------|--------|
| 1 | ProposedAdjustment model | 2 | `feat: add ProposedAdjustment model` |
| 2 | PositionLedger core state | 13 | `feat: add PositionLedger pure state machine` |
| 3 | PositionLedger safety gates | 8 | `test: add safety gate tests` |
| 4 | PositionLedger reconciliation | 5 | `test: add reconciliation tests` |
| 5 | BidAdjuster decision logic | 8 | `feat: add BidAdjuster orchestrator` |
| 6 | BidAdjuster async execution | 3 | `feat: add async execute()` |
| 7 | Wire into TalosApp | manual | `feat: wire BidAdjuster into TalosApp` |
| 8 | Safety verification | skills | fix commits if needed |

**Total: ~39 automated tests + 2 skill-based verification passes**
