"""Settlement P&L tracker — aggregates Kalshi settlements by time window.

Includes SettlementCache for persistent SQLite storage of settlement
data with our estimated P&L captured at settlement time.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from talos.models.portfolio import Settlement
from talos.units import ONE_CENT_BPS, ONE_CONTRACT_FP100, bps_to_cents_round

logger = structlog.get_logger()

PT = ZoneInfo("America/Los_Angeles")

_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS settlement_cache (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT NOT NULL,
    market_result TEXT,
    revenue INTEGER DEFAULT 0,
    fee_cost INTEGER DEFAULT 0,
    no_count INTEGER DEFAULT 0,
    no_total_cost INTEGER DEFAULT 0,
    yes_count INTEGER DEFAULT 0,
    yes_total_cost INTEGER DEFAULT 0,
    settled_time TEXT,
    est_pnl_cents INTEGER,
    sub_title TEXT
);
CREATE INDEX IF NOT EXISTS idx_sc_event ON settlement_cache(event_ticker);
CREATE INDEX IF NOT EXISTS idx_sc_time ON settlement_cache(settled_time);
"""


class SettlementCache:
    """Persistent SQLite cache for settlement data with estimated P&L."""

    def __init__(self, db_path: Path | str) -> None:
        self._db = sqlite3.connect(str(db_path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_CACHE_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def latest_settled_time(self) -> str | None:
        """Return the most recent settled_time in cache, or None if empty."""
        row = self._db.execute(
            "SELECT MAX(settled_time) FROM settlement_cache"
        ).fetchone()
        return row[0] if row and row[0] else None

    def upsert(
        self,
        settlement: Settlement,
        est_pnl_cents: int | None = None,
        sub_title: str = "",
    ) -> None:
        """Insert or update a single settlement."""
        self._db.execute(
            """INSERT INTO settlement_cache
               (ticker, event_ticker, market_result, revenue, fee_cost,
                no_count, no_total_cost, yes_count, yes_total_cost,
                settled_time, est_pnl_cents, sub_title)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                 revenue=excluded.revenue,
                 fee_cost=excluded.fee_cost,
                 market_result=excluded.market_result,
                 no_count=excluded.no_count,
                 no_total_cost=excluded.no_total_cost,
                 yes_count=excluded.yes_count,
                 yes_total_cost=excluded.yes_total_cost,
                 settled_time=excluded.settled_time,
                 est_pnl_cents=COALESCE(excluded.est_pnl_cents, settlement_cache.est_pnl_cents),
                 sub_title=COALESCE(NULLIF(excluded.sub_title, ''), settlement_cache.sub_title)
            """,
            (
                settlement.ticker,
                settlement.event_ticker,
                settlement.market_result,
                bps_to_cents_round(settlement.revenue_bps),
                bps_to_cents_round(settlement.fee_cost_bps),
                settlement.no_count_fp100 // ONE_CONTRACT_FP100,
                bps_to_cents_round(settlement.no_total_cost_bps),
                settlement.yes_count_fp100 // ONE_CONTRACT_FP100,
                bps_to_cents_round(settlement.yes_total_cost_bps),
                settlement.settled_time,
                est_pnl_cents,
                sub_title,
            ),
        )
        self._db.commit()

    def upsert_batch(
        self,
        settlements: list[Settlement],
        est_pnl_map: dict[str, int] | None = None,
        subtitles: dict[str, str] | None = None,
    ) -> None:
        """Batch upsert settlements."""
        est = est_pnl_map or {}
        subs = subtitles or {}
        for s in settlements:
            self.upsert(
                s,
                est_pnl_cents=est.get(s.event_ticker),
                sub_title=subs.get(s.event_ticker, ""),
            )

    def all_settlements(self) -> list[dict[str, Any]]:
        """Return all cached settlements as dicts."""
        cur = self._db.execute(
            "SELECT * FROM settlement_cache ORDER BY settled_time DESC"
        )
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def settlements_as_models(self) -> list[tuple[Settlement, int | None, str]]:
        """Return cached data as (Settlement, est_pnl_cents, sub_title) tuples.

        The SQL columns store integer cents / whole contracts — promote
        back to exact bps / fp100 at the Pydantic boundary.
        """
        rows = self.all_settlements()
        result = []
        for r in rows:
            revenue_cents = r["revenue"] or 0
            fee_cost_cents = r["fee_cost"] or 0
            no_total_cost_cents = r["no_total_cost"] or 0
            yes_total_cost_cents = r["yes_total_cost"] or 0
            no_count_whole = r["no_count"] or 0
            yes_count_whole = r["yes_count"] or 0
            s = Settlement(
                ticker=r["ticker"],
                event_ticker=r["event_ticker"],
                market_result=r["market_result"] or "",
                revenue_bps=revenue_cents * ONE_CENT_BPS,
                fee_cost_bps=fee_cost_cents * ONE_CENT_BPS,
                no_count_fp100=no_count_whole * ONE_CONTRACT_FP100,
                no_total_cost_bps=no_total_cost_cents * ONE_CENT_BPS,
                yes_count_fp100=yes_count_whole * ONE_CONTRACT_FP100,
                yes_total_cost_bps=yes_total_cost_cents * ONE_CENT_BPS,
                settled_time=r["settled_time"] or "",
            )
            result.append((s, r.get("est_pnl_cents"), r.get("sub_title", "")))
        return result


def aggregate_settlements(
    settlements: list[dict[str, Any]],
    now_pt: datetime | None = None,
) -> dict[str, int]:
    """Aggregate settlement revenue and cost by time window.

    Returns dict with keys: today_pnl, today_invested, yesterday_pnl,
    yesterday_invested, week_pnl, week_invested,
    d24h_pnl, d24h_events, d7d_pnl, d7d_events, d30d_pnl, d30d_events.
    """
    if now_pt is None:
        now_pt = datetime.now(PT)

    today_start = now_pt.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    week_start = today_start - timedelta(days=6)
    h24_start = now_pt - timedelta(hours=24)
    d7_start = now_pt - timedelta(days=7)
    d30_start = now_pt - timedelta(days=30)

    buckets: dict[str, int] = {
        "today_pnl": 0,
        "today_invested": 0,
        "yesterday_pnl": 0,
        "yesterday_invested": 0,
        "week_pnl": 0,
        "week_invested": 0,
        "d24h_pnl": 0,
        "d24h_events": 0,
        "d7d_pnl": 0,
        "d7d_events": 0,
        "d30d_pnl": 0,
        "d30d_events": 0,
    }
    # Count unique events, not individual market settlements
    events_24h: set[str] = set()
    events_7d: set[str] = set()
    events_30d: set[str] = set()

    for s in settlements:
        settled_str = s.get("settled_time", "")
        if not settled_str:
            continue
        try:
            settled_dt = datetime.fromisoformat(settled_str.replace("Z", "+00:00")).astimezone(PT)
        except (ValueError, TypeError):
            continue

        revenue = s.get("revenue", 0)
        # Same-ticker YES/NO pairs: Kalshi nets positions, reporting
        # revenue=0 even though each matched pair settles at 100¢.
        # Add back the implicit payout for matched pairs.
        yes_count = s.get("yes_count", 0)
        no_count = s.get("no_count", 0)
        implicit_revenue = min(yes_count, no_count) * 100
        cost = s.get("no_total_cost", 0) + s.get("yes_total_cost", 0)
        fees = s.get("fee_cost", 0)
        profit = revenue + implicit_revenue - cost - fees
        evt = s.get("event_ticker", "")

        if settled_dt >= today_start:
            buckets["today_pnl"] += profit
            buckets["today_invested"] += cost
        if yesterday_start <= settled_dt < today_start:
            buckets["yesterday_pnl"] += profit
            buckets["yesterday_invested"] += cost
        if settled_dt >= week_start:
            buckets["week_pnl"] += profit
            buckets["week_invested"] += cost
        if settled_dt >= h24_start:
            buckets["d24h_pnl"] += profit
            events_24h.add(evt)
        if settled_dt >= d7_start:
            buckets["d7d_pnl"] += profit
            events_7d.add(evt)
        if settled_dt >= d30_start:
            buckets["d30d_pnl"] += profit
            events_30d.add(evt)

    buckets["d24h_events"] = len(events_24h)
    buckets["d7d_events"] = len(events_7d)
    buckets["d30d_events"] = len(events_30d)

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
