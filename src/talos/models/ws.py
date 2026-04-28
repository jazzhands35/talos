"""Pydantic models for Kalshi WebSocket messages.

Post March 12, 2026: integer fields removed from WS payloads.
Validators convert ``_dollars`` / ``_fp`` string fields to exact-precision
bps / fp100 integers.

Task 13a-2d (2026-04-23): legacy integer-cents / integer-contracts fields
deleted from all 8 WS message classes. Bps/fp100 fields are the sole
representation.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, model_validator

from talos.models._converters import dollars_to_bps as _dollars_to_bps
from talos.models._converters import fp_to_fp100 as _fp_to_fp100
from talos.units import complement_bps


class OrderBookSnapshot(BaseModel):
    """Full orderbook snapshot received on subscription.

    Post March 12: ``yes_dollars_fp`` / ``no_dollars_fp`` carry
    ``[["dollars_str", "fp_str"], ...]`` pairs that the validator
    converts to ``[[price_bps, quantity_fp100], ...]``.
    """

    market_ticker: str
    market_id: str
    yes_bps_fp100: list[list[int]] = []
    no_bps_fp100: list[list[int]] = []

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # Convert legacy [[price, qty], ...] integer wire to bps_fp100 via ×100.
        for legacy_key, new_key in [
            ("yes", "yes_bps_fp100"),
            ("no", "no_bps_fp100"),
        ]:
            if legacy_key in data and data[legacy_key] and new_key not in data:
                pairs = data.pop(legacy_key)
                if isinstance(pairs, list) and pairs and isinstance(pairs[0], list):
                    data[new_key] = [[p * 100, q * 100] for p, q in pairs]
        # _dollars_fp wire: ["0.52", "10.00"] strings → exact bps/fp100.
        for new_key, wire in [
            ("yes_bps_fp100", "yes_dollars_fp"),
            ("no_bps_fp100", "no_dollars_fp"),
        ]:
            if wire in data and data[wire] is not None:
                new_pairs: list[list[int]] = []
                for pair in data[wire]:
                    p, q = pair[0], pair[1]
                    new_p = _dollars_to_bps(p) if isinstance(p, str) else p * 100
                    new_q = _fp_to_fp100(q) if isinstance(q, str) else q * 100
                    new_pairs.append([new_p, new_q])
                data[new_key] = new_pairs
        return data


class OrderBookDelta(BaseModel):
    """Incremental orderbook change.

    Post March 12: ``price_dollars`` / ``delta_fp`` replace integer fields.
    """

    market_ticker: str
    market_id: str
    side: Literal["yes", "no"]
    ts: str
    price_bps: int = 0
    delta_fp100: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "price_dollars" in data and data["price_dollars"] is not None:
            data["price_bps"] = _dollars_to_bps(data["price_dollars"])
        elif "price" in data and data["price"] is not None:
            # Legacy integer wire — promote cents → bps.
            data["price_bps"] = data["price"] * 100
            del data["price"]
        if "delta_fp" in data and data["delta_fp"] is not None:
            data["delta_fp100"] = _fp_to_fp100(data["delta_fp"])
        elif "delta" in data and data["delta"] is not None:
            # Legacy integer wire — promote contracts → fp100.
            data["delta_fp100"] = data["delta"] * 100
            del data["delta"]
        return data


class TickerMessage(BaseModel):
    """Market ticker update.

    Post March 12: _dollars/_fp fields replace integer fields.
    WS sends yes_bid_dollars/yes_ask_dollars only (no NO-side fields).
    NO-side prices are derived: no_bid_bps = complement_bps(yes_ask_bps).
    Last price arrives as ``price_dollars`` (not ``last_price_dollars``).
    """

    market_ticker: str
    yes_bid_bps: int | None = None
    yes_ask_bps: int | None = None
    no_bid_bps: int | None = None
    no_ask_bps: int | None = None
    last_price_bps: int | None = None
    volume_fp100: int | None = None
    open_interest_fp100: int | None = None
    dollar_volume_bps: int | None = None
    dollar_open_interest_bps: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # Dollars → bps (exact).
        for new_bps, wire in [
            ("yes_bid_bps", "yes_bid_dollars"),
            ("yes_ask_bps", "yes_ask_dollars"),
            # WS uses price_dollars for last traded price
            ("last_price_bps", "price_dollars"),
            # REST uses last_price_dollars — accept both (later entry wins)
            ("last_price_bps", "last_price_dollars"),
            ("dollar_volume_bps", "dollar_volume_dollars"),
            ("dollar_open_interest_bps", "dollar_open_interest_dollars"),
        ]:
            if wire in data and data[wire] is not None:
                data[new_bps] = _dollars_to_bps(data[wire])
        # FP → fp100 (exact).
        for new_fp100, wire in [
            ("volume_fp100", "volume_fp"),
            ("open_interest_fp100", "open_interest_fp"),
        ]:
            if wire in data and data[wire] is not None:
                data[new_fp100] = _fp_to_fp100(data[wire])
        # WS only sends YES-side BBA — derive NO-side from binary complement.
        if data.get("yes_ask_bps") is not None and data.get("no_bid_bps") is None:
            data["no_bid_bps"] = complement_bps(data["yes_ask_bps"])
        if data.get("yes_bid_bps") is not None and data.get("no_ask_bps") is None:
            data["no_ask_bps"] = complement_bps(data["yes_bid_bps"])
        return data


class TradeMessage(BaseModel):
    """Public trade on a market.

    Post March 12: _dollars/_fp fields replace integer fields.
    AsyncAPI spec uses ``taker_side`` instead of ``side``.
    """

    market_ticker: str
    side: Literal["yes", "no"]
    ts: str
    trade_id: str
    price_bps: int = 0
    count_fp100: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # Price from dollars — populate bps (exact).
        for field in ("yes_price_dollars", "no_price_dollars"):
            if field in data and data[field] is not None:
                data["price_bps"] = _dollars_to_bps(data[field])
                break
        # Legacy integer wire — promote.
        if "price_bps" not in data and "price" in data and data["price"] is not None:
            data["price_bps"] = data["price"] * 100
            del data["price"]
        if "count_fp" in data and data["count_fp"] is not None:
            data["count_fp100"] = _fp_to_fp100(data["count_fp"])
        elif "count" in data and data["count"] is not None:
            data["count_fp100"] = data["count"] * 100
            del data["count"]
        # AsyncAPI spec renamed side → taker_side
        if "taker_side" in data and "side" not in data:
            data["side"] = data["taker_side"]
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
    Note: channel name is ``user_orders`` (plural), message type is
    ``user_order`` (singular).

    NO-side bps derivation via :func:`talos.units.complement_bps`.
    """

    order_id: str
    ticker: str
    status: str = ""
    side: str = ""
    is_yes: bool = False
    client_order_id: str = ""
    created_time: str = ""
    last_update_time: str = ""
    yes_price_bps: int = 0
    no_price_bps: int = 0
    maker_fill_cost_bps: int = 0
    taker_fill_cost_bps: int = 0
    maker_fees_bps: int = 0
    taker_fees_bps: int = 0
    fill_count_fp100: int = 0
    remaining_count_fp100: int = 0
    initial_count_fp100: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # Dollars → bps (exact).
        for new_bps, wire in [
            ("yes_price_bps", "yes_price_dollars"),
            ("no_price_bps", "no_price_dollars"),
            ("maker_fill_cost_bps", "maker_fill_cost_dollars"),
            ("taker_fill_cost_bps", "taker_fill_cost_dollars"),
            ("maker_fees_bps", "maker_fees_dollars"),
            ("taker_fees_bps", "taker_fees_dollars"),
        ]:
            if wire in data and data[wire] is not None:
                data[new_bps] = _dollars_to_bps(data[wire])
        # Legacy integer wire — promote to bps.
        for new_bps, legacy in [
            ("yes_price_bps", "yes_price"),
            ("no_price_bps", "no_price"),
            ("maker_fill_cost_bps", "maker_fill_cost"),
            ("taker_fill_cost_bps", "taker_fill_cost"),
            ("maker_fees_bps", "maker_fees"),
            ("taker_fees_bps", "taker_fees"),
        ]:
            if new_bps not in data and legacy in data and data[legacy] is not None:
                data[new_bps] = data[legacy] * 100
        # Kalshi WS only sends yes_price_dollars — derive no_price from it.
        if (
            "yes_price_bps" in data
            and data.get("no_price_bps") in (None, 0)
            and data["yes_price_bps"] is not None
        ):
            data["no_price_bps"] = complement_bps(data["yes_price_bps"])
        # FP → fp100 (exact).
        for new_fp100, wire in [
            ("fill_count_fp100", "fill_count_fp"),
            ("remaining_count_fp100", "remaining_count_fp"),
            ("initial_count_fp100", "initial_count_fp"),
        ]:
            if wire in data and data[wire] is not None:
                data[new_fp100] = _fp_to_fp100(data[wire])
        # Legacy integer wire — promote to fp100.
        for new_fp100, legacy in [
            ("fill_count_fp100", "fill_count"),
            ("remaining_count_fp100", "remaining_count"),
            ("initial_count_fp100", "initial_count"),
        ]:
            if new_fp100 not in data and legacy in data and data[legacy] is not None:
                data[new_fp100] = data[legacy] * 100
        return data


