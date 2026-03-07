"""Pydantic models for Kalshi WebSocket messages."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class OrderBookSnapshot(BaseModel):
    """Full orderbook snapshot received on subscription."""

    market_ticker: str
    market_id: str
    yes: list[list[int]] = []
    no: list[list[int]] = []


class OrderBookDelta(BaseModel):
    """Incremental orderbook change."""

    market_ticker: str
    market_id: str
    price: int
    delta: int
    side: Literal["yes", "no"]
    ts: str
    price_dollars: float | None = None
    delta_fp: str | None = None
    client_order_id: str | None = None
    subaccount: int | None = None


class TickerMessage(BaseModel):
    """Market ticker update."""

    market_ticker: str
    yes_bid: int | None = None
    yes_ask: int | None = None
    no_bid: int | None = None
    no_ask: int | None = None
    last_price: int | None = None
    volume: int | None = None


class TradeMessage(BaseModel):
    """Public trade on a market."""

    market_ticker: str
    price: int
    count: int
    side: Literal["yes", "no"]
    ts: str
    trade_id: str


class WSSubscribed(BaseModel):
    """Server confirmation of a subscription."""

    channel: str
    sid: int


class WSError(BaseModel):
    """Server error message."""

    code: int
    msg: str
