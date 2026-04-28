"""DRIP/BLIP staggered arbitrage controller.

POC scope: one resting drip per side. The controller is pure; it records
fills and ETA observations, then emits actions for TradingEngine to execute.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import inf, isfinite
from typing import Literal

DripSide = Literal["A", "B"]


@dataclass(frozen=True)
class DripConfig:
    """Per-event DRIP configuration."""

    drip_size: int = 1
    max_drips: int = 1
    blip_delta_min: float = 5.0

    def __post_init__(self) -> None:
        if self.drip_size < 1:
            raise ValueError(f"drip_size must be >= 1 (got {self.drip_size})")
        if self.max_drips < 1:
            raise ValueError(f"max_drips must be >= 1 (got {self.max_drips})")
        if self.blip_delta_min < 0:
            raise ValueError(f"blip_delta_min must be >= 0 (got {self.blip_delta_min})")

    @property
    def max_ahead_per_side(self) -> int:
        return self.drip_size * self.max_drips


@dataclass(frozen=True)
class PlaceOrder:
    side: DripSide
    drip_size_fp100: int


@dataclass(frozen=True)
class BlipAction:
    """Send an ahead-side order to the back of the queue."""

    side: DripSide
    order_id: str


@dataclass(frozen=True)
class NoOp:
    reason: str = ""


Action = PlaceOrder | BlipAction | NoOp


def _identify_ahead_side(
    eta_a_min: float | None,
    eta_b_min: float | None,
) -> DripSide | None:
    """Return the lower-ETA side, or None when there is no usable signal."""
    if eta_a_min is None and eta_b_min is None:
        return None
    if eta_a_min is None:
        return "B"
    if eta_b_min is None:
        return "A"
    if eta_a_min == eta_b_min:
        return None
    return "A" if eta_a_min < eta_b_min else "B"


def _eta_delta(eta_ahead: float, eta_behind: float | None) -> float:
    """Minutes the behind side trails the ahead side."""
    if eta_behind is None:
        return inf
    if not isfinite(eta_behind):
        return inf
    return eta_behind - eta_ahead


@dataclass
class DripController:
    """Pure state machine for DRIP fill tracking and BLIP decisions."""

    config: DripConfig
    filled_a_fp100: int = 0
    filled_b_fp100: int = 0
    _seen_trade_ids: set[str] = field(default_factory=set)

    @property
    def pairs_filled(self) -> int:
        drip_fp100 = self.config.drip_size * 100
        return min(self.filled_a_fp100, self.filled_b_fp100) // drip_fp100

    def record_fill(
        self,
        side: DripSide,
        count_fp100: int,
        trade_id: str | None = None,
    ) -> list[Action]:
        """Record a confirmed fill and emit matched-pair replenishment."""
        if trade_id is not None:
            if trade_id in self._seen_trade_ids:
                return [NoOp("duplicate_trade_id")]
            self._seen_trade_ids.add(trade_id)

        pairs_before = self.pairs_filled
        if side == "A":
            self.filled_a_fp100 += count_fp100
        elif side == "B":
            self.filled_b_fp100 += count_fp100
        else:
            raise ValueError(f"unknown side: {side}")

        pairs_after = self.pairs_filled
        increments = pairs_after - pairs_before
        if increments <= 0:
            return [NoOp("no_pair_completed")]

        drip_fp100 = self.config.drip_size * 100
        actions: list[Action] = []
        for _ in range(increments):
            actions.append(PlaceOrder("A", drip_fp100))
            actions.append(PlaceOrder("B", drip_fp100))
        return actions

    def evaluate_blip(
        self,
        *,
        eta_a_min: float | None,
        eta_b_min: float | None,
        front_a_id: str | None,
        front_b_id: str | None,
    ) -> list[Action]:
        """BLIP ahead side when ETA_behind - ETA_ahead exceeds threshold."""
        ahead = _identify_ahead_side(eta_a_min, eta_b_min)
        if ahead is None:
            return [NoOp("no_eta_signal")]

        if ahead == "A":
            eta_ahead = eta_a_min
            eta_behind = eta_b_min
            order_id = front_a_id
        else:
            eta_ahead = eta_b_min
            eta_behind = eta_a_min
            order_id = front_b_id

        if eta_ahead is None:
            return [NoOp("no_ahead_eta")]
        if order_id is None:
            return [NoOp("no_front_order")]

        if _eta_delta(eta_ahead, eta_behind) > self.config.blip_delta_min:
            return [BlipAction(ahead, order_id)]
        return [NoOp("blip_below_threshold")]


def evaluate_blip(
    config: DripConfig,
    *,
    eta_a_min: float | None,
    eta_b_min: float | None,
    front_a_id: str | None,
    front_b_id: str | None,
) -> Action:
    """BLIP ahead side when ETA_behind - ETA_ahead exceeds threshold.

    Pure function — fill tracking lives in the standard PositionLedger;
    this function only consumes ETA + front-order signals. Returns a single
    Action (not a list).
    """
    ahead = _identify_ahead_side(eta_a_min, eta_b_min)
    if ahead is None:
        return NoOp("no_eta_signal")

    if ahead == "A":
        eta_ahead = eta_a_min
        eta_behind = eta_b_min
        order_id = front_a_id
    else:
        eta_ahead = eta_b_min
        eta_behind = eta_a_min
        order_id = front_b_id

    if eta_ahead is None:
        return NoOp("no_ahead_eta")
    if order_id is None:
        return NoOp("no_front_order")

    if _eta_delta(eta_ahead, eta_behind) > config.blip_delta_min:
        return BlipAction(ahead, order_id)
    return NoOp("blip_below_threshold")
