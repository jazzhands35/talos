# CPM/ETA Granularity Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `CPMTracker` from per-ticker granularity up to the documented per-(ticker, outcome, book_side, price) granularity so per-side fill ETA is correctly computable for arb pairs.

**Architecture:** Replace the single per-ticker event list in `CPMTracker` with a flow-key-keyed dict (matching the doc spec at [docs/KALSHI_CPM_AND_ETA.md](../../KALSHI_CPM_AND_ETA.md)). Each trade decomposes into TWO flow events (one for YES, one for NO) using the `taker_side` rule. Add a per-side ETA helper for downstream arb consumers. Preserve aggregate semantics so existing UI math remains unchanged.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, ruff, pyright. No new dependencies.

**Spec:** [docs/superpowers/specs/2026-04-26-drip-staggered-arb-redesign.md](../specs/2026-04-26-drip-staggered-arb-redesign.md) — POC scope section, "Prerequisite: CPM/ETA fix"

**Doc spec:** [docs/KALSHI_CPM_AND_ETA.md](../../KALSHI_CPM_AND_ETA.md) — already documents the target behavior verbatim

---

## File Map

| File | Change | Purpose |
|------|--------|---------|
| `src/talos/cpm.py` | Refactor `CPMTracker` | Per-(ticker, outcome, book_side, price) keying; trade decomposition; aggregate fallback; per-side ETA |
| `src/talos/engine.py:2915` | Update one call site | Switch `eta_minutes(ticker, queue_pos)` → `eta_minutes(ticker, side, book_side, price_bps, queue_pos)` |
| `tests/test_cpm.py` | New test file | Trade decomposition, per-key CPM, aggregate fallback, ETA per side, equivalence to old aggregate |

No new files in `src/`. No UI changes — the existing engine call site at line 2915 is the only consumer; the new granularity is a strict superset of the old behavior.

---

## Task 1: Add `FlowKey` type and trade decomposition

**Files:**
- Modify: `src/talos/cpm.py`
- Test: `tests/test_cpm.py` (create)

The flow key identifies a unique (ticker, outcome, book_side, price_bps) bucket. Trades are decomposed into two flow events using the doc's rule: a trade with `taker_side == "yes"` corresponds to a YES-side ASK hit AND a NO-side BID hit (because every Kalshi trade has both YES and NO prices via the complement relation).

- [ ] **Step 1: Write the failing test for trade decomposition**

Create `tests/test_cpm.py`:

```python
"""Tests for the CPM/ETA granularity fix."""

from __future__ import annotations

from talos.cpm import CPMTracker, FlowKey
from talos.models.market import Trade


def _make_trade(
    *,
    ticker: str = "KX-TEST",
    trade_id: str = "t1",
    taker_side: str,
    yes_price_dollars: str,
    count_fp: str = "1",
    created_time: str = "2026-04-26T00:00:00Z",
) -> Trade:
    """Build a Trade pydantic model from wire-shape kwargs."""
    no_price = f"{1 - float(yes_price_dollars):.4f}"
    return Trade.model_validate({
        "ticker": ticker,
        "trade_id": trade_id,
        "taker_side": taker_side,
        "yes_price_dollars": yes_price_dollars,
        "no_price_dollars": no_price,
        "count_fp": count_fp,
        "created_time": created_time,
    })


def test_trade_decomposes_into_two_flow_events():
    """A YES-taker trade at YES=0.55 produces:
       - YES outcome ASK at 5500 bps
       - NO outcome BID at 4500 bps
    Each gets count_fp100 added to its flow.
    """
    tracker = CPMTracker()
    trade = _make_trade(taker_side="yes", yes_price_dollars="0.55", count_fp="3")
    tracker.ingest("KX-TEST", [trade])

    yes_ask_5500 = FlowKey(ticker="KX-TEST", outcome="yes", book_side="ASK", price_bps=5500)
    no_bid_4500 = FlowKey(ticker="KX-TEST", outcome="no", book_side="BID", price_bps=4500)

    assert tracker.flow_count(yes_ask_5500) == 300  # 3 contracts = 300 fp100
    assert tracker.flow_count(no_bid_4500) == 300

    # No flow on the opposite buckets.
    yes_bid_5500 = FlowKey(ticker="KX-TEST", outcome="yes", book_side="BID", price_bps=5500)
    assert tracker.flow_count(yes_bid_5500) == 0


def test_no_taker_decomposes_inversely():
    """A NO-taker trade at YES=0.55 produces:
       - NO outcome ASK at 4500 bps
       - YES outcome BID at 5500 bps
    """
    tracker = CPMTracker()
    trade = _make_trade(taker_side="no", yes_price_dollars="0.55", count_fp="2")
    tracker.ingest("KX-TEST", [trade])

    no_ask_4500 = FlowKey(ticker="KX-TEST", outcome="no", book_side="ASK", price_bps=4500)
    yes_bid_5500 = FlowKey(ticker="KX-TEST", outcome="yes", book_side="BID", price_bps=5500)
    assert tracker.flow_count(no_ask_4500) == 200
    assert tracker.flow_count(yes_bid_5500) == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_cpm.py::test_trade_decomposes_into_two_flow_events -v`
