"""Pydantic models for Kalshi market data."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, model_validator

from talos.models._converters import dollars_to_cents as _dollars_to_cents
from talos.models._converters import fp_to_int as _fp_to_int


class OrderBookLevel(BaseModel):
    """A single price level in the orderbook."""

    price: int
    quantity: int


class Market(BaseModel):
    """A Kalshi market (contract).

    Post March 12, 2026: integer cents fields removed from API responses.
    The validator converts _dollars/_fp string fields to int cents/int counts.
    """

    ticker: str
    event_ticker: str
    title: str
    status: str
    yes_bid: int | None = None
    yes_ask: int | None = None
    no_bid: int | None = None
    no_ask: int | None = None
    volume: int | None = None
    volume_24h: int | None = None
    open_interest: int | None = None
    last_price: int | None = None
    settlement_ts: str | None = None
    close_time: str | None = None
    open_time: str | None = None
    result: str = ""
    market_type: str = "binary"
    expected_expiration_time: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # Dollars → cents
        for old, new in [
            ("yes_bid", "yes_bid_dollars"),
            ("yes_ask", "yes_ask_dollars"),
            ("no_bid", "no_bid_dollars"),
            ("no_ask", "no_ask_dollars"),
            ("last_price", "last_price_dollars"),
        ]:
            if new in data and data[new] is not None:
                data[old] = _dollars_to_cents(data[new])
        # FP → int
        for old, new in [
            ("volume", "volume_fp"),
            ("volume_24h", "volume_24h_fp"),
            ("open_interest", "open_interest_fp"),
        ]:
            if new in data and data[new] is not None:
                data[old] = _fp_to_int(data[new])
        return data


class Event(BaseModel):
    """A Kalshi event containing one or more markets."""

    event_ticker: str
    series_ticker: str
    title: str
    sub_title: str = ""
    category: str
    status: str | None = None
    mutually_exclusive: bool | None = None
    markets: list[Market] = []


class Series(BaseModel):
    """A Kalshi series (template for events)."""

    series_ticker: str
    title: str
    category: str
    tags: list[str] = []
    fee_type: str = "quadratic_with_maker_fees"
    fee_multiplier: float = 0.0175
    frequency: str = ""
    settlement_sources: list[dict[str, Any]] = []

    @model_validator(mode="before")
    @classmethod
    def _coerce_nullable_lists(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if data.get("tags") is None:
            data["tags"] = []
        if data.get("settlement_sources") is None:
            data["settlement_sources"] = []
        return data


class OrderBook(BaseModel):
    """Orderbook snapshot for a market.

    Raw API returns [[price, qty], ...] arrays — we parse into OrderBookLevel.
    Post March 12: levels may be [["dollars_str", "fp_str"], ...] strings.
    """

    market_ticker: str
    yes: list[OrderBookLevel]
    no: list[OrderBookLevel]

    @classmethod
    def _parse_levels(cls, raw: list[list[int]]) -> list[OrderBookLevel]:
        return [OrderBookLevel(price=pair[0], quantity=pair[1]) for pair in raw]

    @model_validator(mode="before")
    @classmethod
    def _coerce_levels(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Handle orderbook_fp wrapper (new API may nest under this key)
            if "orderbook_fp" in data and "yes" not in data:
                inner = data["orderbook_fp"]
                if isinstance(inner, dict):
                    data.update(inner)
            # REST returns yes_dollars/no_dollars; WS returns yes_dollars_fp/no_dollars_fp.
            # Normalize to yes/no before parsing levels.
            for side, rest_key, ws_key in [
                ("yes", "yes_dollars", "yes_dollars_fp"),
                ("no", "no_dollars", "no_dollars_fp"),
            ]:
                if side not in data or not data[side]:
                    for alt in (rest_key, ws_key):
                        if alt in data and data[alt]:
                            data[side] = data[alt]
                            break
            for side in ("yes", "no"):
                levels = data.get(side)
                if levels and isinstance(levels, list) and levels and isinstance(levels[0], list):
                    coerced = []
                    for pair in levels:
                        # New format: ["0.52", "10.00"] (dollars str, fp str)
                        # Old format: [52, 10] (cents int, qty int)
                        p, q = pair[0], pair[1]
                        if isinstance(p, str):
                            p = _dollars_to_cents(p)
                        if isinstance(q, str):
                            q = _fp_to_int(q)
                        coerced.append({"price": p, "quantity": q})
                    data[side] = coerced
        return data


class Trade(BaseModel):
    """A single trade execution.

    The Kalshi API returns ``taker_side`` (not ``side``) and ``price`` as a
    dollar float (not cents int).  The validator normalizes both so downstream
    code always sees ``side`` as a string and ``price`` as cents.

    Post March 12: _dollars/_fp fields replace integer fields.
    """

    ticker: str
    trade_id: str
    price: int
    count: int
    side: str
    created_time: str
    yes_price: int | None = None
    no_price: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # API returns taker_side, normalize to side
            if "taker_side" in data and "side" not in data:
                data["side"] = data["taker_side"]
            # FP migration: _dollars → cents, _fp → int
            for old, new in [
                ("yes_price", "yes_price_dollars"),
                ("no_price", "no_price_dollars"),
            ]:
                if new in data and data[new] is not None:
                    data[old] = _dollars_to_cents(data[new])
            if "count_fp" in data and data["count_fp"] is not None:
                data["count"] = _fp_to_int(data["count_fp"])
            # API returns price as float (dollars), normalize to cents
            if "price" in data:
                p = data["price"]
                if isinstance(p, float) and p <= 1.0:
                    data["price"] = round(p * 100)
            # If price missing but yes_price present, derive it
            if "price" not in data and "yes_price" in data:
                data["price"] = data["yes_price"]
        return data