class FillMessage(BaseModel, extra="ignore"):
    """Per-fill event from the fill WS channel.

    Fired for each individual trade execution on your orders.
    ``post_position_fp100`` is Kalshi's authoritative position after this fill
    (negative = NO contracts, positive = YES).

    NO-side derived via :func:`talos.units.complement_bps`.
    """

    trade_id: str
    order_id: str
    market_ticker: str
    is_taker: bool = False
    side: str = ""
    action: str = ""
    purchased_side: str = ""
    ts: int = 0
    client_order_id: str = ""
    yes_price_bps: int = 0
    no_price_bps: int = 0
    count_fp100: int = 0
    fee_cost_bps: int = 0
    post_position_fp100: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "yes_price_dollars" in data and data["yes_price_dollars"] is not None:
            data["yes_price_bps"] = _dollars_to_bps(data["yes_price_dollars"])
        elif "yes_price" in data and data["yes_price"] is not None:
            data["yes_price_bps"] = data["yes_price"] * 100
        # Derive no_price from yes_price (WS only sends YES side).
        if (
            "yes_price_bps" in data
            and data.get("no_price_bps") in (None, 0)
            and data["yes_price_bps"] is not None
        ):
            data["no_price_bps"] = complement_bps(data["yes_price_bps"])
        # fee_cost arrives as a FixedPointDollars string (populate bps).
        if "fee_cost" in data and isinstance(data["fee_cost"], str):
            data["fee_cost_bps"] = _dollars_to_bps(data["fee_cost"])
            del data["fee_cost"]
        elif "fee_cost" in data and isinstance(data["fee_cost"], int):
            # Legacy integer-cents passthrough — promote.
            data["fee_cost_bps"] = data["fee_cost"] * 100
            del data["fee_cost"]
        for new_fp100, wire in [
            ("count_fp100", "count_fp"),
            ("post_position_fp100", "post_position_fp"),
        ]:
            if wire in data and data[wire] is not None:
                data[new_fp100] = _fp_to_fp100(data[wire])
        # Legacy integer wire — promote.
        for new_fp100, legacy in [
            ("count_fp100", "count"),
            ("post_position_fp100", "post_position"),
        ]:
            if new_fp100 not in data and legacy in data and data[legacy] is not None:
                data[new_fp100] = data[legacy] * 100
        return data