Expected: FAIL with `ImportError` or `AttributeError` (FlowKey, flow_count not defined yet).

- [ ] **Step 3: Add `FlowKey` and refactored storage to `src/talos/cpm.py`**

Replace the existing `CPMTracker` body. Full new content of `src/talos/cpm.py`:

```python
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
from talos.units import ONE_CONTRACT_FP100


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
        text = f"{hours:.1f}h"
    else:
        text = f"{max(1, int(round(m)))}m"
    if partial:
        text += "*"
    return text


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
        """
        for t in trades:
            if t.trade_id in self._seen:
                continue
            self._seen.add(t.trade_id)
            ts = _parse_iso(t.created_time)
            yes_price_bps = t.yes_price_bps if t.yes_price_bps is not None else t.price_bps
            no_price_bps = t.no_price_bps if t.no_price_bps is not None else (10_000 - yes_price_bps)

            # YES-outcome bucket.
            yes_book_side = "ASK" if t.side == "yes" else "BID"
            yes_key = FlowKey(ticker=ticker, outcome="yes", book_side=yes_book_side, price_bps=yes_price_bps)
            self._append(yes_key, ts, t.count_fp100)

            # NO-outcome bucket.
            no_book_side = "ASK" if t.side == "no" else "BID"
            no_key = FlowKey(ticker=ticker, outcome="no", book_side=no_book_side, price_bps=no_price_bps)
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
        window_sec: float = 300.0,
    ) -> float | None:
        """Contracts per minute over the given window, optionally filtered.

        If outcome/book_side/price_bps are all provided → per-bucket CPM.
        If any are None → aggregate across the unspecified dimensions for
        that ticker (doc spec's "fallback" behavior).

        Returns None when no events match.
        """
        matching: list[tuple[float, int]] = []
        for key, events in self._events.items():
            if key.ticker != ticker:
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
        window_sec: float = 300.0,
    ) -> bool:
        """True if observed time is shorter than the window (extrapolation flag)."""
        matching_ts: list[float] = []
        for key, events in self._events.items():
            if key.ticker != ticker:
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
        outcome: str,
        book_side: str,
        price_bps: int,
        queue_position: int,
        window_sec: float = 300.0,
    ) -> float | None:
        """Estimated minutes until ``queue_position`` contracts ahead of you fill,
        at the (outcome, book_side, price_bps) bucket on this ticker.

        Falls back to ticker-aggregate CPM when the per-bucket rate has no data.
        """
        rate = self.cpm(ticker, outcome, book_side, price_bps, window_sec)
        if rate is None:
            # Aggregate fallback: broaden by dropping price_bps first, then book_side.
            rate = self.cpm(ticker, outcome, book_side, None, window_sec)
            if rate is None:
                rate = self.cpm(ticker, outcome, None, None, window_sec)
        if rate is None or rate <= 0:
            return None
        return queue_position / rate

    @property
    def tickers(self) -> list[str]:
        """Distinct tickers seen at least once."""
        return list({k.ticker for k in self._events})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_cpm.py -v`
Expected: 2/2 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/talos/cpm.py tests/test_cpm.py
git commit -m "$(cat <<'EOF'
refactor(cpm): per-(outcome, book_side, price) flow keying

Replace per-ticker single event list with FlowKey-indexed dict per
docs/KALSHI_CPM_AND_ETA.md. Trades decompose into two flow events using
the taker_side rule (taker_side==outcome → ASK; else BID). Establishes
the granularity needed for per-side ETA in arb pairs.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Per-bucket CPM with aggregate fallback

**Files:**
- Modify: `tests/test_cpm.py`
- (No source changes — `cpm()` already implemented in Task 1)

This task locks in the aggregate-fallback semantics through tests.

- [ ] **Step 1: Write failing tests for per-bucket CPM and aggregate fallback**

