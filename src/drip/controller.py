"""DripController — pure state machine for Drip arbitrage runs.

Receives events (fills, price jumps, timers) and returns action objects.
NO async, NO I/O, NO network imports.  The app layer executes actions.
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from drip.config import DripConfig
from drip.side_state import DripSide
from talos.fees import fee_adjusted_cost

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Action types — returned by the controller, executed by the app layer
# ---------------------------------------------------------------------------


class PlaceOrder(BaseModel):
    """Instruction to place a 1-contract NO bid."""

    side: str  # "A" or "B"
    price: int  # NO price in cents


class CancelOrder(BaseModel):
    """Instruction to cancel a resting order."""

    side: str
    order_id: str
    reason: str  # "jump_rotate", "delta_cancel", "wind_down"


class NoOp(BaseModel):
    """No action needed — carries a reason for logging."""

    reason: str


# Type alias for action unions
Action = PlaceOrder | CancelOrder | NoOp


class DripController:
    """Pure state machine driving a single Drip arbitrage run.

    All methods are synchronous and return action lists.
    The application layer is responsible for executing them against Kalshi.
    """

    def __init__(self, config: DripConfig) -> None:
        self.config = config
        self.side_a = DripSide(target_price=config.price_a)
        self.side_b = DripSide(target_price=config.price_b)
        self._deploy_turn: str = "A"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _side(self, label: str) -> DripSide:
        """Return the DripSide for a given label."""
        if label == "A":
            return self.side_a
        if label == "B":
            return self.side_b
        raise ValueError(f"Unknown side: {label!r}")

    def _other_label(self, label: str) -> str:
        return "B" if label == "A" else "A"

    def is_profitable(self) -> bool:
        """Check whether current target prices pass the profitability gate."""
        cost = fee_adjusted_cost(
            self.side_a.target_price, rate=self.config.fee_rate
        ) + fee_adjusted_cost(self.side_b.target_price, rate=self.config.fee_rate)
        return cost < 100

    def _place_if_profitable(self, label: str) -> PlaceOrder | NoOp:
        """Return a PlaceOrder if profitable, else NoOp."""
        if self.is_profitable():
            return PlaceOrder(side=label, price=self._side(label).target_price)
        return NoOp(reason=f"unprofitable at current prices for side {label}")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def delta(self) -> int:
        """Absolute fill imbalance between sides."""
        return abs(self.side_a.filled_count - self.side_b.filled_count)

    @property
    def ahead_side(self) -> str | None:
        """Label of the side with MORE fills, or None if balanced."""
        if self.side_a.filled_count > self.side_b.filled_count:
            return "A"
        if self.side_b.filled_count > self.side_a.filled_count:
            return "B"
        return None

    @property
    def behind_side(self) -> str | None:
        """Label of the side with FEWER fills, or None if balanced."""
        ahead = self.ahead_side
        if ahead is None:
            return None
        return self._other_label(ahead)

    @property
    def total_filled(self) -> int:
        """Total fills across both sides."""
        return self.side_a.filled_count + self.side_b.filled_count

    @property
    def matched_pairs(self) -> int:
        """Number of fully matched A+B pairs (guaranteed profit)."""
        return min(self.side_a.filled_count, self.side_b.filled_count)

    # ------------------------------------------------------------------
    # Core event handlers
    # ------------------------------------------------------------------

    def on_fill(self, side: str, order_id: str) -> list[Action]:
        """Handle a fill event on the given side.

        Decision logic based on delta after recording the fill:
        - delta == 0 (balanced): replenish BOTH sides if they have capacity
        - delta == 1 (just unbalanced): replenish BEHIND side only
        - delta > 1 (growing imbalance): cancel AHEAD front bid + replenish behind
        """
        self._side(side).record_fill(order_id)
        actions: list[Action] = []

        delta = self.delta

        if delta == 0:
            # Balanced — replenish both sides
            for label in ("A", "B"):
                s = self._side(label)
                if s.has_capacity(self.config.max_resting):
                    actions.append(self._place_if_profitable(label))
                else:
                    actions.append(NoOp(reason=f"side {label} at capacity ({s.resting_count})"))
        elif delta == 1:
            # Just unbalanced — replenish behind only
            behind = self.behind_side
            assert behind is not None
            behind_side = self._side(behind)
            if behind_side.has_capacity(self.config.max_resting):
                actions.append(self._place_if_profitable(behind))
            else:
                actions.append(
                    NoOp(reason=f"behind side {behind} at capacity ({behind_side.resting_count})")
                )
        else:
            # delta > 1 — cancel ahead front + replenish behind
            ahead = self.ahead_side
            assert ahead is not None
            behind = self.behind_side
            assert behind is not None

            ahead_front = self._side(ahead).front_order()
            if ahead_front is not None:
                actions.append(
                    CancelOrder(
                        side=ahead,
                        order_id=ahead_front.order_id,
                        reason="delta_cancel",
                    )
                )
            else:
                actions.append(NoOp(reason=f"ahead side {ahead} has no resting orders to cancel"))

            behind_side = self._side(behind)
            if behind_side.has_capacity(self.config.max_resting):
                actions.append(self._place_if_profitable(behind))
            else:
                actions.append(
                    NoOp(reason=f"behind side {behind} at capacity ({behind_side.resting_count})")
                )

        log.debug(
            "on_fill",
            side=side,
            order_id=order_id,
            delta=delta,
            actions=[type(a).__name__ for a in actions],
        )
        return actions

    def on_jump(self, side: str, new_price: int) -> list[Action]:
        """Handle a price jump on the given side.

        Updates the target price.  If there are resting orders at the old
        price, cancels the front order.  If profitable at the new price,
        places a replacement.
        """
        drip_side = self._side(side)
        old_price = drip_side.target_price
        drip_side.target_price = new_price
        actions: list[Action] = []

        # Cancel front order at old price if one exists
        front = drip_side.front_order()
        if front is not None:
            actions.append(
                CancelOrder(
                    side=side,
                    order_id=front.order_id,
                    reason="jump_rotate",
                )
            )

        # Place at new price if profitable
        if self.is_profitable():
            actions.append(PlaceOrder(side=side, price=new_price))
        else:
            actions.append(NoOp(reason=f"unprofitable after jump to {new_price} on side {side}"))

        log.debug(
            "on_jump",
            side=side,
            old_price=old_price,
            new_price=new_price,
            actions=[type(a).__name__ for a in actions],
        )
        return actions

    def deploy_next(self) -> list[Action]:
        """Deploy the next contract during the initial stagger phase.

        Alternates A, B, A, B.  Returns PlaceOrder for the next side that
        has capacity, or NoOp if both sides are fully deployed.
        """
        # Try the current turn's side first, then the other
        for _ in range(2):
            label = self._deploy_turn
            side = self._side(label)

            if side.deploying and side.has_capacity(self.config.max_resting):
                action = self._place_if_profitable(label)
                # Advance turn regardless of profitability
                self._deploy_turn = self._other_label(label)
                log.debug("deploy_next", side=label, action=type(action).__name__)
                return [action]

            # This side can't deploy — mark it done and try the other
            side.deploying = False
            self._deploy_turn = self._other_label(label)

        return [NoOp(reason="both sides fully deployed")]

    def on_wind_down(self) -> list[CancelOrder]:
        """Cancel ALL resting orders on both sides."""
        actions: list[CancelOrder] = []

        for label in ("A", "B"):
            side = self._side(label)
            side.deploying = False
            for order in list(side.resting_orders):
                actions.append(
                    CancelOrder(
                        side=label,
                        order_id=order.order_id,
                        reason="wind_down",
                    )
                )

        log.debug("on_wind_down", cancel_count=len(actions))
        return actions

    # ------------------------------------------------------------------
    # Reconciliation (30s REST sync)
    # ------------------------------------------------------------------

    def reconcile(
        self,
        resting_a: list[tuple[str, int]],
        resting_b: list[tuple[str, int]],
        filled_a: int,
        filled_b: int,
    ) -> None:
        """Replace internal state with Kalshi truth.

        Args:
            resting_a: List of (order_id, price) for side A resting orders.
            resting_b: List of (order_id, price) for side B resting orders.
            filled_a: Fill count for side A from Kalshi.
            filled_b: Fill count for side B from Kalshi.

        Fill counts use monotonic max — never decrease.
        """
        # Rebuild resting orders for each side
        for side, resting in [
            (self.side_a, resting_a),
            (self.side_b, resting_b),
        ]:
            side.resting_orders.clear()
            for order_id, price in resting:
                side.add_order(order_id, price)

        # Monotonic max on fill counts
        self.side_a.filled_count = max(self.side_a.filled_count, filled_a)
        self.side_b.filled_count = max(self.side_b.filled_count, filled_b)

        log.debug(
            "reconcile",
            resting_a=len(resting_a),
            resting_b=len(resting_b),
            filled_a=self.side_a.filled_count,
            filled_b=self.side_b.filled_count,
        )
