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


class ProposalKey(BaseModel, frozen=True):
    """Hashable key for deduplicating proposals in a dict."""

    event_ticker: str
    side: str  # "A", "B", or "" for bids (both sides)
    kind: Literal["adjustment", "bid"]


class Proposal(BaseModel):
    """Unified envelope wrapping either an adjustment or bid proposal."""

    key: ProposalKey
    kind: Literal["adjustment", "bid"]
    summary: str
    detail: str
    created_at: datetime
    stale: bool = False
    stale_since: datetime | None = None
    adjustment: ProposedAdjustment | None = None
    bid: ProposedBid | None = None
