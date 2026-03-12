"""Pydantic models for Kalshi WebSocket messages.

Post March 12, 2026: integer fields removed from WS payloads.
Validators convert _dollars/_fp string fields to int cents/int counts.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, model_validator


def _dollars_to_cents(val: Any) -> int | None:
    """Convert a _dollars string/float to integer cents."""
    if val is None:
        return None
    return round(float(val) * 100)


def _fp_to_int(val: Any) -> int | None:
    """Convert an _fp string to integer."""
    if val is None:
        return None
    return int(float(val))


class OrderBookSnapshot(BaseModel):
    """Full orderbook snapshot received on subscription.

    Post March 12: yes/no arrays replaced by yes_dollars_fp/no_dollars_fp
    with [["dollars_str", "fp_str"], ...] format. Validator converts back
    to [[cents_int, qty_int], ...] so OrderBookManager is unchanged.
    """

    market_ticker: str
    market_id: str
    yes: list[list[int]] = []
    no: list[list[int]] = []

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for old, new in [("yes", "yes_dollars_fp"), ("no", "no_dollars_fp")]:
            if new in data and data[new] is not None:
                converted = []
                for pair in data[new]:
                    p, q = pair[0], pair[1]
                    if isinstance(p, str):
                        p = round(float(p) * 100)
                    if isinstance(q, str):
                        q = int(float(q))
                    converted.append([p, q])
                data[old] = converted
        return data


class OrderBookDelta(BaseModel):
    """Incremental orderbook change.

    Post March 12: price_dollars and delta_fp replace price and delta.
    Validator converts new fields to int for downstream compatibility.
    """

    market_ticker: str
    market_id: str
    price: int
    delta: int
    side: Literal["yes", "no"]
    ts: str

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "price_dollars" in data and data["price_dollars"] is not None:
            data["price"] = round(float(data["price_dollars"]) * 100)
        if "delta_fp" in data and data["delta_fp"] is not None:
            data["delta"] = int(float(data["delta_fp"]))
        return data


class TickerMessage(BaseModel):
    """Market ticker update.

    Post March 12: _dollars/_fp fields replace integer fields.
    """

    market_ticker: str
    yes_bid: int | None = None
    yes_ask: int | None = None
    no_bid: int | None = None
    no_ask: int | None = None
    last_price: int | None = None
    volume: int | None = None
    open_interest: int | None = None
    dollar_volume: int | None = None
    dollar_open_interest: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for old, new in [
            ("yes_bid", "yes_bid_dollars"),
            ("yes_ask", "yes_ask_dollars"),
            ("no_bid", "no_bid_dollars"),
            ("no_ask", "no_ask_dollars"),
            ("last_price", "last_price_dollars"),
            ("dollar_volume", "dollar_volume_dollars"),
            ("dollar_open_interest", "dollar_open_interest_dollars"),
        ]:
            if new in data and data[new] is not None:
                data[old] = _dollars_to_cents(data[new])
        for old, new in [
            ("volume", "volume_fp"),
            ("open_interest", "open_interest_fp"),
        ]:
            if new in data and data[new] is not None:
                data[old] = _fp_to_int(data[new])
        return data


class TradeMessage(BaseModel):
    """Public trade on a market.

    Post March 12: _dollars/_fp fields replace integer fields.
    """

    market_ticker: str
    price: int
    count: int
    side: Literal["yes", "no"]
    ts: str
    trade_id: str

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # Price from dollars
        for field in ("yes_price_dollars", "no_price_dollars"):
            if field in data and data[field] is not None:
                data["price"] = round(float(data[field]) * 100)
                break
        if "count_fp" in data and data["count_fp"] is not None:
            data["count"] = int(float(data["count_fp"]))
        return data


class WSSubscribed(BaseModel):
    """Server confirmation of a subscription."""

    channel: str
    sid: int


class WSError(BaseModel):
    """Server error message."""

    code: int
    msg: str


class UserOrderMessage(BaseModel, extra="ignore"):
    """Real-time order state update from the user_orders WS channel.

    Fired whenever any of your orders changes state (fill, cancel, amend).
    Note: channel name is ``user_orders`` (plural), message type is ``user_order`` (singular).
    """

    order_id: str
    ticker: str
    status: str = ""
    side: str = ""
    is_yes: bool = False
    yes_price: int = 0
    no_price: int = 0
    fill_count: int = 0
    remaining_count: int = 0
    initial_count: int = 0
    maker_fill_cost: int = 0
    taker_fill_cost: int = 0
    maker_fees: int = 0
    taker_fees: int = 0
    client_order_id: str = ""
    created_time: str = ""
    last_update_time: str = ""

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for old, new in [
            ("yes_price", "yes_price_dollars"),
            ("no_price", "no_price_dollars"),
            ("maker_fill_cost", "maker_fill_cost_dollars"),
            ("taker_fill_cost", "taker_fill_cost_dollars"),
            ("maker_fees", "maker_fees_dollars"),
            ("taker_fees", "taker_fees_dollars"),
        ]:
            if new in data and data[new] is not None:
                data[old] = _dollars_to_cents(data[new])
        for old, new in [
            ("fill_count", "fill_count_fp"),
            ("remaining_count", "remaining_count_fp"),
            ("initial_count", "initial_count_fp"),
        ]:
            if new in data and data[new] is not None:
                data[old] = _fp_to_int(data[new])
        return data


class FillMessage(BaseModel, extra="ignore"):
    """Per-fill event from the fill WS channel.

    Fired for each individual trade execution on your orders.
    ``post_position`` is Kalshi's authoritative position after this fill
    (negative = NO contracts, positive = YES).
    """

    trade_id: str
    order_id: str
    market_ticker: str
    is_taker: bool = False
    side: str = ""
    action: str = ""
    yes_price: int = 0
    count: int = 0
    fee_cost: int = 0
    post_position: int = 0
    purchased_side: str = ""
    ts: int = 0
    client_order_id: str = ""

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "yes_price_dollars" in data and data["yes_price_dollars"] is not None:
            data["yes_price"] = _dollars_to_cents(data["yes_price_dollars"])
        if "fee_cost" in data and isinstance(data["fee_cost"], str):
            data["fee_cost"] = _dollars_to_cents(data["fee_cost"])
        for old, new in [
            ("count", "count_fp"),
            ("post_position", "post_position_fp"),
        ]:
            if new in data and data[new] is not None:
                data[old] = _fp_to_int(data[new])
        return data


class MarketPositionMessage(BaseModel, extra="ignore"):
    """Real-time position update from the market_positions WS channel.

    All monetary values arrive in centi-cents (1/10,000th dollar) as integers
    OR as _dollars strings. Validator normalizes to cents.
    """

    market_ticker: str
    position: int = 0
    position_cost: int = 0
    realized_pnl: int = 0
    fees_paid: int = 0
    volume: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "position_fp" in data and data["position_fp"] is not None:
            data["position"] = _fp_to_int(data["position_fp"])
        for old, new in [
            ("position_cost", "position_cost_dollars"),
            ("realized_pnl", "realized_pnl_dollars"),
            ("fees_paid", "fees_paid_dollars"),
        ]:
            if new in data and data[new] is not None:
                data[old] = _dollars_to_cents(data[new])
        if "volume_fp" in data and data["volume_fp"] is not None:
            data["volume"] = _fp_to_int(data["volume_fp"])
        return data


class MarketLifecycleMessage(BaseModel, extra="ignore"):
    """Market lifecycle event from the market_lifecycle_v2 WS channel.

    ``event_type`` determines which optional fields are populated:
    - ``created``: open_ts, close_ts, metadata
    - ``determined``: result, settlement_value, determination_ts
    - ``settled``: settled_ts
    - ``deactivated``: is_deactivated (true=paused, false=unpaused)
    - ``close_date_updated``: close_ts
    """

    event_type: str
    market_ticker: str
    result: str = ""
    settlement_value: int | None = None
    is_deactivated: bool | None = None
    close_ts: int | None = None
    settled_ts: int | None = None
    open_ts: int | None = None
    determination_ts: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "settlement_value" in data and isinstance(data["settlement_value"], str):
            data["settlement_value"] = _dollars_to_cents(data["settlement_value"])
        return data
