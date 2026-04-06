"""Per-side order tracking state for a Drip run."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel


class OrderInfo(BaseModel):
    """Snapshot of a single resting order."""

    order_id: str
    price: int
    created_at: datetime


class DripSide:
    """Tracks the state of one side (A or B) of a Drip arbitrage run.

    Orders are stored oldest-first — index 0 is the front of the queue,
    i.e. the order placed earliest (and thus closest to the front of the
    Kalshi maker queue).
    """

    def __init__(self, target_price: int) -> None:
        self.resting_orders: list[OrderInfo] = []
        self.filled_count: int = 0
        self.target_price: int = target_price
        self.deploying: bool = True

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def add_order(self, order_id: str, price: int) -> None:
        """Append a newly placed order to the resting list."""
        self.resting_orders.append(
            OrderInfo(order_id=order_id, price=price, created_at=datetime.now(UTC))
        )

    def remove_order(self, order_id: str) -> OrderInfo | None:
        """Remove an order by ID and return it, or None if not found."""
        for i, order in enumerate(self.resting_orders):
            if order.order_id == order_id:
                return self.resting_orders.pop(i)
        return None

    def record_fill(self, order_id: str) -> None:
        """Record a fill: increment filled_count and remove the order."""
        self.remove_order(order_id)
        self.filled_count += 1

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def front_order(self) -> OrderInfo | None:
        """Return the oldest resting order (front of queue), or None."""
        return self.resting_orders[0] if self.resting_orders else None

    @property
    def resting_count(self) -> int:
        """Number of currently resting orders."""
        return len(self.resting_orders)

    def has_capacity(self, max_resting: int) -> bool:
        """Return True if we can place another order without exceeding max_resting."""
        return self.resting_count < max_resting
