"""Pydantic models for position tracking and P&L summaries."""

from __future__ import annotations

from pydantic import BaseModel


class LegSummary(BaseModel):
    """Aggregated state of one leg (ticker) in an arb pair."""

    ticker: str
    no_price: int
    filled_count: int
    resting_count: int
    total_fill_cost: int = 0
    total_fees: int = 0
    queue_position: int | None = None
    cpm: float | None = None
    cpm_partial: bool = False
    eta_minutes: float | None = None


class EventPositionSummary(BaseModel):
    """Matched-pair P&L summary for one event's arb position."""

    event_ticker: str
    leg_a: LegSummary
    leg_b: LegSummary
    matched_pairs: int
    locked_profit_cents: float
    unmatched_a: int
    unmatched_b: int
    exposure_cents: int
    status: str = ""
