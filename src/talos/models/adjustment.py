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
    action: Literal["follow_jump", "hold", "withdraw"]
    cancel_order_id: str = ""
    cancel_count: int = 0
    cancel_price: int = 0
    new_count: int = 0
    new_price: int = 0
    reason: str
    position_before: str = ""
    position_after: str = ""
    safety_check: str = ""
