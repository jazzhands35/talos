"""Runtime state — tracks desired vs acknowledged exchange state.

Separates what the controller *wants* from what Kalshi has *confirmed*.
The controller remains pure; this module tracks the app-layer reality.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum


class SyncState(StrEnum):
    """Connection / synchronization state for the Drip WS feed."""

    HYDRATING = "hydrating"
    LIVE = "live"
    STALE = "stale"
    RECONNECTING = "reconnecting"


class SimpleBook:
    """Minimal NO-side orderbook for tracking best price.

    Maintains a price -> quantity map from WS snapshots + deltas.
    Only tracks the NO side (Drip buys NO contracts).
    """

    def __init__(self) -> None:
        self._levels: dict[int, int] = {}

    def apply_snapshot(self, no_levels: list[list[int]]) -> None:
        """Replace book with a full snapshot."""
        self._levels.clear()
        for level in no_levels:
            price, qty = level[0], level[1]
            if qty > 0:
                self._levels[price] = qty

    def apply_delta(self, price: int, delta: int) -> None:
        """Apply an incremental quantity change at a price level."""
        current = self._levels.get(price, 0)
        new_qty = current + delta
        if new_qty <= 0:
            self._levels.pop(price, None)
        else:
            self._levels[price] = new_qty

    @property
    def best_price(self) -> int | None:
        """Highest NO bid price, or None if book is empty."""
        return max(self._levels) if self._levels else None


class SideRuntime:
    """Per-side runtime tracking for the app layer.

    Tracks the gap between what REST has accepted and what Kalshi
    has confirmed via the user_orders WS channel:

    - pending_placements: orders REST accepted but WS hasn't confirmed resting
    - pending_cancel_ids: cancels REST accepted but WS hasn't confirmed removed
    """

    def __init__(self) -> None:
        self.pending_placements: dict[str, int] = {}  # order_id -> price
        self.pending_cancel_ids: set[str] = set()
        self.last_best_no: int | None = None
        self.book: SimpleBook = SimpleBook()


class RuntimeState:
    """App-layer runtime state.

    Tracks connection state, per-side orderbook + pending actions,
    and WS liveness timestamps.
    """

    def __init__(self) -> None:
        self.sync_state: SyncState = SyncState.HYDRATING
        self.side_a: SideRuntime = SideRuntime()
        self.side_b: SideRuntime = SideRuntime()
        self.last_ws_at: datetime | None = None

    def get_side(self, label: str) -> SideRuntime:
        """Return SideRuntime for a given label."""
        if label == "A":
            return self.side_a
        if label == "B":
            return self.side_b
        msg = f"Unknown side: {label!r}"
        raise ValueError(msg)

    def touch_ws(self) -> None:
        """Record that a WS message was received."""
        self.last_ws_at = datetime.now(UTC)
