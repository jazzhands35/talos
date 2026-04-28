"""Write-only SQLite data collector for ML training data.

Captures every observable event in Talos: scans, game adds, orders,
fills, market snapshots, settlements, and event outcomes with trap
analysis. No reads at runtime — analysis happens offline.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    events_found INTEGER,
    events_eligible INTEGER,
    events_selected INTEGER,
    series_scanned INTEGER,
    duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS scan_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    scan_id INTEGER,
    event_ticker TEXT,
    series_ticker TEXT,
    sport TEXT,
    league TEXT,
    title TEXT,
    sub_title TEXT,
    volume_a INTEGER,
    volume_b INTEGER,
    no_bid_a INTEGER,
    no_ask_a INTEGER,
    no_bid_b INTEGER,
    no_ask_b INTEGER,
    edge REAL,
    selected INTEGER
);

CREATE TABLE IF NOT EXISTS game_adds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_ticker TEXT,
    series_ticker TEXT,
    sport TEXT,
    league TEXT,
    source TEXT,
    ticker_a TEXT,
    ticker_b TEXT,
    volume_a INTEGER,
    volume_b INTEGER,
    fee_type TEXT,
    fee_rate REAL,
    scheduled_start TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_ticker TEXT,
    order_id TEXT,
    ticker TEXT,
    side TEXT,
    action TEXT,
    status TEXT,
    price INTEGER,
    initial_count INTEGER,
    fill_count INTEGER,
    remaining_count INTEGER,
    maker_fill_cost INTEGER,
    maker_fees INTEGER,
    source TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_ticker TEXT,
    trade_id TEXT,
    order_id TEXT,
    ticker TEXT,
    side TEXT,
    price INTEGER,
    count INTEGER,
    fee_cost INTEGER,
    is_taker INTEGER,
    post_position INTEGER,
    queue_position INTEGER,
    time_since_order REAL
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_ticker TEXT,
    ticker_a TEXT,
    ticker_b TEXT,
    no_a INTEGER,
    no_b INTEGER,
    edge REAL,
    volume_a INTEGER,
    volume_b INTEGER,
    open_interest_a INTEGER,
    open_interest_b INTEGER,
    game_state TEXT,
    status TEXT,
    filled_a INTEGER,
    filled_b INTEGER,
    resting_a INTEGER,
    resting_b INTEGER
);

CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_ticker TEXT,
    ticker TEXT,
    event_type TEXT,
    result TEXT,
    settlement_value INTEGER,
    total_pnl INTEGER
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_ticker TEXT,
    ticker TEXT,
    side TEXT,
    trigger TEXT,
    outcome TEXT,
    reason TEXT,
    book_top INTEGER,
    resting_price INTEGER,
    resting_count INTEGER,
    new_price INTEGER,
    effective_this REAL,
    effective_other REAL,
    fee_edge REAL,
    exit_only INTEGER
);
CREATE INDEX IF NOT EXISTS idx_decisions_event_ts
    ON decisions(event_ticker, ts);

CREATE TABLE IF NOT EXISTS event_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_ticker TEXT,
    sport TEXT,
    league TEXT,
    filled_a INTEGER,
    filled_b INTEGER,
    avg_price_a REAL,
    avg_price_b REAL,
    total_cost_a INTEGER,
    total_cost_b INTEGER,
    total_fees_a INTEGER,
    total_fees_b INTEGER,
    result_a TEXT,
    result_b TEXT,
    revenue INTEGER,
    total_pnl INTEGER,
    trapped INTEGER,
    trap_side TEXT,
    trap_delta INTEGER,
    trap_loss INTEGER,
    game_state_at_fill TEXT,
    time_to_start REAL,
    fill_duration REAL
);
"""


