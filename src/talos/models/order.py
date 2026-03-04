"""Pydantic models for Kalshi orders and fills."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Order(BaseModel, extra="ignore"):
    """A Kalshi order — matches Kalshi REST API response schema."""

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
    queue_position: int | None = None


class Fill(BaseModel, extra="ignore"):
    """A single fill (partial or full order execution)."""

    trade_id: str
    order_id: str
    ticker: str
    side: str
    yes_price: int = 0
    no_price: int = 0
    count: int = 0
    created_time: str = ""


class BatchOrderResult(BaseModel, extra="ignore"):
    """Result of a single order in a batch operation."""

    order_id: str = ""
    success: bool = False
    error: str | None = None
