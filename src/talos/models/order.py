"""Pydantic models for Kalshi orders and fills."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from talos.models._converters import dollars_to_cents as _dollars_to_cents
from talos.models._converters import fp_to_int as _fp_to_int
from talos.models._converters import log_unknown_fields

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

    Post March 12, 2026: integer fields removed. The validator converts
    _dollars/_fp string fields to int cents/int counts for internal use.
    """

    order_id: str
    ticker: str
    action: str = "buy"
    side: str
    type: str = Field(default="limit", alias="type")
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

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        log_unknown_fields("Order", data, cls.model_fields.keys() | _ORDER_FP_FIELDS)
        # Dollars → cents
        for old, new in [
            ("yes_price", "yes_price_dollars"),
            ("no_price", "no_price_dollars"),
            ("taker_fees", "taker_fees_dollars"),
            ("maker_fees", "maker_fees_dollars"),
            ("maker_fill_cost", "maker_fill_cost_dollars"),
            ("taker_fill_cost", "taker_fill_cost_dollars"),
        ]:
            if new in data and data[new] is not None:
                data[old] = _dollars_to_cents(data[new])
        # FP → int
        for old, new in [
            ("fill_count", "fill_count_fp"),
            ("remaining_count", "remaining_count_fp"),
            ("initial_count", "initial_count_fp"),
        ]:
            if new in data and data[new] is not None:
                data[old] = _fp_to_int(data[new])
        return data


class Fill(BaseModel, extra="ignore"):
    """A single fill (partial or full order execution).

    Post March 12, 2026: _dollars/_fp fields replace integer fields.
    """

    trade_id: str
    order_id: str
    ticker: str
    side: str
    yes_price: int = 0
    no_price: int = 0
    count: int = 0
    fee_cost: int = 0
    action: str = ""
    is_taker: bool = False
    purchased_side: str = ""
    created_time: str = ""

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        log_unknown_fields("Fill", data, cls.model_fields.keys() | _FILL_FP_FIELDS)
        for old, new in [
            ("yes_price", "yes_price_dollars"),
            ("no_price", "no_price_dollars"),
        ]:
            if new in data and data[new] is not None:
                data[old] = _dollars_to_cents(data[new])
        # fee_cost arrives as a FixedPointDollars string
        if "fee_cost" in data and isinstance(data["fee_cost"], str):
            data["fee_cost"] = _dollars_to_cents(data["fee_cost"])
        if "count_fp" in data and data["count_fp"] is not None:
            data["count"] = _fp_to_int(data["count_fp"])
        return data


class BatchOrderResult(BaseModel, extra="ignore"):
    """Result of a single order in a batch operation."""

    order_id: str = ""
    success: bool = False
    error: str | None = None
