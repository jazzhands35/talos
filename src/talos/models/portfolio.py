"""Pydantic models for Kalshi portfolio data."""

from __future__ import annotations

from pydantic import BaseModel


class Balance(BaseModel):
    """Account balance."""

    balance: int
    portfolio_value: int


class Position(BaseModel, extra="ignore"):
    """A position in a market from GET /portfolio/positions.

    The `position` field is the authoritative contract count (P7/P15).
    Negative = NO contracts, positive = YES contracts.
    Unlike GET /portfolio/orders, this never archives — it always
    reflects the true state on Kalshi.
    """

    ticker: str
    position: int = 0
    total_traded: int = 0
    market_exposure: int = 0
    resting_orders_count: int = 0


class EventPosition(BaseModel, extra="ignore"):
    """An aggregate position across an event (from Kalshi event_positions)."""

    event_ticker: str


class Settlement(BaseModel):
    """A settled market position."""

    ticker: str
    settlement_price: int
    payout: int
    settled_time: str


class ExchangeStatus(BaseModel):
    """Exchange operational status."""

    trading_active: bool
    exchange_active: bool
