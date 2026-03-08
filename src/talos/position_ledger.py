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

    def is_placement_safe(self, side: Side, count: int, price: int) -> tuple[bool, str]:
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

    def record_resting(self, side: Side, order_id: str, count: int, price: int) -> None:
        """Record a new resting order. Called after order placement confirmed."""
        s = self._sides[side]
        s.resting_order_id = order_id
        s.resting_count = count
        s.resting_price = price

    def record_cancel(self, side: Side, order_id: str) -> None:
        """Record an order cancellation."""
        s = self._sides[side]
        if s.resting_order_id != order_id:
            raise ValueError(f"order_id mismatch: expected {s.resting_order_id}, got {order_id}")
        s.resting_order_id = None
        s.resting_count = 0
        s.resting_price = 0

    def reset_pair(self) -> None:
        """Clear state after both sides complete. Ready for next pair."""
        self._sides[Side.A].reset()
        self._sides[Side.B].reset()
        self._discrepancy = None

    def sync_from_orders(self, orders: list, ticker_a: str, ticker_b: str) -> None:
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
                kalshi_resting[side].append((order.order_id, order.remaining_count, order.no_price))

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
                    f"side {side.value} has {len(resting_list)} resting orders (expected 0 or 1)"
                )
            elif len(resting_list) == 1:
                oid, cnt, price = resting_list[0]
                if s.resting_order_id is not None and s.resting_order_id != oid:
                    problems.append(
                        f"side {side.value} resting order_id: "
                        f"ledger={s.resting_order_id}, kalshi={oid}"
                    )
            elif len(resting_list) == 0 and s.resting_order_id is not None:
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
