"""Pydantic models for Kalshi WebSocket messages.

Post March 12, 2026: integer fields removed from WS payloads.
Validators convert _dollars/_fp string fields to int cents/int counts.

Dual-unit migration (bps/fp100): each money/count field has both a
legacy integer-cents / integer-contracts attribute and a new
exact-precision ``_bps`` / ``_fp100`` sibling. Both populate from the
same wire payload. Downstream callers migrate from the legacy names
to the ``_bps`` / ``_fp100`` names incrementally; the legacy fields
are deleted in Task 13 of
``docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md``
once all callers have migrated.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, model_validator

from talos.models._converters import dollars_to_bps as _dollars_to_bps
from talos.models._converters import dollars_to_cents as _dollars_to_cents
from talos.models._converters import fp_to_fp100 as _fp_to_fp100
from talos.models._converters import fp_to_int as _fp_to_int
from talos.units import complement_bps


class OrderBookSnapshot(BaseModel):
    """Full orderbook snapshot received on subscription.

    Post March 12: yes/no arrays replaced by yes_dollars_fp/no_dollars_fp
    with [["dollars_str", "fp_str"], ...] format. Validator converts back
    to [[cents_int, qty_int], ...] so OrderBookManager is unchanged.

    Dual-unit migration: ``yes_bps_fp100`` / ``no_bps_fp100`` ship
    alongside the legacy arrays with exact-precision pairs
    ``[price_bps, quantity_fp100]``. See module docstring.
    """

    market_ticker: str
    market_id: str
    # Legacy integer-cents / integer-contract pairs (lossy for sub-cent
    # / fractional markets — deprecated, removed in Task 13).
    yes: list[list[int]] = []
    no: list[list[int]] = []
    # New bps / fp100 pairs (exact precision — preferred).
    yes_bps_fp100: list[list[int]] = []
    no_bps_fp100: list[list[int]] = []

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for legacy, new_bps_fp100, wire in [
            ("yes", "yes_bps_fp100", "yes_dollars_fp"),
            ("no", "no_bps_fp100", "no_dollars_fp"),
        ]:
            if wire in data and data[wire] is not None:
                legacy_pairs: list[list[int]] = []
                new_pairs: list[list[int]] = []
                for pair in data[wire]:
                    p, q = pair[0], pair[1]
                    # Dual-populate: legacy cents/int alongside exact bps/fp100.
                    if isinstance(p, str):
                        legacy_p = _dollars_to_cents(p)
                        new_p = _dollars_to_bps(p)
                    else:
                        legacy_p = p
                        new_p = 0
                    if isinstance(q, str):
                        legacy_q = _fp_to_int(q)
                        new_q = _fp_to_fp100(q)
                    else:
                        legacy_q = q
                        new_q = 0
                    legacy_pairs.append([legacy_p, legacy_q])
                    new_pairs.append([new_p, new_q])
                data[legacy] = legacy_pairs
                data[new_bps_fp100] = new_pairs
        return data


class OrderBookDelta(BaseModel):
    """Incremental orderbook change.

    Post March 12: price_dollars and delta_fp replace price and delta.
    Validator converts new fields to int for downstream compatibility.

    Dual-unit migration: ``price_bps`` / ``delta_fp100`` ship alongside.
    """

    market_ticker: str
    market_id: str
    # Legacy integer-cents / integer-contract fields.
    price: int
    delta: int
    side: Literal["yes", "no"]
    ts: str
    # New bps / fp100 fields.
    price_bps: int = 0
    delta_fp100: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "price_dollars" in data and data["price_dollars"] is not None:
            data["price"] = _dollars_to_cents(data["price_dollars"])
            data["price_bps"] = _dollars_to_bps(data["price_dollars"])
        if "delta_fp" in data and data["delta_fp"] is not None:
            data["delta"] = _fp_to_int(data["delta_fp"])
            data["delta_fp100"] = _fp_to_fp100(data["delta_fp"])
        return data


class TickerMessage(BaseModel):
    """Market ticker update.

    Post March 12: _dollars/_fp fields replace integer fields.
    WS sends yes_bid_dollars/yes_ask_dollars only (no NO-side fields).
    NO-side prices are derived: no_bid = 100 - yes_ask, no_ask = 100 - yes_bid.
    Last price arrives as ``price_dollars`` (not ``last_price_dollars``).

    Dual-unit migration: each money/count field gets a ``_bps`` /
    ``_fp100`` sibling. NO-side bps derivation uses
    :func:`talos.units.complement_bps`.
    """

    market_ticker: str
    # Legacy integer-cents / integer-contract fields.
    yes_bid: int | None = None
    yes_ask: int | None = None
    no_bid: int | None = None
    no_ask: int | None = None
    last_price: int | None = None
    volume: int | None = None
    open_interest: int | None = None
    dollar_volume: int | None = None
    dollar_open_interest: int | None = None
    # New bps / fp100 fields.
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
        # Dollars → cents (legacy) + bps (new, exact).
        for legacy, new_bps, wire in [
            ("yes_bid", "yes_bid_bps", "yes_bid_dollars"),
            ("yes_ask", "yes_ask_bps", "yes_ask_dollars"),
            # WS uses price_dollars for last traded price
            ("last_price", "last_price_bps", "price_dollars"),
            # REST uses last_price_dollars — accept both (later entry wins)
            ("last_price", "last_price_bps", "last_price_dollars"),
            ("dollar_volume", "dollar_volume_bps", "dollar_volume_dollars"),
            ("dollar_open_interest", "dollar_open_interest_bps",
             "dollar_open_interest_dollars"),
        ]:
            if wire in data and data[wire] is not None:
                data[legacy] = _dollars_to_cents(data[wire])
                data[new_bps] = _dollars_to_bps(data[wire])
        # FP → int (legacy, floor) + fp100 (new, exact).
        for legacy, new_fp100, wire in [
            ("volume", "volume_fp100", "volume_fp"),
            ("open_interest", "open_interest_fp100", "open_interest_fp"),
        ]:
            if wire in data and data[wire] is not None:
                data[legacy] = _fp_to_int(data[wire])
                data[new_fp100] = _fp_to_fp100(data[wire])
        # WS only sends YES-side BBA — derive NO-side from binary complement.
        if data.get("yes_ask") is not None and data.get("no_bid") is None:
            data["no_bid"] = 100 - data["yes_ask"]
        if data.get("yes_bid") is not None and data.get("no_ask") is None:
            data["no_ask"] = 100 - data["yes_bid"]
        # Parallel bps derivation via complement_bps.
        if data.get("yes_ask_bps") is not None and data.get("no_bid_bps") is None:
            data["no_bid_bps"] = complement_bps(data["yes_ask_bps"])
        if data.get("yes_bid_bps") is not None and data.get("no_ask_bps") is None:
            data["no_ask_bps"] = complement_bps(data["yes_bid_bps"])
        return data


class TradeMessage(BaseModel):
    """Public trade on a market.

    Post March 12: _dollars/_fp fields replace integer fields.
    AsyncAPI spec uses ``taker_side`` instead of ``side``.

    Dual-unit migration: ``price_bps`` / ``count_fp100`` siblings.
    """

    market_ticker: str
    # Legacy integer-cents / integer-contract fields.
    price: int
    count: int
    side: Literal["yes", "no"]
    ts: str
    trade_id: str
    # New bps / fp100 fields.
    price_bps: int = 0
    count_fp100: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # Price from dollars — dual-populate cents (legacy) and bps (new).
        for field in ("yes_price_dollars", "no_price_dollars"):
            if field in data and data[field] is not None:
                data["price"] = _dollars_to_cents(data[field])
                data["price_bps"] = _dollars_to_bps(data[field])
                break
        if "count_fp" in data and data["count_fp"] is not None:
            data["count"] = _fp_to_int(data["count_fp"])
            data["count_fp100"] = _fp_to_fp100(data["count_fp"])
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
    Note: channel name is ``user_orders`` (plural), message type is ``user_order`` (singular).

    Dual-unit migration: 6 money + 3 count siblings; NO-side bps
    derivation via :func:`talos.units.complement_bps`.
    """

    order_id: str
    ticker: str
    status: str = ""
    side: str = ""
    is_yes: bool = False
    # Legacy integer-cents / integer-contract fields.
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
    # New bps / fp100 fields.
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
        # Dollars → cents (legacy) + bps (new, exact).
        for legacy, new_bps, wire in [
            ("yes_price", "yes_price_bps", "yes_price_dollars"),
            ("no_price", "no_price_bps", "no_price_dollars"),
            ("maker_fill_cost", "maker_fill_cost_bps", "maker_fill_cost_dollars"),
            ("taker_fill_cost", "taker_fill_cost_bps", "taker_fill_cost_dollars"),
            ("maker_fees", "maker_fees_bps", "maker_fees_dollars"),
            ("taker_fees", "taker_fees_bps", "taker_fees_dollars"),
        ]:
            if wire in data and data[wire] is not None:
                data[legacy] = _dollars_to_cents(data[wire])
                data[new_bps] = _dollars_to_bps(data[wire])
        # Kalshi WS only sends yes_price_dollars — derive no_price from it.
        if (
            "yes_price" in data
            and data.get("no_price") in (None, 0)
            and data["yes_price"] is not None
        ):
            data["no_price"] = 100 - data["yes_price"]
        if (
            "yes_price_bps" in data
            and data.get("no_price_bps") in (None, 0)
            and data["yes_price_bps"] is not None
        ):
            data["no_price_bps"] = complement_bps(data["yes_price_bps"])
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


