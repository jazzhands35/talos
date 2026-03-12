"""Pydantic models for Kalshi portfolio data."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, model_validator


def _dollars_to_cents(val: Any) -> int:
    """Convert a _dollars string/float to integer cents."""
    if val is None:
        return 0
    return round(float(val) * 100)


def _fp_to_int(val: Any) -> int:
    """Convert an _fp string to integer."""
    if val is None:
        return 0
    return int(float(val))


class Balance(BaseModel):
    """Account balance.

    Balance fields may use _dollars format post March 12, 2026.
    """

    balance: int
    portfolio_value: int

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "balance_dollars" in data and data["balance_dollars"] is not None:
            data["balance"] = _dollars_to_cents(data["balance_dollars"])
        if "portfolio_value_dollars" in data and data["portfolio_value_dollars"] is not None:
            data["portfolio_value"] = _dollars_to_cents(data["portfolio_value_dollars"])
        return data


class Position(BaseModel, extra="ignore"):
    """A position in a market from GET /portfolio/positions.

    The `position` field is the authoritative contract count (P7/P15).
    Negative = NO contracts, positive = YES contracts.
    Unlike GET /portfolio/orders, this never archives — it always
    reflects the true state on Kalshi.

    Post March 12, 2026: _fp/_dollars fields replace integer fields.
    """

    ticker: str
    position: int = 0
    total_traded: int = 0
    market_exposure: int = 0
    resting_orders_count: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "position_fp" in data and data["position_fp"] is not None:
            data["position"] = _fp_to_int(data["position_fp"])
        for old, new in [
            ("total_traded", "total_traded_dollars"),
            ("market_exposure", "market_exposure_dollars"),
        ]:
            if new in data and data[new] is not None:
                data[old] = _dollars_to_cents(data[new])
        if "resting_orders_count_fp" in data and data["resting_orders_count_fp"] is not None:
            data["resting_orders_count"] = _fp_to_int(data["resting_orders_count_fp"])
        return data


class EventPosition(BaseModel, extra="ignore"):
    """An aggregate position across an event (from Kalshi event_positions)."""

    event_ticker: str


class Settlement(BaseModel):
    """A settled market position."""

    ticker: str
    settlement_price: int
    payout: int
    settled_time: str

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "settlement_value_dollars" in data and data["settlement_value_dollars"] is not None:
            data["settlement_price"] = _dollars_to_cents(data["settlement_value_dollars"])
        if "payout_dollars" in data and data["payout_dollars"] is not None:
            data["payout"] = _dollars_to_cents(data["payout_dollars"])
        return data


class ExchangeStatus(BaseModel):
    """Exchange operational status."""

    trading_active: bool
    exchange_active: bool
