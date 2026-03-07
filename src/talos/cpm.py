"""Trade flow tracking and CPM/ETA computation.

CPM (Contracts Per Minute) measures how fast a market is trading.
ETA estimates how long until a resting order fills based on queue position and CPM.
"""

from __future__ import annotations

import math
import time
from datetime import datetime

from talos.models.market import Trade


def _parse_iso(ts: str) -> float:
    """Parse ISO 8601 timestamp to Unix seconds."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, AttributeError):
        return time.time()


def format_cpm(value: float | None, partial: bool = False) -> str:
    """Format a CPM value for display."""
    if value is None:
        return "—"
    v = abs(value)
    if v >= 1000:
        text = f"{value:,.0f}"
    elif v >= 100:
        text = f"{value:.0f}"
    elif v >= 10:
        text = f"{value:.1f}"
    else:
        text = f"{value:.2f}"
    if partial:
        text += "*"
    return text


def format_eta(minutes: float | None, partial: bool = False) -> str:
    """Format an ETA in minutes for display."""
    if minutes is None:
        return "—"
    m = max(0.0, float(minutes))
    if not math.isfinite(m) or m > 525_600:
        text = "∞"
    elif m >= 60:
        hours = m / 60.0
        if hours > 5.0:
            text = f"{int(round(hours))}h"
        else:
            text = f"{hours:.1f}h"
    else:
        text = f"{max(1, int(round(m)))}m"
    if partial:
        text += "*"
    return text


class CPMTracker:
    """Pure state machine that tracks trade flow and computes CPM/ETA per ticker.

    Ingests trades, deduplicates by trade_id, and computes contracts-per-minute
    over configurable windows.  Follows the doc spec in KALSHI_CPM_AND_ETA.md.
    """

    _MAX_SEEN = 20_000
    _MAX_EVENTS_PER_KEY = 320

    def __init__(self) -> None:
        self._events: dict[str, list[tuple[float, float]]] = {}
        self._seen: set[str] = set()

    def ingest(self, ticker: str, trades: list[Trade]) -> None:
        """Add trades, deduplicating by trade_id."""
        events = self._events.setdefault(ticker, [])
        for t in trades:
            if t.trade_id in self._seen:
                continue
            self._seen.add(t.trade_id)
            ts = _parse_iso(t.created_time)
            events.append((ts, float(t.count)))
        # Cap per-key to avoid unbounded growth
        if len(events) > self._MAX_EVENTS_PER_KEY:
            self._events[ticker] = events[-self._MAX_EVENTS_PER_KEY :]

    def prune(self, max_age: float = 3700.0) -> None:
        """Remove events older than max_age seconds."""
        cutoff = time.time() - max_age
        for ticker in list(self._events):
            self._events[ticker] = [
                (ts, qty) for ts, qty in self._events[ticker] if ts >= cutoff
            ]
            if not self._events[ticker]:
                del self._events[ticker]
        if len(self._seen) > self._MAX_SEEN:
            self._seen.clear()

    def cpm(self, ticker: str, window_sec: float = 300.0) -> float | None:
        """Contracts per minute over the given window. None if no data."""
        events = self._events.get(ticker)
        if not events:
            return None
        now = time.time()
        cutoff = now - window_sec
        qty_sum = sum(qty for ts, qty in events if ts >= cutoff)
        if qty_sum == 0:
            return 0.0
        # Use actual observed time if less than the full window
        first_ts = min(ts for ts, _ in events)
        observed = now - first_ts
        use_sec = min(window_sec, max(1.0, observed))
        return qty_sum / (use_sec / 60.0)

    def is_partial(self, ticker: str, window_sec: float = 300.0) -> bool:
        """True if we have less than window_sec of observation data."""
        events = self._events.get(ticker)
        if not events:
            return True
        first_ts = min(ts for ts, _ in events)
        return (time.time() - first_ts) < window_sec

    def eta_minutes(self, ticker: str, queue_position: int) -> float | None:
        """Estimated minutes until queue_position contracts ahead are filled."""
        rate = self.cpm(ticker)
        if rate is None or rate <= 0:
            return None
        return queue_position / rate

    @property
    def tickers(self) -> list[str]:
        """Tickers with at least one event."""
        return list(self._events.keys())
