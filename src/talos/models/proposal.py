"""Pydantic models for the supervised-automation proposal system."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from talos.models.adjustment import ProposedAdjustment


class ProposedBid(BaseModel):
    """A proposed new arb bid for operator approval."""

    event_ticker: str
    ticker_a: str
    ticker_b: str
    no_a: int
    no_b: int
    qty: int
    edge_cents: float
    stable_for_seconds: float
    reason: str


class ProposedRebalance(BaseModel):
    """A proposed rebalance: reduce over-side resting, then catch up under-side.

    Either or both steps may be present:
    - Step 1 (reduce): order_id + ticker set when over-side has resting to cancel/amend
    - Step 2 (catch-up): catchup_ticker + catchup_qty set when under-side needs orders
    """

    event_ticker: str
    side: str  # "A" or "B" — the over-extended side
    # Step 1: reduce over-side resting
    order_id: str | None = None
    ticker: str | None = None
    current_resting: int = 0
    target_resting: int = 0
    filled_count: int = 0
    resting_price: int = 0
    # Step 2: catch-up bid on under-side
    catchup_ticker: str | None = None
    catchup_price: int = 0
    catchup_qty: int = 0


class ProposalKey(BaseModel, frozen=True):
    """Hashable key for deduplicating proposals in a dict."""

    event_ticker: str
    side: str  # "A", "B", or "" for bids (both sides)
    kind: Literal["adjustment", "bid", "hold", "rebalance"]


class Proposal(BaseModel):
    """Unified envelope wrapping either an adjustment or bid proposal."""

    key: ProposalKey
    kind: Literal["adjustment", "bid", "hold", "rebalance"]
    summary: str
    detail: str
    created_at: datetime
    stale: bool = False
    stale_since: datetime | None = None
    adjustment: ProposedAdjustment | None = None
    bid: ProposedBid | None = None
    rebalance: ProposedRebalance | None = None