Append to `tests/test_cpm.py`:

```python
import time as _time
from unittest.mock import patch


def test_per_bucket_cpm_isolates_one_side():
    """Two trades on opposite sides at different prices: per-bucket CPM
    sees only the matching bucket; bare-ticker aggregate (yes-only after
    the C1 fix in 54e20b2) counts each trade once."""
    tracker = CPMTracker()
    now = _time.time()
    with patch("talos.cpm.time.time", return_value=now):
        with patch("talos.cpm._parse_iso", return_value=now - 60):
            tracker.ingest("KX-TEST", [
                _make_trade(trade_id="t1", taker_side="yes", yes_price_dollars="0.55", count_fp="6"),
            ])
        with patch("talos.cpm._parse_iso", return_value=now - 60):
            tracker.ingest("KX-TEST", [
                _make_trade(trade_id="t2", taker_side="no", yes_price_dollars="0.45", count_fp="6"),
            ])

        # Per-bucket: yes ASK at 5500 should see only the t1 (6 contracts).
        per_bucket = tracker.cpm("KX-TEST", "yes", "ASK", 5500, window_sec=300.0)
        assert per_bucket is not None
        # 6 contracts over 60 observed seconds → 6 / (60/60) = 6 cpm
        assert abs(per_bucket - 6.0) < 0.01

        # Bare-ticker aggregate (yes-only after C1 fix): sees yes-ASK-5500 (t1, 6 contracts)
        # + yes-BID-5500 (t2, 6 contracts) = 12 contracts. Each trade is counted once.
        aggregate = tracker.cpm("KX-TEST", window_sec=300.0)
        assert aggregate is not None
        assert abs(aggregate - 12.0) < 0.01


def test_aggregate_fallback_when_per_bucket_empty():
    """eta_minutes broadens when the exact bucket has no data."""
    tracker = CPMTracker()
    now = _time.time()
    with patch("talos.cpm.time.time", return_value=now):
        with patch("talos.cpm._parse_iso", return_value=now - 60):
            # Trade at YES=0.55 → produces yes-ASK-5500 and no-BID-4500.
            tracker.ingest("KX-TEST", [
                _make_trade(trade_id="t1", taker_side="yes", yes_price_dollars="0.55", count_fp="6"),
            ])

        # Query ETA at a DIFFERENT price level (5400) — no exact bucket.
        # Falls back to (yes, ASK, *) aggregate which is empty too at 5400 only,
        # so falls back to (yes, *, *) which has the t1 yes-ASK-5500 flow → cpm=6.
        eta = tracker.eta_minutes("KX-TEST", "yes", "ASK", 5400, queue_position=12, window_sec=300.0)
        assert eta is not None
        assert abs(eta - 2.0) < 0.01  # 12 contracts / 6 cpm = 2 min


def test_eta_returns_none_when_no_flow():
    """No trades at all on this ticker → ETA is None."""
    tracker = CPMTracker()
    eta = tracker.eta_minutes("KX-TEST", "yes", "ASK", 5500, queue_position=10)
    assert eta is None


def test_dedup_by_trade_id():
    """Re-ingesting the same trade_id is a no-op."""
    tracker = CPMTracker()
    trade = _make_trade(trade_id="t1", taker_side="yes", yes_price_dollars="0.55", count_fp="3")
    tracker.ingest("KX-TEST", [trade])
    tracker.ingest("KX-TEST", [trade])  # Should NOT double-count.

    yes_ask_5500 = FlowKey(ticker="KX-TEST", outcome="yes", book_side="ASK", price_bps=5500)
    assert tracker.flow_count(yes_ask_5500) == 300  # 3 contracts × 100, NOT 600
```

- [ ] **Step 2: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_cpm.py -v`
Expected: 6/6 PASS (2 from Task 1 + 4 new).

- [ ] **Step 3: Commit**

```bash
git add tests/test_cpm.py
git commit -m "$(cat <<'EOF'
test(cpm): per-bucket CPM, aggregate fallback, dedup

Lock in the aggregate-fallback semantics for eta_minutes and verify
trade_id dedup survives the granularity refactor.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Update the engine call site

**Files:**
- Modify: `src/talos/engine.py:2914-2922`

The existing call uses `eta_minutes(ticker, queue_pos)`. The new signature requires `(ticker, outcome, book_side, price_bps, queue_pos)`. The engine knows `behind_side` (Side.A/B) and the resting order's price; we infer the outcome and book_side from the ledger's NO+NO arb convention (resting bids on the NO outcome, hitting the BID side).

