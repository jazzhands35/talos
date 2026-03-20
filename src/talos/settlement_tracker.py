"""Settlement P&L tracker — aggregates Kalshi settlements by time window."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")


def aggregate_settlements(
    settlements: list[dict[str, Any]],
    now_pt: datetime | None = None,
) -> dict[str, int]:
    """Aggregate settlement revenue and cost by time window.

    Returns dict with keys: today_pnl, today_invested, yesterday_pnl,
    yesterday_invested, week_pnl, week_invested.
    """
    if now_pt is None:
        now_pt = datetime.now(PT)

    today_start = now_pt.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    week_start = today_start - timedelta(days=6)

    buckets: dict[str, int] = {
        "today_pnl": 0,
        "today_invested": 0,
        "yesterday_pnl": 0,
        "yesterday_invested": 0,
        "week_pnl": 0,
        "week_invested": 0,
    }

    for s in settlements:
        settled_str = s.get("settled_time", "")
        if not settled_str:
            continue
        try:
            settled_dt = datetime.fromisoformat(settled_str.replace("Z", "+00:00")).astimezone(PT)
        except (ValueError, TypeError):
            continue

        revenue = s.get("revenue", 0)
        cost = s.get("no_total_cost", 0) + s.get("yes_total_cost", 0)
        profit = revenue - cost

        if settled_dt >= today_start:
            buckets["today_pnl"] += profit
            buckets["today_invested"] += cost
        if yesterday_start <= settled_dt < today_start:
            buckets["yesterday_pnl"] += profit
            buckets["yesterday_invested"] += cost
        if settled_dt >= week_start:
            buckets["week_pnl"] += profit
            buckets["week_invested"] += cost

    return buckets


def reconcile_event(
    our_revenue: int,
    kalshi_revenue: int,
    event_ticker: str,
) -> dict[str, Any] | None:
    """Compare our expected revenue vs Kalshi's actual.

    Returns None if they match, or a discrepancy dict if they differ.
    """
    if our_revenue == kalshi_revenue:
        return None
    return {
        "event_ticker": event_ticker,
        "our_revenue": our_revenue,
        "kalshi_revenue": kalshi_revenue,
        "difference": our_revenue - kalshi_revenue,
    }
