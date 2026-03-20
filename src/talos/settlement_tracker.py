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
                settlement.revenue,
                settlement.fee_cost,
                settlement.no_count,
                settlement.no_total_cost,
                settlement.yes_count,
                settlement.yes_total_cost,
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
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def settlements_as_models(self) -> list[tuple[Settlement, int | None, str]]:
        """Return cached data as (Settlement, est_pnl_cents, sub_title) tuples."""
        rows = self.all_settlements()
        result = []
        for r in rows:
            s = Settlement(
                ticker=r["ticker"],
                event_ticker=r["event_ticker"],
                market_result=r["market_result"] or "",
                revenue=r["revenue"] or 0,
                fee_cost=r["fee_cost"] or 0,
                no_count=r["no_count"] or 0,
                no_total_cost=r["no_total_cost"] or 0,
                yes_count=r["yes_count"] or 0,
                yes_total_cost=r["yes_total_cost"] or 0,
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
