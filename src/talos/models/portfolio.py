"""Pydantic models for Kalshi portfolio data."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, model_validator

from talos.models._converters import dollars_to_bps as _dollars_to_bps
from talos.models._converters import dollars_to_bps_round as _dollars_to_bps_round
from talos.models._converters import dollars_to_cents as _dollars_to_cents
from talos.models._converters import fp_to_fp100 as _fp_to_fp100
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
    "fee_cost_dollars",
})


class Balance(BaseModel):
    """Account balance.

    Dual-unit migration (bps/fp100): ``balance`` / ``portfolio_value``
    keep the legacy integer-cents representation; ``balance_bps`` /
    ``portfolio_value_bps`` are the new exact-precision siblings. Both
    populate from the same ``_dollars`` wire payload. Downstream callers
    migrate from the legacy names to the ``_bps`` names incrementally;
    the legacy fields are deleted in Task 13 of
    ``docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md``
    once all callers have migrated.

    Post March 12, 2026: _dollars format replaces integer cents.
    """

    # Legacy integer-cents fields (lossy for sub-cent — deprecated).
    balance: int
    portfolio_value: int
    # New bps fields (exact precision — preferred).
    balance_bps: int = 0
    portfolio_value_bps: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # Balance aggregates account-wide; use aggregate-rounded parser.
        for legacy, new_bps, wire in [
            ("balance", "balance_bps", "balance_dollars"),
            ("portfolio_value", "portfolio_value_bps", "portfolio_value_dollars"),
        ]:
            if wire in data and data[wire] is not None:
                data[legacy] = _dollars_to_cents(data[wire])
                data[new_bps] = _dollars_to_bps_round(data[wire])
        return data


class Position(BaseModel, extra="ignore"):
    """A position in a market from GET /portfolio/positions.

    The `position` field is the authoritative contract count (P7/P15).
    Negative = NO contracts, positive = YES contracts.
    Unlike GET /portfolio/orders, this never archives — it always
    reflects the true state on Kalshi.

    Dual-unit migration (bps/fp100): each money/count field has a
    ``_bps`` / ``_fp100`` sibling alongside the legacy cents/int field.
    Legacy fields are deleted in Task 13 of
    ``docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md``.

    Post March 12, 2026: _fp/_dollars fields replace integer fields.
    """

    ticker: str
    # Legacy integer-cents / integer-contract fields.
    position: int = 0
    total_traded: int = 0
    market_exposure: int = 0
    resting_orders_count: int = 0
    realized_pnl: int = 0
    fees_paid: int = 0
    # New bps / fp100 fields (exact precision — preferred).
    position_fp100: int = 0
    total_traded_bps: int = 0
    market_exposure_bps: int = 0
    realized_pnl_bps: int = 0
    fees_paid_bps: int = 0
    resting_orders_count_fp100: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        log_unknown_fields("Position", data, cls.model_fields.keys() | _POSITION_FP_FIELDS)
        # Dollars → cents (legacy, lossy) + bps (new, AGGREGATE-rounded).
        # These are sums across fills/trades — Kalshi legitimately emits
        # sub-bps precision (6-decimal) values. Use the rounding parser;
        # per-contract prices use the strict parser elsewhere.
        for legacy, new_bps, wire in [
            ("total_traded", "total_traded_bps", "total_traded_dollars"),
            ("market_exposure", "market_exposure_bps", "market_exposure_dollars"),
            ("realized_pnl", "realized_pnl_bps", "realized_pnl_dollars"),
            ("fees_paid", "fees_paid_bps", "fees_paid_dollars"),
        ]:
            if wire in data and data[wire] is not None:
                data[legacy] = _dollars_to_cents(data[wire])
                data[new_bps] = _dollars_to_bps_round(data[wire])
        # FP → int (legacy, floor) + fp100 (new, exact).
        for legacy, new_fp100, wire in [
            ("position", "position_fp100", "position_fp"),
            ("resting_orders_count", "resting_orders_count_fp100", "resting_orders_count_fp"),
        ]:
            if wire in data and data[wire] is not None:
                data[legacy] = _fp_to_int(data[wire])
                data[new_fp100] = _fp_to_fp100(data[wire])
        return data


class EventPosition(BaseModel, extra="ignore"):
    """An aggregate position across an event (from Kalshi event_positions).

    Rich fields give Kalshi's live P&L per event — authoritative data (P21).

    Dual-unit migration (bps/fp100): each money/count field has a
    ``_bps`` / ``_fp100`` sibling alongside the legacy cents/int field.
    Legacy fields are deleted in Task 13 of
    ``docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md``.
    """

    event_ticker: str
    # Legacy integer-cents / integer-contract fields.
    total_cost: int = 0
    total_cost_shares: int = 0
    event_exposure: int = 0
    realized_pnl: int = 0
    resting_orders_count: int = 0
    fees_paid: int = 0
    # New bps / fp100 fields (exact precision — preferred).
    total_cost_bps: int = 0
    event_exposure_bps: int = 0
    realized_pnl_bps: int = 0
    fees_paid_bps: int = 0
    total_cost_shares_fp100: int = 0
    resting_orders_count_fp100: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        log_unknown_fields(
            "EventPosition", data, cls.model_fields.keys() | _EVENT_POSITION_FP_FIELDS
        )
        # Dollars → cents (legacy, lossy) + bps (new, AGGREGATE-rounded).
        # Kalshi emits sums with 6-decimal precision (sub-bps); use the
        # rounding parser rather than fail-closed strict.
        for legacy, new_bps, wire in [
            ("total_cost", "total_cost_bps", "total_cost_dollars"),
            ("event_exposure", "event_exposure_bps", "event_exposure_dollars"),
            ("realized_pnl", "realized_pnl_bps", "realized_pnl_dollars"),
            ("fees_paid", "fees_paid_bps", "fees_paid_dollars"),
        ]:
            if wire in data and data[wire] is not None:
                data[legacy] = _dollars_to_cents(data[wire])
                data[new_bps] = _dollars_to_bps_round(data[wire])
        # FP → int (legacy, floor) + fp100 (new, exact).
        for legacy, new_fp100, wire in [
            ("total_cost_shares", "total_cost_shares_fp100", "total_cost_shares_fp"),
            ("resting_orders_count", "resting_orders_count_fp100", "resting_orders_count_fp"),
        ]:
            if wire in data and data[wire] is not None:
                data[legacy] = _fp_to_int(data[wire])
                data[new_fp100] = _fp_to_fp100(data[wire])
        return data


class Settlement(BaseModel, extra="ignore"):
    """A settled market from GET /portfolio/settlements.

    CAUTION: Mixed units in the same response!
    - ``revenue`` is an integer in cents (NOT a dollars string)
    - ``fee_cost`` is a dollars string (NOT a cents integer)

    Dual-unit migration (bps/fp100): each money/count field has a
    ``_bps`` / ``_fp100`` sibling alongside the legacy cents/int field.
    Legacy fields are deleted in Task 13 of
    ``docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md``.

    ``revenue_bps`` is populated as ``revenue * 100`` because the wire
    ``revenue`` is already int cents (not a dollar string) — do NOT run
    it through ``_dollars_to_bps``.
    """

    ticker: str
    event_ticker: str = ""
    market_result: str = ""
    # Legacy integer-cents / integer-contract fields.
    revenue: int = 0
    fee_cost: int = 0
    yes_count: int = 0
    no_count: int = 0
    yes_total_cost: int = 0
    no_total_cost: int = 0
    settled_time: str = ""
    value: int | None = None
    # New bps / fp100 fields (exact precision — preferred).
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
        # revenue is already cents integer — leave legacy alone, promote to bps
        # exactly (cents * 100 = bps). Do NOT route through _dollars_to_bps:
        # revenue is NOT a dollars string.
        if "revenue" in data and isinstance(data["revenue"], int):
            data["revenue_bps"] = data["revenue"] * 100
        # fee_cost arrives as a dollars string from the API — convert to cents.
        # Also handle fee_cost_dollars (FP variant) if present. Settlement
        # aggregates fee across all fills on a market; use aggregate-rounded
        # parser (Kalshi emits sub-bps precision on sums).
        if "fee_cost_dollars" in data and data["fee_cost_dollars"] is not None:
            wire_fee = data["fee_cost_dollars"]
            data["fee_cost"] = _dollars_to_cents(wire_fee)
            data["fee_cost_bps"] = _dollars_to_bps_round(wire_fee)
        elif "fee_cost" in data and isinstance(data["fee_cost"], str):
            wire_fee = data["fee_cost"]
            data["fee_cost"] = _dollars_to_cents(wire_fee)
            data["fee_cost_bps"] = _dollars_to_bps_round(wire_fee)
        # Dollars → cents (legacy, lossy) + bps (new, AGGREGATE-rounded).
        # yes_total_cost / no_total_cost sum costs across fills.
        for legacy, new_bps, wire in [
            ("yes_total_cost", "yes_total_cost_bps", "yes_total_cost_dollars"),
            ("no_total_cost", "no_total_cost_bps", "no_total_cost_dollars"),
        ]:
            if wire in data and data[wire] is not None:
                data[legacy] = _dollars_to_cents(data[wire])
                data[new_bps] = _dollars_to_bps_round(data[wire])
        # FP → int (legacy, floor) + fp100 (new, exact).
        for legacy, new_fp100, wire in [
            ("yes_count", "yes_count_fp100", "yes_count_fp"),
            ("no_count", "no_count_fp100", "no_count_fp"),
        ]:
            if wire in data and data[wire] is not None:
                data[legacy] = _fp_to_int(data[wire])
                data[new_fp100] = _fp_to_fp100(data[wire])
        # value (per-contract payout) — may be dollars string.
        if "value_dollars" in data and data["value_dollars"] is not None:
            data["value"] = _dollars_to_cents(data["value_dollars"])
            data["value_bps"] = _dollars_to_bps(data["value_dollars"])
        return data


class ExchangeStatus(BaseModel):
    """Exchange operational status."""

    trading_active: bool
    exchange_active: bool
