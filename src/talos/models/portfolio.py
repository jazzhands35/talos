"""Pydantic models for Kalshi portfolio data."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, model_validator

from talos.models._converters import dollars_to_cents as _dollars_to_cents
from talos.models._converters import fp_to_int as _fp_to_int
from talos.models._converters import log_unknown_fields

_POSITION_FP_FIELDS = frozenset({
    "position_fp", "total_traded_dollars", "market_exposure_dollars",
    "resting_orders_count_fp", "realized_pnl_dollars", "fees_paid_dollars",
})

_EVENT_POSITION_FP_FIELDS = frozenset({
    "total_cost_dollars", "event_exposure_dollars", "realized_pnl_dollars",
    "fees_paid_dollars", "total_cost_shares_fp", "resting_orders_count_fp",
})

_SETTLEMENT_FP_FIELDS = frozenset({
    "yes_total_cost_dollars", "no_total_cost_dollars",
    "yes_count_fp", "no_count_fp", "value_dollars",
})


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
    realized_pnl: int = 0
    fees_paid: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        log_unknown_fields("Position", data, cls.model_fields.keys() | _POSITION_FP_FIELDS)
        if "position_fp" in data and data["position_fp"] is not None:
            data["position"] = _fp_to_int(data["position_fp"])
        for old, new in [
            ("total_traded", "total_traded_dollars"),
            ("market_exposure", "market_exposure_dollars"),
            ("realized_pnl", "realized_pnl_dollars"),
            ("fees_paid", "fees_paid_dollars"),
        ]:
            if new in data and data[new] is not None:
                data[old] = _dollars_to_cents(data[new])
        if "resting_orders_count_fp" in data and data["resting_orders_count_fp"] is not None:
            data["resting_orders_count"] = _fp_to_int(data["resting_orders_count_fp"])
        return data


class EventPosition(BaseModel, extra="ignore"):
    """An aggregate position across an event (from Kalshi event_positions).

    Rich fields give Kalshi's live P&L per event — authoritative data (P21).
    """

    event_ticker: str
    total_cost: int = 0
    total_cost_shares: int = 0
    event_exposure: int = 0
    realized_pnl: int = 0
    resting_orders_count: int = 0
    fees_paid: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        log_unknown_fields(
            "EventPosition", data, cls.model_fields.keys() | _EVENT_POSITION_FP_FIELDS
        )
        for old, new in [
            ("total_cost", "total_cost_dollars"),
            ("event_exposure", "event_exposure_dollars"),
            ("realized_pnl", "realized_pnl_dollars"),
            ("fees_paid", "fees_paid_dollars"),
        ]:
            if new in data and data[new] is not None:
                data[old] = _dollars_to_cents(data[new])
        if "total_cost_shares_fp" in data and data["total_cost_shares_fp"] is not None:
            data["total_cost_shares"] = _fp_to_int(data["total_cost_shares_fp"])
        if "resting_orders_count_fp" in data and data["resting_orders_count_fp"] is not None:
            data["resting_orders_count"] = _fp_to_int(data["resting_orders_count_fp"])
        return data


class Settlement(BaseModel, extra="ignore"):
    """A settled market from GET /portfolio/settlements.

    CAUTION: Mixed units in the same response!
    - ``revenue`` is an integer in cents (NOT a dollars string)
    - ``fee_cost`` is a dollars string (NOT a cents integer)
    """

    ticker: str
    event_ticker: str = ""
    market_result: str = ""
    revenue: int = 0
    fee_cost: int = 0
    yes_count: int = 0
    no_count: int = 0
    yes_total_cost: int = 0
    no_total_cost: int = 0
    settled_time: str = ""
    value: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        log_unknown_fields("Settlement", data, cls.model_fields.keys() | _SETTLEMENT_FP_FIELDS)
        # revenue is already cents integer — leave it
        # fee_cost arrives as a dollars string from the API — convert to cents.
        # Also handle fee_cost_dollars (FP variant) if present.
        if "fee_cost_dollars" in data and data["fee_cost_dollars"] is not None:
            data["fee_cost"] = _dollars_to_cents(data["fee_cost_dollars"])
        elif "fee_cost" in data and isinstance(data["fee_cost"], str):
            data["fee_cost"] = _dollars_to_cents(data["fee_cost"])
        for old, new in [
            ("yes_total_cost", "yes_total_cost_dollars"),
            ("no_total_cost", "no_total_cost_dollars"),
        ]:
            if new in data and data[new] is not None:
                data[old] = _dollars_to_cents(data[new])
        for old, new in [
            ("yes_count", "yes_count_fp"),
            ("no_count", "no_count_fp"),
        ]:
            if new in data and data[new] is not None:
                data[old] = _fp_to_int(data[new])
        # value (per-contract payout) — may be dollars string
        if "value_dollars" in data and data["value_dollars"] is not None:
            data["value"] = _dollars_to_cents(data["value_dollars"])
        return data


class ExchangeStatus(BaseModel):
    """Exchange operational status."""

    trading_active: bool
    exchange_active: bool
