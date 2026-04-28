"""Trade flow tracking and CPM/ETA computation.

CPM (Contracts Per Minute) measures how fast a market is trading at a
specific (outcome, book_side, price) bucket. ETA estimates how long until
a resting order fills given queue position and CPM.

Granularity per docs/KALSHI_CPM_AND_ETA.md: each trade decomposes into TWO
flow events using the complement relation. A trade with taker_side==X at
YES=p produces:
  - X-outcome  ASK at price-X
  - !X-outcome BID at price-!X (complement, derived from YES+NO=1)
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime

from talos.models.market import Trade
from talos.units import ONE_CONTRACT_FP100, ONE_DOLLAR_BPS

DEFAULT_FLOW_WINDOW_SEC = 3600.0


def _parse_iso(ts: str) -> float:
    """Parse ISO 8601 timestamp to Unix seconds."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, AttributeError):
        return time.time()


@dataclass(frozen=True)
class FlowKey:
    """One bucket of trade flow.

    Frozen + hashable so it's a valid dict key. Matches the doc spec's
    composite key: ``(ticker, outcome, book_side, price)``.
    """

    ticker: str
    outcome: str  # "yes" | "no"
    book_side: str  # "BID" | "ASK"
    price_bps: int  # 0..10_000


@dataclass(frozen=True)
class FlowMetrics:
    """Trade frequency and burstiness over a rolling window."""

    volume_contracts: float
    trade_count: int
    largest_trade_contracts: float
    burst_ratio: float
    window_sec: float

    @property
    def trades_per_hour(self) -> float:
        if self.window_sec <= 0:
            return 0.0
        return self.trade_count / (self.window_sec / 3600.0)


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


def format_eta(
    minutes: float | None,
    partial: bool = False,
    round_hours_after: float | None = None,
) -> str:
    """Format an ETA in minutes for display.

    round_hours_after: if set, hours larger than this are rounded to whole
    hours (e.g. ``round_hours_after=5.0`` → '8h' instead of '8.0h' for
    long-tail ETAs where decimal precision is meaningless).
    """
    if minutes is None:
        return "—"
    m = max(0.0, float(minutes))
    if not math.isfinite(m) or m > 525_600:
        text = "∞"
    elif m >= 60:
        hours = m / 60.0
        if round_hours_after is not None and hours > round_hours_after:
            text = f"{int(round(hours))}h"
        else:
            text = f"{hours:.1f}h"
    else:
        text = f"{max(1, int(round(m)))}m"
    if partial:
        text += "*"
    return text


def format_frequency(metrics: FlowMetrics | None) -> str:
    """Format trade frequency as trades/hour."""
    if metrics is None or metrics.trade_count == 0:
        return "—"
    return f"{metrics.trades_per_hour:.0f}/h"


def format_flow(metrics: FlowMetrics | None) -> str:
    """Format burstiness as largest-trade share of window volume."""
    if metrics is None or metrics.volume_contracts <= 0:
        return "—"
    return f"{metrics.burst_ratio * 100:.0f}%"


