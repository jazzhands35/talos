"""Pydantic models for Kalshi orders and fills."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from talos.models._converters import (
    dollars_to_bps as _dollars_to_bps,
)
from talos.models._converters import (
    dollars_to_cents as _dollars_to_cents,
)
from talos.models._converters import (
    fp_to_fp100 as _fp_to_fp100,
)
from talos.models._converters import (
    fp_to_int as _fp_to_int,
)
from talos.models._converters import (
    log_unknown_fields,
)

ACTIVE_STATUSES = frozenset({"resting", "executed"})

_ORDER_FP_FIELDS = frozenset({
    "yes_price_dollars", "no_price_dollars",
    "taker_fees_dollars", "maker_fees_dollars",
    "maker_fill_cost_dollars", "taker_fill_cost_dollars",
    "fill_count_fp", "remaining_count_fp", "initial_count_fp",
})

_FILL_FP_FIELDS = frozenset({
    "yes_price_dollars", "no_price_dollars", "count_fp",
})


class Order(BaseModel, extra="ignore"):
    """A Kalshi order — matches Kalshi REST API response schema.

    Dual-unit migration (bps/fp100): each money/count field has both a
    legacy integer-cents / integer-contracts attribute (``yes_price``,
    ``fill_count``, ...) and a new exact-precision bps / fp100 sibling
    (``yes_price_bps``, ``fill_count_fp100``, ...). Both populate from
    the same wire payload. Downstream callers migrate from the legacy
    names to the ``_bps`` / ``_fp100`` names incrementally; the legacy
    fields are deleted in Task 13 of
    ``docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md``
    once all callers have migrated.

    Post March 12, 2026: integer wire fields removed. The validator
    converts ``_dollars`` / ``_fp`` string fields into both the legacy
    and the new representations.
    """

    order_id: str
    ticker: str
    action: str = "buy"
    side: str
    type: str = Field(default="limit", alias="type")
    # Legacy integer-cents / integer-contract fields (lossy for sub-cent
    # / fractional markets — deprecated, removed in Task 13).
    yes_price: int = 0
    no_price: int = 0
    initial_count: int = 0
    remaining_count: int = 0
    fill_count: int = 0
    status: str = ""
    created_time: str = ""
    client_order_id: str | None = None
    expiration_time: str | None = None
    taker_fees: int = 0
    maker_fees: int = 0
    maker_fill_cost: int = 0
    taker_fill_cost: int = 0
    order_group_id: str | None = None
    queue_position: int | None = None
    # New bps / fp100 fields (exact precision — preferred).
    yes_price_bps: int = 0
    no_price_bps: int = 0
    initial_count_fp100: int = 0
    remaining_count_fp100: int = 0
    fill_count_fp100: int = 0
    taker_fees_bps: int = 0
    maker_fees_bps: int = 0
    maker_fill_cost_bps: int = 0
    taker_fill_cost_bps: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        log_unknown_fields("Order", data, cls.model_fields.keys() | _ORDER_FP_FIELDS)
        # Dollars → cents (legacy, lossy) + bps (new, exact).
        for legacy, new_bps, wire in [
            ("yes_price", "yes_price_bps", "yes_price_dollars"),
            ("no_price", "no_price_bps", "no_price_dollars"),
            ("taker_fees", "taker_fees_bps", "taker_fees_dollars"),
            ("maker_fees", "maker_fees_bps", "maker_fees_dollars"),
            ("maker_fill_cost", "maker_fill_cost_bps", "maker_fill_cost_dollars"),
            ("taker_fill_cost", "taker_fill_cost_bps", "taker_fill_cost_dollars"),
        ]:
            if wire in data and data[wire] is not None:
                data[legacy] = _dollars_to_cents(data[wire])
                data[new_bps] = _dollars_to_bps(data[wire])
        # FP → int (legacy, floor) + fp100 (new, exact).
        for legacy, new_fp100, wire in [
            ("fill_count", "fill_count_fp100", "fill_count_fp"),
            ("remaining_count", "remaining_count_fp100", "remaining_count_fp"),
            ("initial_count", "initial_count_fp100", "initial_count_fp"),
        ]:
            if wire in data and data[wire] is not None:
                data[legacy] = _fp_to_int(data[wire])
                data[new_fp100] = _fp_to_fp100(data[wire])
        return data


class Fill(BaseModel, extra="ignore"):
    """A single fill (partial or full order execution).

    Dual-unit migration: the same fields are exposed in legacy integer
    cents / integer contracts AND in bps / fp100. See :class:`Order` for
    the full migration contract.

    Post March 12, 2026: ``_dollars`` / ``_fp`` wire fields replace the
    integer wire fields.
    """

    trade_id: str
    order_id: str
    ticker: str
    side: str
    # Legacy integer-cents / integer-contract fields.
    yes_price: int = 0
    no_price: int = 0
    count: int = 0
    fee_cost: int = 0
    action: str = ""
    is_taker: bool = False
    purchased_side: str = ""
    created_time: str = ""
    # New bps / fp100 fields.
    yes_price_bps: int = 0
    no_price_bps: int = 0
    count_fp100: int = 0
    fee_cost_bps: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        log_unknown_fields("Fill", data, cls.model_fields.keys() | _FILL_FP_FIELDS)
        for legacy, new_bps, wire in [
            ("yes_price", "yes_price_bps", "yes_price_dollars"),
            ("no_price", "no_price_bps", "no_price_dollars"),
        ]:
            if wire in data and data[wire] is not None:
                data[legacy] = _dollars_to_cents(data[wire])
                data[new_bps] = _dollars_to_bps(data[wire])
        # fee_cost arrives as a FixedPointDollars string (dual-populate bps);
        # the integer-passthrough path is legacy-only and does not promote.
        if "fee_cost" in data and isinstance(data["fee_cost"], str):
            wire_fee = data["fee_cost"]
            data["fee_cost"] = _dollars_to_cents(wire_fee)
            data["fee_cost_bps"] = _dollars_to_bps(wire_fee)
        if "count_fp" in data and data["count_fp"] is not None:
            data["count"] = _fp_to_int(data["count_fp"])
            data["count_fp100"] = _fp_to_fp100(data["count_fp"])
        return data


class BatchOrderResult(BaseModel, extra="ignore"):
    """Result of a single order in a batch operation."""

    order_id: str = ""
    success: bool = False
    error: str | None = None
