"""Pydantic models for Kalshi portfolio data."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, model_validator

from talos.models._converters import dollars_to_bps as _dollars_to_bps
from talos.models._converters import dollars_to_bps_round as _dollars_to_bps_round
from talos.models._converters import fp_to_fp100 as _fp_to_fp100
from talos.models._converters import log_unknown_fields

_POSITION_FP_FIELDS = frozenset(
    {
        "position_fp",
        "total_traded_dollars",
        "market_exposure_dollars",
        "realized_pnl_dollars",
        "fees_paid_dollars",
    }
)

_EVENT_POSITION_FP_FIELDS = frozenset(
    {
        "total_cost_dollars",
        "event_exposure_dollars",
        "realized_pnl_dollars",
        "fees_paid_dollars",
        "total_cost_shares_fp",
    }
)

_SETTLEMENT_FP_FIELDS = frozenset(
    {
        "yes_total_cost_dollars",
        "no_total_cost_dollars",
        "yes_count_fp",
        "no_count_fp",
        "value_dollars",
        "fee_cost_dollars",
    }
)


class Balance(BaseModel):
    """Account balance.

    CAUTION: Mixed wire formats across Kalshi endpoints.
    ``/portfolio/balance`` STILL returns integer-cents ``balance`` /
    ``portfolio_value`` — it was not migrated to the ``_dollars`` string
    format that the rest of the portfolio endpoints use. Both wire
    formats are handled here; integer cents is the common case today,
    ``_dollars`` is honored for forward-compatibility if Kalshi migrates
    this endpoint later.

    Task 13a-2c (2026-04-23): legacy integer-cents Python fields removed.
    ``balance_bps`` / ``portfolio_value_bps`` are the sole money
    representation.
    """

    balance_bps: int = 0
    portfolio_value_bps: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # Integer-cents wire format (current Kalshi behavior). Promote
        # via ×100 to bps.
        if "balance" in data and isinstance(data["balance"], int):
            data["balance_bps"] = data["balance"] * 100
            del data["balance"]
        if "portfolio_value" in data and isinstance(data["portfolio_value"], int):
            data["portfolio_value_bps"] = data["portfolio_value"] * 100
            del data["portfolio_value"]
        # _dollars wire format (forward-compatible — balance aggregates
        # account-wide, so use the rounding parser).
        for new_bps, wire in [
            ("balance_bps", "balance_dollars"),
            ("portfolio_value_bps", "portfolio_value_dollars"),
        ]:
            if wire in data and data[wire] is not None:
                data[new_bps] = _dollars_to_bps_round(data[wire])
        return data


class Position(BaseModel, extra="ignore"):
    """A position in a market from GET /portfolio/positions.

    The ``position_fp100`` field is the authoritative contract count (P7/P15).
    Negative = NO contracts, positive = YES contracts.
    Unlike GET /portfolio/orders, this never archives — it always
    reflects the true state on Kalshi.

    Task 13a-2c (2026-04-23): legacy integer-cents / integer-contract
    fields (``position``, ``total_traded``, ``market_exposure``, ...)
    removed. ``_bps`` / ``_fp100`` siblings are the sole representation.
    """

    ticker: str
    position_fp100: int = 0
    total_traded_bps: int = 0
    market_exposure_bps: int = 0
    realized_pnl_bps: int = 0
    fees_paid_bps: int = 0
    # Count of resting orders (number of orders, not contracts). Kalshi returns
    # this as an integer — verified against production /portfolio/positions
    # 2026-04-25. There is NO ``_fp`` variant; orders are inherently whole.
    resting_orders_count: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        log_unknown_fields("Position", data, cls.model_fields.keys() | _POSITION_FP_FIELDS)
        # Dollars → bps (AGGREGATE-rounded). These are sums across fills/trades —
        # Kalshi legitimately emits sub-bps precision (6-decimal) values. Use
        # the rounding parser; per-contract prices use the strict parser.
        for new_bps, wire in [
            ("total_traded_bps", "total_traded_dollars"),
            ("market_exposure_bps", "market_exposure_dollars"),
            ("realized_pnl_bps", "realized_pnl_dollars"),
            ("fees_paid_bps", "fees_paid_dollars"),
        ]:
            if wire in data and data[wire] is not None:
                data[new_bps] = _dollars_to_bps_round(data[wire])
        # FP → fp100 (exact).
        for new_fp100, wire in [
            ("position_fp100", "position_fp"),
        ]:
            if wire in data and data[wire] is not None:
                data[new_fp100] = _fp_to_fp100(data[wire])
        return data


class EventPosition(BaseModel, extra="ignore"):
    """An aggregate position across an event (from Kalshi event_positions).

    Rich fields give Kalshi's live P&L per event — authoritative data (P21).

    Task 13a-2c (2026-04-23): legacy cents/contracts fields removed.
    """

    event_ticker: str
    total_cost_bps: int = 0
    event_exposure_bps: int = 0
    realized_pnl_bps: int = 0
    fees_paid_bps: int = 0
    total_cost_shares_fp100: int = 0
    # Note: event_positions[] entries do NOT carry a resting-orders count from
    # Kalshi (verified 2026-04-25). Resting-orders state lives only on
    # market_positions entries via Position.resting_orders_count.

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        log_unknown_fields(
            "EventPosition", data, cls.model_fields.keys() | _EVENT_POSITION_FP_FIELDS
        )
        # Dollars → bps (AGGREGATE-rounded). Kalshi emits sums with 6-decimal
        # precision (sub-bps); use the rounding parser rather than fail-closed.
        for new_bps, wire in [
            ("total_cost_bps", "total_cost_dollars"),
            ("event_exposure_bps", "event_exposure_dollars"),
            ("realized_pnl_bps", "realized_pnl_dollars"),
            ("fees_paid_bps", "fees_paid_dollars"),
        ]:
            if wire in data and data[wire] is not None:
                data[new_bps] = _dollars_to_bps_round(data[wire])
        # FP → fp100 (exact).
        for new_fp100, wire in [
            ("total_cost_shares_fp100", "total_cost_shares_fp"),
        ]:
            if wire in data and data[wire] is not None:
                data[new_fp100] = _fp_to_fp100(data[wire])
        return data


class Settlement(BaseModel, extra="ignore"):
    """A settled market from GET /portfolio/settlements.

    CAUTION: Mixed units in the same response!
    - ``revenue`` arrives as an integer in cents (NOT a dollars string)
    - ``fee_cost`` arrives as a dollars string

    The validator promotes the integer cents wire ``revenue`` to
    ``revenue_bps`` via ×100. ``fee_cost``'s dollars string routes through
    the aggregate-rounded Decimal parser.

    Task 13a-2c (2026-04-23): legacy cents/contracts fields removed.
    """

    ticker: str
    event_ticker: str = ""
    market_result: str = ""
    settled_time: str = ""
    revenue_bps: int = 0
    fee_cost_bps: int = 0
    yes_count_fp100: int = 0
    no_count_fp100: int = 0
    yes_total_cost_bps: int = 0
    no_total_cost_bps: int = 0
    value_bps: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        log_unknown_fields("Settlement", data, cls.model_fields.keys() | _SETTLEMENT_FP_FIELDS)
        # revenue wire is int cents — promote via ×100. Do NOT run through
        # _dollars_to_bps: revenue is NOT a dollars string.
        if "revenue" in data and isinstance(data["revenue"], int):
            data["revenue_bps"] = data["revenue"] * 100
            del data["revenue"]
        # fee_cost arrives as a dollars string from the API. Also handle
        # fee_cost_dollars (FP variant) if present. Settlement aggregates
        # fee across all fills on a market; use aggregate-rounded parser.
        if "fee_cost_dollars" in data and data["fee_cost_dollars"] is not None:
            data["fee_cost_bps"] = _dollars_to_bps_round(data["fee_cost_dollars"])
        elif "fee_cost" in data and isinstance(data["fee_cost"], str):
            data["fee_cost_bps"] = _dollars_to_bps_round(data["fee_cost"])
            del data["fee_cost"]
        # Dollars → bps (AGGREGATE-rounded). yes_total_cost / no_total_cost
        # sum costs across fills.
        for new_bps, wire in [
            ("yes_total_cost_bps", "yes_total_cost_dollars"),
            ("no_total_cost_bps", "no_total_cost_dollars"),
        ]:
            if wire in data and data[wire] is not None:
                data[new_bps] = _dollars_to_bps_round(data[wire])
        # FP → fp100 (exact).
        for new_fp100, wire in [
            ("yes_count_fp100", "yes_count_fp"),
            ("no_count_fp100", "no_count_fp"),
        ]:
            if wire in data and data[wire] is not None:
                data[new_fp100] = _fp_to_fp100(data[wire])
        # value (per-contract payout) — may be dollars string.
        if "value_dollars" in data and data["value_dollars"] is not None:
            data["value_bps"] = _dollars_to_bps(data["value_dollars"])
        return data


class ExchangeStatus(BaseModel):
    """Exchange operational status."""

    trading_active: bool
    exchange_active: bool
