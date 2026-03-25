"""Pydantic models for arbitrage strategy."""

from __future__ import annotations

from pydantic import BaseModel


class ArbPair(BaseModel):
    """Two mutually exclusive markets within a game event."""

    event_ticker: str
    ticker_a: str
    ticker_b: str
    side_a: str = "no"  # "yes" or "no" — Kalshi side for leg A
    side_b: str = "no"  # "yes" or "no" — Kalshi side for leg B
    kalshi_event_ticker: str = ""  # Real Kalshi event ticker for API calls
    series_ticker: str = ""  # for volume refresh and category display
    fee_type: str = "quadratic_with_maker_fees"
    fee_rate: float = 0.0175
    close_time: str | None = None
    expected_expiration_time: str | None = None

    @property
    def is_same_ticker(self) -> bool:
        """True when both legs trade the same market (YES/NO arb)."""
        return self.ticker_a == self.ticker_b

    @property
    def api_event_ticker(self) -> str:
        """Event ticker for Kalshi API calls."""
        return self.kalshi_event_ticker or self.event_ticker


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
    close_time: str | None = None
    fee_rate: float = 0.0175

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