- [ ] **Step 1: Read the current call-site code to confirm context**

Run: `.venv/Scripts/python -c "with open('src/talos/engine.py') as f: print(''.join(f.readlines()[2895:2925]))"`
Expected output: a block beginning with `resting_price = ledger.resting_price(behind_side)` and ending past the `eta = self._cpm.eta_minutes(...)` call.

- [ ] **Step 2: Edit the call site**

In `src/talos/engine.py`, replace:

```python
            ticker = pair.ticker_a if behind_side == Side.A else pair.ticker_b
            eta = self._cpm.eta_minutes(ticker, queue_pos)
            if eta is None:
                # CPM = 0 means dead market — treat as infinite ETA
                cpm = self._cpm.cpm(ticker)
                if cpm is not None and cpm == 0:
                    eta = float("inf")
                else:
                    continue
```

with:

```python
            ticker = pair.ticker_a if behind_side == Side.A else pair.ticker_b
            # Talos arb convention: NO+NO bids resting on the NO outcome's BID side.
            # resting_price is in bps already (NO-side price).
            eta = self._cpm.eta_minutes(
                ticker,
                outcome="no",
                book_side="BID",
                price_bps=resting_price,
                queue_position=queue_pos,
            )
            if eta is None:
                # CPM = 0 means dead market — treat as infinite ETA
                cpm = self._cpm.cpm(ticker, outcome="no", book_side="BID", price_bps=resting_price)
                if cpm is not None and cpm == 0:
                    eta = float("inf")
                else:
                    continue
```

- [ ] **Step 3: Run the engine's existing tests to confirm nothing regressed**

Run: `.venv/Scripts/python -m pytest tests/ -k "engine or cpm" -v`
Expected: PASS (if any pre-existing engine tests reference the old signature, they'll fail and need updating — handle inline).

- [ ] **Step 4: Run lint + typecheck in parallel**

Run in parallel:
```bash
.venv/Scripts/python -m ruff check src/talos/cpm.py src/talos/engine.py tests/test_cpm.py
.venv/Scripts/python -m pyright src/talos/cpm.py
```
Expected: clean on cpm.py and tests; pre-existing engine.py warnings unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/talos/engine.py
git commit -m "$(cat <<'EOF'
refactor(engine): pass outcome/book_side/price to CPMTracker.eta_minutes

Sole consumer of the old aggregate eta_minutes signature. Talos's NO+NO
arb convention always rests on the NO outcome's BID side, so the call
site supplies the bucket explicitly.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Run the full test suite + commit doc reference update

**Files:**
- Modify: `docs/KALSHI_CPM_AND_ETA.md` (footer note only)

- [ ] **Step 1: Run full suite**

Run: `.venv/Scripts/python -m pytest -x`
Expected: green (or pre-existing unrelated failures explicitly listed in `brain/decisions.md`).

- [ ] **Step 2: Add an implementation-status note at the top of the doc**

In `docs/KALSHI_CPM_AND_ETA.md`, add immediately after line 1 (`# Kalshi CPM and Fill ETA — How It Works`):

```markdown
> **Implementation status (2026-04-26):** `src/talos/cpm.py` matches this
> doc spec at the per-(ticker, outcome, book_side, price) granularity.
> Earlier per-ticker-only behavior was fixed in the CPM granularity PR.
```

- [ ] **Step 3: Commit**

```bash
git add docs/KALSHI_CPM_AND_ETA.md
git commit -m "$(cat <<'EOF'
docs(cpm): note implementation now matches doc spec granularity

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-review checklist (run before opening the PR)

- [ ] Per-(ticker, outcome, book_side, price) keying — Task 1 ✓
- [ ] Trade decomposition with taker_side rule — Task 1 ✓
- [ ] Aggregate fallback in `cpm()` and `eta_minutes()` — Task 1 ✓
- [ ] Per-side ETA helper — Task 1 ✓ (`eta_minutes` with new signature)
- [ ] Existing UI consumers updated — Task 3 ✓ (engine.py:2915 is the only one)
- [ ] No regressions in existing tests — Task 4 ✓
- [ ] Doc reference matches implementation — Task 4 ✓

## Out of scope (for the DRIP POC plan to consume)

- Per-side ETA queries from arb-pair contexts (DRIP controller) — `eta_minutes()` is now ready for that consumer; no change here.
- Top-of-book quantity-decrease as a second CPM source — doc-described, intentionally not implemented in this fix. Add only if observed CPM signals are too sparse during DRIP POC runs.
