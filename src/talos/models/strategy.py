"""Pydantic models for arbitrage strategy."""

from __future__ import annotations

from pydantic import BaseModel


class ArbPair(BaseModel):
    """Two mutually exclusive markets within a game event."""

    event_ticker: str
    ticker_a: str
    ticker_b: str


class Opportunity(BaseModel):
    """A detected NO+NO arbitrage opportunity."""

    event_ticker: str
    ticker_a: str
    ticker_b: str
    no_a: int
    no_b: int
    qty_a: int
    qty_b: int
    raw_edge: int
    fee_edge: float = 0.0
    tradeable_qty: int
    timestamp: str

    @property
    def cost(self) -> int:
        """Total NO cost per contract in cents."""
        return self.no_a + self.no_b


class BidConfirmation(BaseModel):
    """Result from the bid confirmation modal."""

    ticker_a: str
    ticker_b: str
    no_a: int
    no_b: int
    qty: int
