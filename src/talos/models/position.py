"""Pydantic models for position tracking and P&L summaries."""

from __future__ import annotations

from pydantic import BaseModel

from talos.automation_config import DEFAULT_UNIT_SIZE


class LegSummary(BaseModel):
    """Aggregated state of one leg (ticker) in an arb pair.

    Money-scale fields are in bps ($1 = 10_000 bps); count fields remain in
    whole contracts for operator display. See
    ``docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md``.
    """

    ticker: str
    no_price_bps: int
    filled_count: int
    resting_count: int
    total_fill_cost_bps: int = 0
    total_fees_bps: int = 0
    queue_position: int | None = None
    cpm: float | None = None
    cpm_partial: bool = False
    eta_minutes: float | None = None
    frequency: float | None = None
    flow_burst_ratio: float | None = None
    resting_no_price_bps: int | None = None


class EventPositionSummary(BaseModel):
    """Matched-pair P&L summary for one event's arb position.

    Money-scale fields are in bps ($1 = 10_000 bps).
    """

    event_ticker: str
    leg_a: LegSummary
    leg_b: LegSummary
    matched_pairs: int
    locked_profit_bps: float
    unmatched_a: int
    unmatched_b: int
    exposure_bps: int
    unit_size: int = DEFAULT_UNIT_SIZE
    status: str = ""
    kalshi_pnl_bps: int | None = None
