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
        ]:
            if new in data and data[new] is not None:
                data[old] = _dollars_to_cents(data[new])
        if "volume_fp" in data and data["volume_fp"] is not None:
            data["volume"] = _fp_to_int(data["volume_fp"])
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