class FillMessage(BaseModel, extra="ignore"):
    """Per-fill event from the fill WS channel.

    Fired for each individual trade execution on your orders.
    ``post_position`` is Kalshi's authoritative position after this fill
    (negative = NO contracts, positive = YES).

    Dual-unit migration: ``yes_price_bps`` / ``no_price_bps`` (NO derived
    via :func:`talos.units.complement_bps`), ``count_fp100``,
    ``fee_cost_bps``, ``post_position_fp100``.
    """

    trade_id: str
    order_id: str
    market_ticker: str
    is_taker: bool = False
    side: str = ""
    action: str = ""
    # Legacy integer-cents / integer-contract fields.
    yes_price: int = 0
    no_price: int = 0
    count: int = 0
    fee_cost: int = 0
    post_position: int = 0
    purchased_side: str = ""
    ts: int = 0
    client_order_id: str = ""
    # New bps / fp100 fields.
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
            data["yes_price"] = _dollars_to_cents(data["yes_price_dollars"])
            data["yes_price_bps"] = _dollars_to_bps(data["yes_price_dollars"])
        # Derive no_price from yes_price (WS only sends YES side).
        if (
            "yes_price" in data
            and data.get("no_price") in (None, 0)
            and data["yes_price"] is not None
        ):
            data["no_price"] = 100 - data["yes_price"]
        if (
            "yes_price_bps" in data
            and data.get("no_price_bps") in (None, 0)
            and data["yes_price_bps"] is not None
        ):
            data["no_price_bps"] = complement_bps(data["yes_price_bps"])
        # fee_cost arrives as a FixedPointDollars string (dual-populate bps);
        # the integer-passthrough path is legacy-only and does not promote.
        if "fee_cost" in data and isinstance(data["fee_cost"], str):
            wire_fee = data["fee_cost"]
            data["fee_cost"] = _dollars_to_cents(wire_fee)
            data["fee_cost_bps"] = _dollars_to_bps(wire_fee)
        for legacy, new_fp100, wire in [
            ("count", "count_fp100", "count_fp"),
            ("post_position", "post_position_fp100", "post_position_fp"),
        ]:
            if wire in data and data[wire] is not None:
                data[legacy] = _fp_to_int(data[wire])
                data[new_fp100] = _fp_to_fp100(data[wire])
        return data