class DataCollector:
    """Write-only SQLite collector for trading data."""

    def __init__(self, db_path: Path) -> None:
        self._db = sqlite3.connect(str(db_path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()
        logger.info("data_collector_initialized", path=str(db_path))

    def close(self) -> None:
        self._db.close()

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()

    def _insert(self, table: str, **kwargs: Any) -> int:
        cols = list(kwargs.keys())
        placeholders = ", ".join(["?"] * len(cols))
        col_names = ", ".join(cols)
        values = [kwargs[c] for c in cols]
        try:
            cur = self._db.execute(
                f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})",
                values,
            )
            self._db.commit()
            return cur.lastrowid or 0
        except Exception:
            logger.warning("data_collector_insert_failed", table=table, exc_info=True)
            return 0

    # ── Scan ──────────────────────────────────────────────────────

    def log_scan(
        self,
        *,
        events_found: int,
        events_eligible: int,
        events_selected: int,
        series_scanned: int,
        duration_ms: int,
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        """Log a scan invocation and its discovered events."""
        scan_id = self._insert(
            "scan_results",
            ts=self._now(),
            events_found=events_found,
            events_eligible=events_eligible,
            events_selected=events_selected,
            series_scanned=series_scanned,
            duration_ms=duration_ms,
        )
        if events:
            for ev in events:
                self._insert(
                    "scan_events",
                    ts=self._now(),
                    scan_id=scan_id,
                    **ev,
                )

    # ── Game adds ─────────────────────────────────────────────────

    def log_game_add(
        self,
        *,
        event_ticker: str,
        series_ticker: str = "",
        sport: str = "",
        league: str = "",
        source: str = "manual",
        ticker_a: str = "",
        ticker_b: str = "",
        volume_a: int = 0,
        volume_b: int = 0,
        fee_type: str = "",
        fee_rate: float = 0.0,
        scheduled_start: str | None = None,
    ) -> None:
        """Log a game being added to monitoring."""
        self._insert(
            "game_adds",
            ts=self._now(),
            event_ticker=event_ticker,
            series_ticker=series_ticker,
            sport=sport,
            league=league,
            source=source,
            ticker_a=ticker_a,
            ticker_b=ticker_b,
            volume_a=volume_a,
            volume_b=volume_b,
            fee_type=fee_type,
            fee_rate=fee_rate,
            scheduled_start=scheduled_start,
        )

    # ── Orders ────────────────────────────────────────────────────

    def log_order(
        self,
        *,
        event_ticker: str,
        order_id: str,
        ticker: str,
        side: str,
        action: str = "buy",
        status: str = "resting",
        price: int = 0,
        initial_count: int = 0,
        fill_count: int = 0,
        remaining_count: int = 0,
        maker_fill_cost: int = 0,
        maker_fees: int = 0,
        source: str = "",
    ) -> None:
        """Log an order state change."""
        self._insert(
            "orders",
            ts=self._now(),
            event_ticker=event_ticker,
            order_id=order_id,
            ticker=ticker,
            side=side,
            action=action,
            status=status,
            price=price,
            initial_count=initial_count,
            fill_count=fill_count,
            remaining_count=remaining_count,
            maker_fill_cost=maker_fill_cost,
            maker_fees=maker_fees,
            source=source,
        )

    # ── Fills ─────────────────────────────────────────────────────

    def log_fill(
        self,
        *,
        event_ticker: str,
        trade_id: str,
        order_id: str,
        ticker: str,
        side: str,
        price: int = 0,
        count: int = 0,
        fee_cost: int = 0,
        is_taker: bool = False,
        post_position: int = 0,
        queue_position: int | None = None,
        time_since_order: float | None = None,
    ) -> None:
        """Log an individual fill from the WS fill channel."""
        self._insert(
            "fills",
            ts=self._now(),
            event_ticker=event_ticker,
            trade_id=trade_id,
            order_id=order_id,
            ticker=ticker,
            side=side,
            price=price,
            count=count,
            fee_cost=fee_cost,
            is_taker=1 if is_taker else 0,
            post_position=post_position,
            queue_position=queue_position,
            time_since_order=time_since_order,
        )

    # ── Decisions ─────────────────────────────────────────────────

    def log_decision(
        self,
        *,
        event_ticker: str,
        ticker: str = "",
        side: str = "",
        trigger: str,
        outcome: str,
        reason: str = "",
        book_top: int | None = None,
        resting_price: int | None = None,
        resting_count: int | None = None,
        new_price: int | None = None,
        effective_this: float | None = None,
        effective_other: float | None = None,
        fee_edge: float | None = None,
        exit_only: bool | None = None,
    ) -> None:
        """Record an evaluation decision — including silent skips.

        Every exit path of BidAdjuster.evaluate_jump and every silent
        short-circuit in the engine should call this so the timeline
        in the review panel can show what Talos decided (or declined
        to decide) at each moment.
        """
        self._insert(
            "decisions",
            ts=self._now(),
            event_ticker=event_ticker,
            ticker=ticker,
            side=side,
            trigger=trigger,
            outcome=outcome,
            reason=reason,
            book_top=book_top,
            resting_price=resting_price,
            resting_count=resting_count,
            new_price=new_price,
            effective_this=effective_this,
            effective_other=effective_other,
            fee_edge=fee_edge,
            exit_only=None if exit_only is None else (1 if exit_only else 0),
        )

    # ── Market snapshots ──────────────────────────────────────────

    def log_market_snapshots(self, snapshots: list[dict[str, Any]]) -> None:
        """Bulk insert market snapshots for all monitored events."""
        if not snapshots:
            return
        ts = self._now()
        try:
            # Build SQL once — all snapshots share the same schema.
            cols = ["ts"] + list(snapshots[0].keys())
            col_names = ", ".join(cols)
            placeholders = ", ".join(["?"] * len(cols))
            sql = f"INSERT INTO market_snapshots ({col_names}) VALUES ({placeholders})"
            rows = [[ts] + [snap[c] for c in snapshots[0]] for snap in snapshots]
            self._db.executemany(sql, rows)
            self._db.commit()
        except Exception:
            logger.warning("data_collector_snapshot_batch_failed", exc_info=True)

    # ── Settlements ───────────────────────────────────────────────

    def log_settlement(
        self,
        *,
        event_ticker: str,
        ticker: str,
        event_type: str,
        result: str = "",
        settlement_value: int = 0,
        total_pnl: int | None = None,
    ) -> None:
        """Log a market determination or settlement."""
        self._insert(
            "settlements",
            ts=self._now(),
            event_ticker=event_ticker,
            ticker=ticker,
            event_type=event_type,
            result=result,
            settlement_value=settlement_value,
            total_pnl=total_pnl,
        )

    # ── Event outcomes ────────────────────────────────────────────

    def log_event_outcome(
        self,
        *,
        event_ticker: str,
        sport: str = "",
        league: str = "",
        filled_a: int = 0,
        filled_b: int = 0,
        avg_price_a: float = 0.0,
        avg_price_b: float = 0.0,
        total_cost_a: int = 0,
        total_cost_b: int = 0,
        total_fees_a: int = 0,
        total_fees_b: int = 0,
        result_a: str = "",
        result_b: str = "",
        revenue: int = 0,
        total_pnl: int = 0,
        game_state_at_fill: str = "",
        time_to_start: float | None = None,
        fill_duration: float | None = None,
    ) -> None:
        """Log the final outcome of an event with trap analysis."""
        trapped = 1 if filled_a != filled_b else 0
        if trapped:
            trap_side = "A" if filled_a > filled_b else "B"
            trap_delta = abs(filled_a - filled_b)
            # Estimate trap loss: what we'd have earned balanced vs what we got
            balanced = min(filled_a, filled_b)
            if balanced > 0:
                balanced_pnl = revenue * balanced // max(filled_a, filled_b)
                trap_loss = total_pnl - balanced_pnl
            else:
                trap_loss = total_pnl  # all loss is from the trap
        else:
            trap_side = None
            trap_delta = 0
            trap_loss = None

        self._insert(
            "event_outcomes",
            ts=self._now(),
            event_ticker=event_ticker,
            sport=sport,
            league=league,
            filled_a=filled_a,
            filled_b=filled_b,
            avg_price_a=avg_price_a,
            avg_price_b=avg_price_b,
            total_cost_a=total_cost_a,
            total_cost_b=total_cost_b,
            total_fees_a=total_fees_a,
            total_fees_b=total_fees_b,
            result_a=result_a,
            result_b=result_b,
            revenue=revenue,
            total_pnl=total_pnl,
            trapped=trapped,
            trap_side=trap_side,
            trap_delta=trap_delta,
            trap_loss=trap_loss,
            game_state_at_fill=game_state_at_fill,
            time_to_start=time_to_start,
            fill_duration=fill_duration,
        )
