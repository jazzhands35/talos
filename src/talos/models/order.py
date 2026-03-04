"""Pydantic models for Kalshi orders and fills."""

from __future__ import annotations

from pydantic import BaseModel


class Order(BaseModel):
    """A Kalshi order."""

    order_id: str
    ticker: str
    side: str
    order_type: str
    price: int
    count: int
    remaining_count: int
    fill_count: int
    status: str
    created_time: str
    expiration_time: str | None = None


class Fill(BaseModel):
    """A single fill (partial or full order execution)."""

    trade_id: str
    order_id: str
    ticker: str
    side: str
    price: int
    count: int
    created_time: str


class BatchOrderResult(BaseModel):
    """Result of a single order in a batch operation."""

    order_id: str
    success: bool
    error: str | None = None