class MarketPositionMessage(BaseModel, extra="ignore"):
    """Real-time position update from the market_positions WS channel.

    All monetary values arrive in centi-cents (1/10,000th dollar) as integers
    OR as _dollars strings. Validator normalizes to cents.

    Dual-unit migration: ``position_fp100``, ``position_cost_bps``,
    ``realized_pnl_bps``, ``fees_paid_bps``, ``volume_fp100``.
    """

    market_ticker: str
    # Legacy integer-cents / integer-contract fields.
    position: int = 0
    position_cost: int = 0
    realized_pnl: int = 0
    fees_paid: int = 0
    volume: int = 0
    # New bps / fp100 fields.
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
            data["position"] = _fp_to_int(data["position_fp"])
            data["position_fp100"] = _fp_to_fp100(data["position_fp"])
        for legacy, new_bps, wire in [
            ("position_cost", "position_cost_bps", "position_cost_dollars"),
            ("realized_pnl", "realized_pnl_bps", "realized_pnl_dollars"),
            ("fees_paid", "fees_paid_bps", "fees_paid_dollars"),
        ]:
            if wire in data and data[wire] is not None:
                data[legacy] = _dollars_to_cents(data[wire])
                data[new_bps] = _dollars_to_bps(data[wire])
        if "volume_fp" in data and data["volume_fp"] is not None:
            data["volume"] = _fp_to_int(data["volume_fp"])
            data["volume_fp100"] = _fp_to_fp100(data["volume_fp"])
        return data


class MarketLifecycleMessage(BaseModel, extra="ignore"):
    """Market lifecycle event from the market_lifecycle_v2 WS channel.

    ``event_type`` determines which optional fields are populated:
    - ``created``: open_ts, close_ts, metadata
    - ``determined``: result, settlement_value, determination_ts
    - ``settled``: settled_ts
    - ``deactivated``: is_deactivated (true=paused, false=unpaused)
    - ``close_date_updated``: close_ts

    Dual-unit migration: ``settlement_value_bps`` sibling.
    """

    event_type: str
    market_ticker: str
    result: str = ""
    # Legacy integer-cents field.
    settlement_value: int | None = None
    is_deactivated: bool | None = None
    close_ts: int | None = None
    settled_ts: int | None = None
    open_ts: int | None = None
    determination_ts: int | None = None
    # New bps field.
    settlement_value_bps: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "settlement_value" in data and isinstance(data["settlement_value"], str):
            wire = data["settlement_value"]
            data["settlement_value"] = _dollars_to_cents(wire)
            data["settlement_value_bps"] = _dollars_to_bps(wire)
        return data
