"""Pydantic models for Kalshi portfolio data."""

from __future__ import annotations

from pydantic import BaseModel


class Balance(BaseModel):
    """Account balance."""

    balance: int
    portfolio_value: int


class Position(BaseModel):
    """A position in a market. Positive = long, negative = short."""

    ticker: str
    position: int
    total_traded: int
    market_exposure: int


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
