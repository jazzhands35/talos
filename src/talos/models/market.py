"""Pydantic models for Kalshi market data."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, model_validator


class OrderBookLevel(BaseModel):
    """A single price level in the orderbook."""

    price: int
    quantity: int


class Market(BaseModel):
    """A Kalshi market (contract)."""

    ticker: str
    event_ticker: str
    title: str
    status: str
    yes_bid: int | None = None
    yes_ask: int | None = None
    no_bid: int | None = None
    no_ask: int | None = None
    volume: int | None = None
    open_interest: int | None = None
    last_price: int | None = None


class Event(BaseModel):
    """A Kalshi event containing one or more markets."""

    event_ticker: str
    series_ticker: str
    title: str
    category: str
    status: str
    markets: list[Market] = []


class Series(BaseModel):
    """A Kalshi series (template for events)."""

    series_ticker: str
    title: str
    category: str
    tags: list[str] = []


class OrderBook(BaseModel):
    """Orderbook snapshot for a market.

    Raw API returns [[price, qty], ...] arrays — we parse into OrderBookLevel.
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
            for side in ("yes", "no"):
                levels = data.get(side)
                if levels and isinstance(levels, list) and levels and isinstance(levels[0], list):
                    data[side] = [{"price": p[0], "quantity": p[1]} for p in levels]
        return data


class Trade(BaseModel):
    """A single trade execution."""

    ticker: str
    trade_id: str
    price: int
    count: int
    side: Literal["yes", "no"]
    created_time: str