class CPMTracker:
    """Pure state machine that tracks trade flow per (ticker, outcome,
    book_side, price) and computes CPM/ETA over rolling windows.

    Storage is keyed by FlowKey. ``ingest`` decomposes each trade into TWO
    flow events using the doc spec's taker_side rule.
    """

    _MAX_SEEN = 20_000
    _MAX_EVENTS_PER_KEY = 320

    def __init__(self) -> None:
        # FlowKey → list of (timestamp, count_fp100) tuples.
        self._events: dict[FlowKey, list[tuple[float, int]]] = {}
        self._seen: set[str] = set()

    def ingest(self, ticker: str, trades: list[Trade]) -> None:
        """Add trades, deduplicating by trade_id, decomposing each into TWO
        flow events (yes-side and no-side).

        Decomposition rule (from doc spec):
          taker_side == outcome → ASK hit at outcome's price
          taker_side != outcome → BID hit at outcome's price

        Trades without a recoverable price (1 <= yes_price_bps <= 9_999) are
        skipped — bucketing them at 0 or 10_000 would pollute flow_count.
        """
        for t in trades:
            if t.trade_id in self._seen:
                continue
            yes_price_bps = t.yes_price_bps if t.yes_price_bps is not None else t.price_bps
            no_price_bps = (
                t.no_price_bps if t.no_price_bps is not None else (ONE_DOLLAR_BPS - yes_price_bps)
            )
            # Skip trades without a usable price — bucketing at the extremes
            # would be misleading.
            if not (1 <= yes_price_bps <= 9_999):
                continue
            self._seen.add(t.trade_id)
            ts = _parse_iso(t.created_time)

            # YES-outcome bucket.
            yes_book_side = "ASK" if t.side == "yes" else "BID"
            yes_key = FlowKey(
                ticker=ticker,
                outcome="yes",
                book_side=yes_book_side,
                price_bps=yes_price_bps,
            )
            self._append(yes_key, ts, t.count_fp100)

            # NO-outcome bucket.
            no_book_side = "ASK" if t.side == "no" else "BID"
            no_key = FlowKey(
                ticker=ticker,
                outcome="no",
                book_side=no_book_side,
                price_bps=no_price_bps,
            )
            self._append(no_key, ts, t.count_fp100)

        if len(self._seen) > self._MAX_SEEN:
            self._seen.clear()

    def _append(self, key: FlowKey, ts: float, count_fp100: int) -> None:
        events = self._events.setdefault(key, [])
        events.append((ts, count_fp100))
        if len(events) > self._MAX_EVENTS_PER_KEY:
            self._events[key] = events[-self._MAX_EVENTS_PER_KEY :]

    def prune(self, max_age: float = 3700.0) -> None:
        """Remove events older than max_age seconds."""
        cutoff = time.time() - max_age
        for key in list(self._events):
            self._events[key] = [(ts, c) for ts, c in self._events[key] if ts >= cutoff]
            if not self._events[key]:
                del self._events[key]
        if len(self._seen) > self._MAX_SEEN:
            self._seen.clear()

    def flow_count(self, key: FlowKey, max_age: float | None = None) -> int:
        """Total count_fp100 in a flow bucket. ``max_age`` of None = all events."""
        events = self._events.get(key)
        if not events:
            return 0
        if max_age is None:
            return sum(c for _, c in events)
        cutoff = time.time() - max_age
        return sum(c for ts, c in events if ts >= cutoff)

    def cpm(
        self,
        ticker: str,
        outcome: str | None = None,
        book_side: str | None = None,
        price_bps: int | None = None,
        window_sec: float = DEFAULT_FLOW_WINDOW_SEC,
    ) -> float | None:
        """Contracts per minute over the given window, optionally filtered.

        If outcome/book_side/price_bps are all provided → per-bucket CPM.
        If any are None → aggregate across the unspecified dimensions for
        that ticker.

        IMPORTANT: when ALL of outcome/book_side/price_bps are None
        (bare-ticker aggregate), iteration is restricted to one outcome side
        because each trade decomposes into a yes-bucket and a no-bucket event
        with the same count_fp100. Iterating one side counts each trade
        exactly once. Without this guard, ticker-aggregate would double.

        Returns None when no events match.
        """
        # Bare-ticker aggregate: each trade decomposes into yes+no events with
        # the same count. Iterate ONE outcome to count each trade exactly once.
        bare_aggregate = outcome is None and book_side is None and price_bps is None
        matching: list[tuple[float, int]] = []
        for key, events in self._events.items():
            if key.ticker != ticker:
                continue
            if bare_aggregate and key.outcome != "yes":
                continue
            if outcome is not None and key.outcome != outcome:
                continue
            if book_side is not None and key.book_side != book_side:
                continue
            if price_bps is not None and key.price_bps != price_bps:
                continue
            matching.extend(events)
        if not matching:
            return None
        now = time.time()
        cutoff = now - window_sec
        qty_sum_fp100 = sum(c for ts, c in matching if ts >= cutoff)
        if qty_sum_fp100 == 0:
            return 0.0
        first_ts = min(ts for ts, _ in matching)
        observed = now - first_ts
        use_sec = min(window_sec, max(1.0, observed))
        qty_sum_contracts = qty_sum_fp100 / ONE_CONTRACT_FP100
        return qty_sum_contracts / (use_sec / 60.0)

    def is_partial(
        self,
        ticker: str,
        outcome: str | None = None,
        book_side: str | None = None,
        price_bps: int | None = None,
        window_sec: float = DEFAULT_FLOW_WINDOW_SEC,
    ) -> bool:
        """True if observed time is shorter than the window (extrapolation flag)."""
        bare_aggregate = outcome is None and book_side is None and price_bps is None
        matching_ts: list[float] = []
        for key, events in self._events.items():
            if key.ticker != ticker:
                continue
            if bare_aggregate and key.outcome != "yes":
                continue
            if outcome is not None and key.outcome != outcome:
                continue
            if book_side is not None and key.book_side != book_side:
                continue
            if price_bps is not None and key.price_bps != price_bps:
                continue
            matching_ts.extend(ts for ts, _ in events)
        if not matching_ts:
            return True
        return (time.time() - min(matching_ts)) < window_sec

    def eta_minutes(
        self,
        ticker: str,
        queue_position: int,
        outcome: str | None = None,
        book_side: str | None = None,
        price_bps: int | None = None,
        window_sec: float = DEFAULT_FLOW_WINDOW_SEC,
    ) -> float | None:
        """Estimated minutes until ``queue_position`` contracts ahead of you fill,
        optionally narrowed to a specific (outcome, book_side, price_bps) bucket.

        BACKWARD-COMPATIBLE: callers passing only (ticker, queue_position) get
        the ticker-aggregate ETA (matches old behavior). Callers passing the
        full bucket get per-side ETA.

        Fallback chain (when narrower buckets have no data):
          1. Exact (outcome, book_side, price_bps)
          2. Drop price_bps  → (outcome, book_side, None)
          3. Drop book_side  → (outcome, None, None)
          → return None if still no data

        We do NOT fall back further by dropping outcome. The bare-ticker
        aggregate iterates only over outcome=="yes" (per the C1 fix to stop
        double-counting), so falling back from an outcome="no" query to it
        would return a yes-side fill rate — the wrong direction. Returning
        None when the per-outcome aggregate is empty is the honest answer.
        """
        rate = self.cpm(ticker, outcome, book_side, price_bps, window_sec)
        if rate is None and price_bps is not None:
            rate = self.cpm(ticker, outcome, book_side, None, window_sec)
        if rate is None and book_side is not None:
            rate = self.cpm(ticker, outcome, None, None, window_sec)
        if rate is None or rate <= 0:
            return None
        return queue_position / rate

    def flow_metrics(
        self,
        ticker: str,
        outcome: str | None = None,
        book_side: str | None = None,
        price_bps: int | None = None,
        window_sec: float = DEFAULT_FLOW_WINDOW_SEC,
    ) -> FlowMetrics | None:
        """Return frequency and burstiness for the same bucket shape as CPM."""
        bare_aggregate = outcome is None and book_side is None and price_bps is None
        matching: list[tuple[float, int]] = []
        for key, events in self._events.items():
            if key.ticker != ticker:
                continue
            if bare_aggregate and key.outcome != "yes":
                continue
            if outcome is not None and key.outcome != outcome:
                continue
            if book_side is not None and key.book_side != book_side:
                continue
            if price_bps is not None and key.price_bps != price_bps:
                continue
            matching.extend(events)
        if not matching:
            return None

        cutoff = time.time() - window_sec
        recent = [(ts, c) for ts, c in matching if ts >= cutoff]
        if not recent:
            return FlowMetrics(
                volume_contracts=0.0,
                trade_count=0,
                largest_trade_contracts=0.0,
                burst_ratio=0.0,
                window_sec=window_sec,
            )

        counts = [count_fp100 / ONE_CONTRACT_FP100 for _, count_fp100 in recent]
        volume = sum(counts)
        largest = max(counts)
        return FlowMetrics(
            volume_contracts=volume,
            trade_count=len(recent),
            largest_trade_contracts=largest,
            burst_ratio=largest / volume if volume > 0 else 0.0,
            window_sec=window_sec,
        )

    @property
    def tickers(self) -> list[str]:
        """Distinct tickers seen at least once."""
        return list({k.ticker for k in self._events})