class MarketPositionMessage(BaseModel, extra="ignore"):
    """Real-time position update from the market_positions WS channel.

    All monetary values arrive as _dollars strings; the validator converts
    to bps. Counts arrive as _fp strings → fp100.
    """

    market_ticker: str
    position_fp100: int = 0
    position_cost_bps: int = 0
    realized_pnl_bps: int = 0
    fees_paid_bps: int = 0
    volume_fp100: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "position_fp" in data and data["position_fp"] is not None:
            data["position_fp100"] = _fp_to_fp100(data["position_fp"])
        elif "position" in data and data["position"] is not None:
            data["position_fp100"] = data["position"] * 100
            del data["position"]
        for new_bps, wire in [
            ("position_cost_bps", "position_cost_dollars"),
            ("realized_pnl_bps", "realized_pnl_dollars"),
            ("fees_paid_bps", "fees_paid_dollars"),
        ]:
            if wire in data and data[wire] is not None:
                data[new_bps] = _dollars_to_bps(data[wire])
        # Legacy integer wire — promote to bps.
        for new_bps, legacy in [
            ("position_cost_bps", "position_cost"),
            ("realized_pnl_bps", "realized_pnl"),
            ("fees_paid_bps", "fees_paid"),
        ]:
            if new_bps not in data and legacy in data and data[legacy] is not None:
                data[new_bps] = data[legacy] * 100
        if "volume_fp" in data and data["volume_fp"] is not None:
            data["volume_fp100"] = _fp_to_fp100(data["volume_fp"])
        elif "volume" in data and data["volume"] is not None:
            data["volume_fp100"] = data["volume"] * 100
            del data["volume"]
        return data


class MarketLifecycleMessage(BaseModel, extra="ignore"):
    """Market lifecycle event from the market_lifecycle_v2 WS channel.

    ``event_type`` determines which optional fields are populated:
    - ``created``: open_ts, close_ts, metadata
    - ``determined``: result, settlement_value_bps, determination_ts
    - ``settled``: settled_ts
    - ``deactivated``: is_deactivated (true=paused, false=unpaused)
    - ``close_date_updated``: close_ts
    """

    event_type: str
    market_ticker: str
    result: str = ""
    is_deactivated: bool | None = None
    close_ts: int | None = None
    settled_ts: int | None = None
    open_ts: int | None = None
    determination_ts: int | None = None
    settlement_value_bps: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "settlement_value" in data and isinstance(data["settlement_value"], str):
            data["settlement_value_bps"] = _dollars_to_bps(data["settlement_value"])
            del data["settlement_value"]
        elif "settlement_value" in data and isinstance(data["settlement_value"], int):
            # Legacy integer-cents passthrough — promote.
            data["settlement_value_bps"] = data["settlement_value"] * 100
            del data["settlement_value"]
        return data
