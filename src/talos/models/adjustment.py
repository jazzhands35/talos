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
