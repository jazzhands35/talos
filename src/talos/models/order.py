"""Pydantic models for Kalshi orders and fills."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from talos.models._converters import (
    dollars_to_bps as _dollars_to_bps,
)
from talos.models._converters import (
    fp_to_fp100 as _fp_to_fp100,
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

    Money/count fields use bps (basis points, 1/10,000 of a dollar) and
    fp100 (1/100 of a contract) for exact precision. The validator
    converts ``_dollars`` / ``_fp`` wire strings into the ``_bps`` /
    ``_fp100`` fields via the Decimal parsers in :mod:`talos.units`.

    Task 13a-2a (2026-04-23): legacy integer-cents / integer-contracts
    fields (``yes_price``, ``fill_count``, ``taker_fees``, ...) were
    removed. All downstream code reads ``_bps`` / ``_fp100`` directly.
    """

    order_id: str
    ticker: str
    action: str = "buy"
    side: str
    type: str = Field(default="limit", alias="type")
    status: str = ""
    created_time: str = ""
    client_order_id: str | None = None
    expiration_time: str | None = None
    order_group_id: str | None = None
    queue_position: int | None = None
    # bps / fp100 fields (exact precision).
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
        # Dollars → bps (exact).
        for new_bps, wire in [
            ("yes_price_bps", "yes_price_dollars"),
            ("no_price_bps", "no_price_dollars"),
            ("taker_fees_bps", "taker_fees_dollars"),
            ("maker_fees_bps", "maker_fees_dollars"),
            ("maker_fill_cost_bps", "maker_fill_cost_dollars"),
            ("taker_fill_cost_bps", "taker_fill_cost_dollars"),
        ]:
            if wire in data and data[wire] is not None:
                data[new_bps] = _dollars_to_bps(data[wire])
        # FP → fp100 (exact).
        for new_fp100, wire in [
            ("fill_count_fp100", "fill_count_fp"),
            ("remaining_count_fp100", "remaining_count_fp"),
            ("initial_count_fp100", "initial_count_fp"),
        ]:
            if wire in data and data[wire] is not None:
                data[new_fp100] = _fp_to_fp100(data[wire])
        return data


class Fill(BaseModel, extra="ignore"):
    """A single fill (partial or full order execution).

    Money/count fields use bps and fp100 for exact precision. See
    :class:`Order` for the full contract.

    Task 13a-2a (2026-04-23): legacy integer-cents / integer-contracts
    fields were removed.
    """

    trade_id: str
    order_id: str
    ticker: str
    side: str
    action: str = ""
    is_taker: bool = False
    purchased_side: str = ""
    created_time: str = ""
    # bps / fp100 fields.
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
        for new_bps, wire in [
            ("yes_price_bps", "yes_price_dollars"),
            ("no_price_bps", "no_price_dollars"),
        ]:
            if wire in data and data[wire] is not None:
                data[new_bps] = _dollars_to_bps(data[wire])
        # fee_cost arrives as a FixedPointDollars string; dual-populate bps.
        if "fee_cost" in data and isinstance(data["fee_cost"], str):
            data["fee_cost_bps"] = _dollars_to_bps(data["fee_cost"])
            # fee_cost was a legacy field; remove it so Pydantic does not
            # try to assign a string to a (now non-existent) int field.
            del data["fee_cost"]
        if "count_fp" in data and data["count_fp"] is not None:
            data["count_fp100"] = _fp_to_fp100(data["count_fp"])
        return data


class BatchOrderResult(BaseModel, extra="ignore"):
    """Result of a single order in a batch operation."""

    order_id: str = ""
    success: bool = False
    error: str | None = None
