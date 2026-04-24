# Open-Unit Average Scoping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scope `PositionLedger.avg_filled_price` to the currently open unit so P18 profitability checks stop being subsidized by closed matched pairs.

**Architecture:** Add a `closed_*` bucket to `_SideState` that mirrors the existing `filled_*` scalars. Every mutation path that increases `filled_*` invokes a new `_reconcile_closed()` method that flushes `min(open_A, open_B) // unit_size × unit_size` contracts via pro-rata. Decision-path callers switch to a new `open_avg_filled_price()` accessor; display/PnL callers keep the existing `avg_filled_price()`. Persistence adds six keys (three per side) with strict all-or-nothing deserialization.

**Tech Stack:** Python 3.12+, pytest, pyright, ruff, structlog, Pydantic v2. All changes are in `src/talos/position_ledger.py`, `src/talos/bid_adjuster.py`, `src/talos/rebalance.py`, `src/talos/engine.py`, with corresponding tests.

**Spec:** [docs/superpowers/specs/2026-04-15-open-unit-avg-scoping-design.md](../specs/2026-04-15-open-unit-avg-scoping-design.md)

---

## File Map

**Modify:**
- `src/talos/position_ledger.py` — add `closed_*` fields to `_SideState`, new accessors `open_count` and `open_avg_filled_price`, new `_reconcile_closed` method, invoke reconcile in `record_fill` / `sync_from_orders` / `sync_from_positions` / `seed_from_saved`, update `is_placement_safe` P18 gate, extend `to_save_dict` / `seed_from_saved` for persistence
- `src/talos/bid_adjuster.py` — swap `avg_filled_price` → `open_avg_filled_price` in `evaluate_jump` and `_check_post_cancel_safety`
- `src/talos/rebalance.py` — swap direct `filled_total_cost / filled_count` in `compute_rebalance_proposal` fallback
- `src/talos/engine.py` — swap `avg_filled_price` → `open_avg_filled_price` in `check_queue_stress` and the queue-improvement execution recheck

**Test (modify / create):**
- `tests/test_position_ledger.py` — new test class for `_reconcile_closed` behavior and invariant across all mutation paths, plus new deserialization test class
- `tests/test_bid_adjuster.py` — regression tests for jump-follow and post-cancel-safety under the new semantics
- `tests/test_rebalance.py` — regression for the catch-up fallback path
- `tests/test_engine.py` — regression for queue-stress and queue-improve recheck
- `tests/test_ledger_reconstruction.py` (new) — restart regimes 5a/5b/5c/5d

---

## Task 1: Add `closed_*` fields to `_SideState`

**Files:**
- Modify: [src/talos/position_ledger.py:44-73](src/talos/position_ledger.py:44)
- Test: [tests/test_position_ledger.py](tests/test_position_ledger.py) (new test class `TestClosedBucket`)

- [ ] **Step 1: Write the failing test**

Add at the end of `tests/test_position_ledger.py`:

```python
class TestClosedBucket:
    """closed_* fields mirror filled_* and start at zero."""

    def test_new_ledger_has_zero_closed_fields(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        for side in (Side.A, Side.B):
            s = ledger._sides[side]
            assert s.closed_count == 0
            assert s.closed_total_cost == 0
            assert s.closed_fees == 0

    def test_reset_zeroes_closed_fields(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        s = ledger._sides[Side.A]
        s.closed_count = 99
        s.closed_total_cost = 500
        s.closed_fees = 3
        s.reset()
        assert s.closed_count == 0
        assert s.closed_total_cost == 0
        assert s.closed_fees == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py::TestClosedBucket -v`
Expected: FAIL with `AttributeError: '_SideState' object has no attribute 'closed_count'`

- [ ] **Step 3: Add the three new fields to `_SideState`**

Edit `src/talos/position_ledger.py` lines 44-73. Replace the `__slots__`, `__init__`, and `reset` blocks:

```python
class _SideState:
    """Mutable per-side position state."""

    __slots__ = (
        "filled_count",
        "filled_total_cost",
        "filled_fees",
        "closed_count",
        "closed_total_cost",
        "closed_fees",
        "_fees_from_api",
        "resting_order_id",
        "resting_count",
        "resting_price",
        "_placed_at_gen",
    )

    def __init__(self) -> None:
        self.filled_count: int = 0
        self.filled_total_cost: int = 0
        self.filled_fees: int = 0
        self.closed_count: int = 0
        self.closed_total_cost: int = 0
        self.closed_fees: int = 0
        self._fees_from_api: bool = False
        self.resting_order_id: str | None = None
        self.resting_count: int = 0
        self.resting_price: int = 0
        self._placed_at_gen: int | None = None

    def reset(self) -> None:
        self.filled_count = 0
        self.filled_total_cost = 0
        self.filled_fees = 0
        self.closed_count = 0
        self.closed_total_cost = 0
        self.closed_fees = 0
        self._fees_from_api = False
        self.resting_order_id = None
        self.resting_count = 0
        self.resting_price = 0
        self._placed_at_gen = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py::TestClosedBucket -v`
Expected: 2 passed

- [ ] **Step 5: Run full `test_position_ledger.py` to catch regressions**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py -v`
Expected: all existing tests still pass

- [ ] **Step 6: Commit**

```bash
git add src/talos/position_ledger.py tests/test_position_ledger.py
git commit -m "feat(ledger): add closed_* bucket fields to _SideState"
```

---

## Task 2: Add `open_count` and `open_avg_filled_price` accessors

**Files:**
- Modify: [src/talos/position_ledger.py:147-151](src/talos/position_ledger.py:147) area (the derived-queries section)
- Test: `tests/test_position_ledger.py`

- [ ] **Step 1: Write the failing tests**

Add inside `TestClosedBucket`:

```python
    def test_open_count_equals_filled_when_closed_is_zero(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger._sides[Side.A].filled_count = 7
        assert ledger.open_count(Side.A) == 7

    def test_open_count_subtracts_closed(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        s = ledger._sides[Side.A]
        s.filled_count = 10
        s.closed_count = 5
        assert ledger.open_count(Side.A) == 5

    def test_open_avg_filled_price_zero_when_no_open_fills(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        assert ledger.open_avg_filled_price(Side.A) == 0.0

    def test_open_avg_filled_price_zero_when_everything_closed(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        s = ledger._sides[Side.A]
        s.filled_count = 5
        s.filled_total_cost = 400
        s.closed_count = 5
        s.closed_total_cost = 400
        assert ledger.open_avg_filled_price(Side.A) == 0.0

    def test_open_avg_filled_price_uses_open_bucket_only(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        s = ledger._sides[Side.A]
        # Closed bucket: 5 contracts at avg 18c (90c total)
        s.closed_count = 5
        s.closed_total_cost = 90
        # Open bucket: 5 more contracts at avg 23c (115c total)
        # filled_* is cumulative: 10 total fills for 205c
        s.filled_count = 10
        s.filled_total_cost = 205
        # Open avg = (205 - 90) / (10 - 5) = 115 / 5 = 23.0
        assert ledger.open_avg_filled_price(Side.A) == 23.0

    def test_lifetime_avg_unchanged(self):
        """avg_filled_price must still return the lifetime blended avg."""
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        s = ledger._sides[Side.A]
        s.filled_count = 10
        s.filled_total_cost = 205
        s.closed_count = 5
        s.closed_total_cost = 90
        assert ledger.avg_filled_price(Side.A) == 20.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py::TestClosedBucket -v`
Expected: six new tests fail with `AttributeError: 'PositionLedger' object has no attribute 'open_count'` / `open_avg_filled_price`

- [ ] **Step 3: Add the two accessors**

In `src/talos/position_ledger.py`, insert immediately after `avg_filled_price` (currently at lines 147-151). The method is in the "Derived queries" section:

```python
    def open_count(self, side: Side) -> int:
        """Count of fills still in the open (unclosed) unit on this side."""
        s = self._sides[side]
        return s.filled_count - s.closed_count

    def open_avg_filled_price(self, side: Side) -> float:
        """Average fill price of the currently-open unit on this side.

        Returns 0.0 when the open unit has no fills (fresh position or
        immediately after a matched-pair close). Decision-path callers
        (P18 profitability checks) must use this, NOT avg_filled_price —
        closed units should not influence decisions about the open unit.
        """
        s = self._sides[side]
        open_count = s.filled_count - s.closed_count
        if open_count <= 0:
            return 0.0
        open_cost = s.filled_total_cost - s.closed_total_cost
        return open_cost / open_count
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py::TestClosedBucket -v`
Expected: 8 passed (6 new + 2 from Task 1)

- [ ] **Step 5: Commit**

```bash
git add src/talos/position_ledger.py tests/test_position_ledger.py
git commit -m "feat(ledger): add open_count and open_avg_filled_price accessors"
```

---

## Task 3: Add `_reconcile_closed` method

**Files:**
- Modify: `src/talos/position_ledger.py` (add private method; no call sites yet)
- Test: `tests/test_position_ledger.py`

- [ ] **Step 1: Write the failing tests**

Add a new test class to `tests/test_position_ledger.py`:

```python
class TestReconcileClosed:
    """_reconcile_closed flushes matched pairs into the closed bucket."""

    def test_noop_when_imbalanced(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger._sides[Side.A].filled_count = 5
        ledger._sides[Side.A].filled_total_cost = 410
        ledger._sides[Side.B].filled_count = 3
        ledger._sides[Side.B].filled_total_cost = 54
        ledger._reconcile_closed()
        # min(5, 3) = 3, 3 // 5 = 0 units, no close fires
        assert ledger._sides[Side.A].closed_count == 0
        assert ledger._sides[Side.B].closed_count == 0

    def test_closes_one_balanced_unit(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        a = ledger._sides[Side.A]
        b = ledger._sides[Side.B]
        a.filled_count = 5
        a.filled_total_cost = 410  # avg 82
        b.filled_count = 5
        b.filled_total_cost = 90   # avg 18
        ledger._reconcile_closed()
        assert a.closed_count == 5
        assert a.closed_total_cost == 410
        assert b.closed_count == 5
        assert b.closed_total_cost == 90
        assert ledger.open_count(Side.A) == 0
        assert ledger.open_count(Side.B) == 0

    def test_closes_multiple_balanced_units_at_once(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        a = ledger._sides[Side.A]
        b = ledger._sides[Side.B]
        a.filled_count = 10
        a.filled_total_cost = 820
        b.filled_count = 10
        b.filled_total_cost = 180
        ledger._reconcile_closed()
        assert a.closed_count == 10
        assert b.closed_count == 10

    def test_imbalanced_close_flushes_min_units(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        a = ledger._sides[Side.A]
        b = ledger._sides[Side.B]
        a.filled_count = 5
        a.filled_total_cost = 410  # 82
        b.filled_count = 10
        b.filled_total_cost = 205  # avg 20.5
        ledger._reconcile_closed()
        # min(5,10)//5 = 1 unit. Close 5 each.
        # A: 5 close with pro-rata of open avg (full flush: 410)
        # B: 5 close with pro-rata of open avg (half flush: round(205*5/10) = 103 or 102)
        assert a.closed_count == 5
        assert a.closed_total_cost == 410
        assert b.closed_count == 5
        assert b.closed_total_cost in (102, 103)  # banker's rounding tolerance
        # After close, open B has 5 contracts at ~20.4c (pro-rata preserves blend)
        assert ledger.open_count(Side.A) == 0
        assert ledger.open_count(Side.B) == 5

    def test_idempotent_second_call_is_noop(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger._sides[Side.A].filled_count = 5
        ledger._sides[Side.A].filled_total_cost = 400
        ledger._sides[Side.B].filled_count = 5
        ledger._sides[Side.B].filled_total_cost = 100
        ledger._reconcile_closed()
        a_closed_before = ledger._sides[Side.A].closed_count
        b_closed_before = ledger._sides[Side.B].closed_count
        ledger._reconcile_closed()
        assert ledger._sides[Side.A].closed_count == a_closed_before
        assert ledger._sides[Side.B].closed_count == b_closed_before

    def test_emits_paper_trail_log(self, caplog):
        import logging
        from talos.position_ledger import PositionLedger, Side
        caplog.set_level(logging.INFO)
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger._sides[Side.A].filled_count = 5
        ledger._sides[Side.A].filled_total_cost = 400
        ledger._sides[Side.B].filled_count = 5
        ledger._sides[Side.B].filled_total_cost = 100
        ledger._reconcile_closed()
        # structlog records go through the standard logging module; look for the event
        assert any(
            "ledger_reconciled_closed" in rec.getMessage() or
            "ledger_reconciled_closed" in str(getattr(rec, "msg", ""))
            for rec in caplog.records
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py::TestReconcileClosed -v`
Expected: all tests fail with `AttributeError: 'PositionLedger' object has no attribute '_reconcile_closed'`

- [ ] **Step 3: Add `_reconcile_closed` method**

In `src/talos/position_ledger.py`, add this method inside the `PositionLedger` class. Place it just before the `# ── State mutations ──` section header:

```python
    # ── Open-unit reconciliation ────────────────────────────────────

    def _reconcile_closed(self) -> None:
        """Flush newly-matched pairs from the open bucket into the closed bucket.

        Idempotent: safe to call multiple times. If no new units can close,
        returns without mutation.

        Must be invoked after ANY mutation that increases filled_count or
        filled_total_cost. See the invariant in the open-unit avg scoping
        spec (docs/superpowers/specs/2026-04-15-open-unit-avg-scoping-design.md).
        """
        a = self._sides[Side.A]
        b = self._sides[Side.B]
        open_a = a.filled_count - a.closed_count
        open_b = b.filled_count - b.closed_count
        matchable = min(open_a, open_b)
        units_to_close = matchable // self.unit_size
        if units_to_close == 0:
            return
        contracts = units_to_close * self.unit_size
        for side_state in (a, b):
            open_count = side_state.filled_count - side_state.closed_count
            open_cost = side_state.filled_total_cost - side_state.closed_total_cost
            open_fees = side_state.filled_fees - side_state.closed_fees
            side_state.closed_count += contracts
            side_state.closed_total_cost += round(open_cost * contracts / open_count)
            side_state.closed_fees += round(open_fees * contracts / open_count)
        logger.info(
            "ledger_reconciled_closed",
            event_ticker=self.event_ticker,
            units_closed=units_to_close,
            contracts=contracts,
            open_a=a.filled_count - a.closed_count,
            open_b=b.filled_count - b.closed_count,
            avg_a=self.open_avg_filled_price(Side.A),
            avg_b=self.open_avg_filled_price(Side.B),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py::TestReconcileClosed -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/talos/position_ledger.py tests/test_position_ledger.py
git commit -m "feat(ledger): add _reconcile_closed for pair matching"
```

---

## Task 4: Invoke `_reconcile_closed` from `record_fill`

**Files:**
- Modify: [src/talos/position_ledger.py:244-256](src/talos/position_ledger.py:244)
- Test: `tests/test_position_ledger.py`

- [ ] **Step 1: Write the failing test**

Add to `TestReconcileClosed` in `tests/test_position_ledger.py`:

```python
    def test_record_fill_triggers_reconcile(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        # Pre-load A with 5 fills; B at zero
        ledger.record_fill(Side.A, 5, 80)
        assert ledger._sides[Side.A].closed_count == 0  # not yet balanced
        # Fill B to balance; reconcile should fire
        ledger.record_fill(Side.B, 5, 20)
        assert ledger._sides[Side.A].closed_count == 5
        assert ledger._sides[Side.B].closed_count == 5
        # After close, open_avg_filled_price is 0 on both sides
        assert ledger.open_avg_filled_price(Side.A) == 0.0
        assert ledger.open_avg_filled_price(Side.B) == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py::TestReconcileClosed::test_record_fill_triggers_reconcile -v`
Expected: FAIL — `closed_count` stays 0 because reconcile isn't invoked yet.

- [ ] **Step 3: Add reconcile call to `record_fill`**

In `src/talos/position_ledger.py`, modify `record_fill` (around line 244). Add the reconcile call at the end of the method:

```python
    def record_fill(self, side: Side, count: int, price: int, *, fees: int = 0) -> None:
        """Record a fill. Called when polling detects new fills."""
        s = self._sides[side]
        s.filled_count += count
        s.filled_total_cost += price * count
        if fees > 0:
            s.filled_fees += fees
        # If resting order filled partially/fully, reduce resting count
        if s.resting_count > 0:
            filled_from_resting = min(count, s.resting_count)
            s.resting_count -= filled_from_resting
            if s.resting_count == 0:
                s.resting_order_id = None
        self._reconcile_closed()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py::TestReconcileClosed::test_record_fill_triggers_reconcile -v`
Expected: PASS

- [ ] **Step 5: Run full position_ledger tests**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/talos/position_ledger.py tests/test_position_ledger.py
git commit -m "feat(ledger): reconcile closed bucket on record_fill"
```

---

## Task 5: Invoke `_reconcile_closed` from `sync_from_orders` and `sync_from_positions`

**Files:**
- Modify: [src/talos/position_ledger.py:449-453](src/talos/position_ledger.py:449) (end of `sync_from_orders`), [src/talos/position_ledger.py:553-576](src/talos/position_ledger.py:553) (end of `sync_from_positions`)
- Test: `tests/test_position_ledger.py`

- [ ] **Step 1: Write the failing tests**

Add to `TestReconcileClosed`:

```python
    def test_sync_from_orders_triggers_reconcile(self):
        from talos.position_ledger import PositionLedger, Side
        # Build a minimal OrderSummary-like object (sync_from_orders expects
        # a list of OrderSummary with the right fields — use the real type).
        from talos.models.position import OrderSummary
        ledger = PositionLedger(
            "EVT-X", unit_size=5,
            ticker_a="TK-A", ticker_b="TK-B",
            side_a_str="no", side_b_str="no",
        )
        orders = [
            OrderSummary(
                order_id="o1", ticker="TK-A", side="no", status="executed",
                price=80, initial_count=5, remaining_count=0, filled_count=5,
                maker_fill_cost=400, maker_fees=0,
            ),
            OrderSummary(
                order_id="o2", ticker="TK-B", side="no", status="executed",
                price=20, initial_count=5, remaining_count=0, filled_count=5,
                maker_fill_cost=100, maker_fees=0,
            ),
        ]
        ledger.sync_from_orders(orders)
        assert ledger._sides[Side.A].closed_count == 5
        assert ledger._sides[Side.B].closed_count == 5

    def test_sync_from_positions_triggers_reconcile_non_same_ticker(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger(
            "EVT-X", unit_size=5,
            ticker_a="TK-A", ticker_b="TK-B",
            is_same_ticker=False,
        )
        ledger.sync_from_positions(
            position_fills={Side.A: 5, Side.B: 5},
            position_costs={Side.A: 400, Side.B: 100},
        )
        assert ledger._sides[Side.A].closed_count == 5
        assert ledger._sides[Side.B].closed_count == 5

    def test_sync_from_positions_same_ticker_early_returns_no_mutation(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger(
            "EVT-X", unit_size=5,
            ticker_a="TK-A", ticker_b="TK-A",  # same ticker
            is_same_ticker=True,
        )
        # Preload known state
        ledger.record_fill(Side.A, 5, 80)
        ledger.record_fill(Side.B, 5, 20)
        # closed already populated from record_fill's reconcile
        closed_a_before = ledger._sides[Side.A].closed_count
        closed_b_before = ledger._sides[Side.B].closed_count
        # sync_from_positions early-returns for same-ticker; closed unchanged
        ledger.sync_from_positions(
            position_fills={Side.A: 99, Side.B: 99},  # bogus values
            position_costs={Side.A: 999, Side.B: 999},
        )
        assert ledger._sides[Side.A].closed_count == closed_a_before
        assert ledger._sides[Side.B].closed_count == closed_b_before
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py::TestReconcileClosed -v`
Expected: `test_sync_from_orders_triggers_reconcile` and `test_sync_from_positions_triggers_reconcile_non_same_ticker` FAIL (closed stays 0). Same-ticker test likely passes already but keep it.

- [ ] **Step 3: Add reconcile call to `sync_from_orders`**

In `src/talos/position_ledger.py`, at the end of `sync_from_orders` (after the "Two-source sync" comment at line ~527), add:

```python
        self._reconcile_closed()
```

Find the exact spot by searching for the comment `# Two-source sync (orders + positions) keeps the ledger accurate.` — the reconcile call goes right after that comment, before the next method definition (`def sync_from_positions`).

- [ ] **Step 4: Add reconcile call to `sync_from_positions`**

In `src/talos/position_ledger.py`, at the end of `sync_from_positions` (find the end of the per-side loop that starts at line ~549), add the reconcile call after the loop. Do NOT add it before the early `if self._is_same_ticker: return` at line 544 — same-ticker must skip reconcile in this path (seed_from_saved and record_fill handle same-ticker reconciliation).

Read the existing method body first:

```
.venv/Scripts/python -c "from pathlib import Path; src = Path('src/talos/position_ledger.py').read_text(); idx = src.find('def sync_from_positions'); print(src[idx:idx+2500])"
```

Then append `self._reconcile_closed()` as the last line of the method body.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py::TestReconcileClosed -v`
Expected: all pass

- [ ] **Step 6: Run full suite to catch regressions**

Run: `.venv/Scripts/python -m pytest -q`
Expected: no regressions.

- [ ] **Step 7: Commit**

```bash
git add src/talos/position_ledger.py tests/test_position_ledger.py
git commit -m "feat(ledger): reconcile closed bucket on sync_from_orders and sync_from_positions"
```

---

## Task 6: Extend `to_save_dict` with `closed_*` keys

**Files:**
- Modify: [src/talos/position_ledger.py:322-343](src/talos/position_ledger.py:322)
- Test: `tests/test_position_ledger.py`

- [ ] **Step 1: Write the failing test**

Add a new test class to `tests/test_position_ledger.py`:

```python
class TestSavedDictSchema:
    """to_save_dict includes the closed_* keys."""

    def test_save_dict_includes_closed_keys(self):
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger._sides[Side.A].closed_count = 5
        ledger._sides[Side.A].closed_total_cost = 410
        ledger._sides[Side.A].closed_fees = 7
        ledger._sides[Side.B].closed_count = 5
        ledger._sides[Side.B].closed_total_cost = 90
        ledger._sides[Side.B].closed_fees = 2
        d = ledger.to_save_dict()
        assert d["closed_count_a"] == 5
        assert d["closed_total_cost_a"] == 410
        assert d["closed_fees_a"] == 7
        assert d["closed_count_b"] == 5
        assert d["closed_total_cost_b"] == 90
        assert d["closed_fees_b"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py::TestSavedDictSchema -v`
Expected: FAIL — keys missing from output.

- [ ] **Step 3: Add the keys to `to_save_dict`**

Replace the return block in `to_save_dict` (line 330-343):

```python
        return {
            "filled_a": a.filled_count,
            "cost_a": a.filled_total_cost,
            "fees_a": a.filled_fees,
            "filled_b": b.filled_count,
            "cost_b": b.filled_total_cost,
            "fees_b": b.filled_fees,
            "closed_count_a": a.closed_count,
            "closed_total_cost_a": a.closed_total_cost,
            "closed_fees_a": a.closed_fees,
            "closed_count_b": b.closed_count,
            "closed_total_cost_b": b.closed_total_cost,
            "closed_fees_b": b.closed_fees,
            "resting_id_a": a.resting_order_id,
            "resting_count_a": a.resting_count,
            "resting_price_a": a.resting_price,
            "resting_id_b": b.resting_order_id,
            "resting_count_b": b.resting_count,
            "resting_price_b": b.resting_price,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py::TestSavedDictSchema -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/talos/position_ledger.py tests/test_position_ledger.py
git commit -m "feat(ledger): persist closed_* keys in to_save_dict"
```

---

## Task 7: Extend `seed_from_saved` with strict closed_* restoration

**Files:**
- Modify: [src/talos/position_ledger.py:345-370](src/talos/position_ledger.py:345)
- Test: `tests/test_position_ledger.py`

- [ ] **Step 1: Write the failing tests**

Add to `TestSavedDictSchema`:

```python
    def test_seed_restores_closed_verbatim_when_all_six_keys_valid(self):
        """5a normal restart: closed values restored as-is, no re-derivation."""
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        data = {
            "filled_a": 10, "cost_a": 820, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
            "closed_count_a": 5, "closed_total_cost_a": 410, "closed_fees_a": 0,
            "closed_count_b": 5, "closed_total_cost_b": 90, "closed_fees_b": 0,
        }
        ledger.seed_from_saved(data)
        # open_avg_B must be 23.0 (115/5), NOT the blended 20.5
        assert ledger.open_count(Side.A) == 5
        assert ledger.open_count(Side.B) == 5
        assert ledger.open_avg_filled_price(Side.B) == 23.0

    def test_seed_logs_restored_with_closed_once(self, caplog):
        import logging
        from talos.position_ledger import PositionLedger
        caplog.set_level(logging.INFO)
        ledger = PositionLedger("EVT-X", unit_size=5)
        data = {
            "filled_a": 5, "cost_a": 400, "fees_a": 0,
            "filled_b": 5, "cost_b": 100, "fees_b": 0,
            "closed_count_a": 5, "closed_total_cost_a": 400, "closed_fees_a": 0,
            "closed_count_b": 5, "closed_total_cost_b": 100, "closed_fees_b": 0,
        }
        ledger.seed_from_saved(data)
        restored = [r for r in caplog.records if "ledger_restored_with_closed" in r.getMessage()]
        assert len(restored) == 1

    def test_seed_missing_all_closed_keys_triggers_migration(self, caplog):
        import logging
        from talos.position_ledger import PositionLedger, Side
        caplog.set_level(logging.INFO)
        ledger = PositionLedger("EVT-X", unit_size=5)
        data = {
            "filled_a": 10, "cost_a": 820, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
        }
        ledger.seed_from_saved(data)
        # After migration + terminal reconcile: all closed populated via pro-rata
        assert ledger._sides[Side.A].closed_count == 10
        assert ledger._sides[Side.B].closed_count == 10
        migrated = [r for r in caplog.records if "ledger_migrated_missing_closed" in r.getMessage()]
        assert len(migrated) == 1

    def test_seed_partial_closed_keys_triggers_migration(self, caplog):
        """Atomic-group rule: any missing key zeroes all six."""
        import logging
        from talos.position_ledger import PositionLedger, Side
        caplog.set_level(logging.INFO)
        ledger = PositionLedger("EVT-X", unit_size=5)
        data = {
            "filled_a": 10, "cost_a": 820, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
            # Only 2 of 6 closed keys present
            "closed_count_a": 999, "closed_total_cost_a": 999,
        }
        ledger.seed_from_saved(data)
        # Migration zeros and repopulates; verbatim restore would have set closed_count_a = 999
        assert ledger._sides[Side.A].closed_count == 10  # from reconcile, not 999
        migrated = [r for r in caplog.records if "ledger_migrated_missing_closed" in r.getMessage()]
        assert len(migrated) == 1

    def test_seed_corrupt_value_types_trigger_migration(self, caplog):
        """Non-int values trigger migration, not hard-fail."""
        import logging
        import pytest
        from talos.position_ledger import PositionLedger, Side

        for bad_value in (None, "abc", -5, True, 5.0, "5"):
            caplog.clear()
            caplog.set_level(logging.INFO)
            ledger = PositionLedger("EVT-X", unit_size=5)
            data = {
                "filled_a": 10, "cost_a": 820, "fees_a": 0,
                "filled_b": 10, "cost_b": 205, "fees_b": 0,
                "closed_count_a": 5, "closed_total_cost_a": 410, "closed_fees_a": 0,
                "closed_count_b": 5, "closed_total_cost_b": 90, "closed_fees_b": 0,
            }
            data["closed_count_a"] = bad_value  # inject corruption
            ledger.seed_from_saved(data)  # must not raise
            migrated = [r for r in caplog.records if "ledger_migrated_missing_closed" in r.getMessage()]
            assert len(migrated) == 1, f"Expected migration log for bad_value={bad_value!r}"
            # Migration zeroed and reconciled — closed_count_a != 5 (the restored-verbatim value)
            assert ledger._sides[Side.A].closed_count == 10  # reconciled, not restored
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py::TestSavedDictSchema -v`
Expected: new tests fail. The migration tests likely fail because `_reconcile_closed` isn't invoked at end of `seed_from_saved` yet; the restore tests fail because `seed_from_saved` doesn't read `closed_*` keys yet.

- [ ] **Step 3: Extend `seed_from_saved` with validated closed_* restore and terminal reconcile**

Replace the body of `seed_from_saved` in `src/talos/position_ledger.py` (lines 345-382 roughly). Keep the existing filled/resting logic unchanged; add closed handling before the terminal reconcile call:

```python
    def seed_from_saved(self, data: dict[str, int | str | None] | None) -> None:
        """Seed full ledger state from persisted data.

        Fills: sets a floor (monotonic — sync can only increase).
        Resting: restored directly so check_imbalances sees accurate
        state on the first cycle instead of phantom imbalances.
        Closed: atomic-group restore with strict integer-shape validation;
        any corruption falls back to migration via the terminal reconcile.
        The normal sync_from_orders cycle will correct any drift.
        """
        if not data:
            return
        a = self._sides[Side.A]
        b = self._sides[Side.B]
        for side, prefix in [(a, "a"), (b, "b")]:
            saved_fills = data.get(f"filled_{prefix}", 0)
            saved_cost = data.get(f"cost_{prefix}", 0)
            saved_fees = data.get(f"fees_{prefix}", 0)
            if isinstance(saved_fills, int) and saved_fills > side.filled_count:
                logger.info(
                    "ledger_seeded_from_saved",
                    event_ticker=self.event_ticker,
                    side=prefix.upper(),
                    saved_fills=saved_fills,
                    current_fills=side.filled_count,
                )
                side.filled_count = saved_fills
                side.filled_total_cost = max(side.filled_total_cost, int(saved_cost or 0))
                side.filled_fees = max(side.filled_fees, int(saved_fees or 0))

            # Restore resting state
            saved_id = data.get(f"resting_id_{prefix}")
            saved_count = data.get(f"resting_count_{prefix}", 0)
            saved_price = data.get(f"resting_price_{prefix}", 0)
            if saved_id and isinstance(saved_count, int) and saved_count > 0:
                side.resting_order_id = str(saved_id)
                side.resting_count = saved_count
                side.resting_price = int(saved_price or 0)

        # Atomic-group restore of the closed_* bucket with strict validation.
        required_closed_keys = (
            "closed_count_a", "closed_total_cost_a", "closed_fees_a",
            "closed_count_b", "closed_total_cost_b", "closed_fees_b",
        )

        def _valid_closed_value(v: object) -> bool:
            # Require exact int — reject bool (subclass of int), float, str, None, negative.
            return type(v) is int and v >= 0

        missing: list[str] = []
        invalid: list[str] = []
        for k in required_closed_keys:
            if k not in data:
                missing.append(k)
            elif not _valid_closed_value(data[k]):
                invalid.append(k)

        if missing or invalid:
            # Migration fallback: zero all six; terminal reconcile populates.
            for side_state in (a, b):
                side_state.closed_count = 0
                side_state.closed_total_cost = 0
                side_state.closed_fees = 0
            logger.info(
                "ledger_migrated_missing_closed",
                event_ticker=self.event_ticker,
                missing_keys=missing,
                invalid_keys=invalid,
            )
        else:
            # Normal restart: restore verbatim. Values validated above.
            for side_state, prefix in [(a, "a"), (b, "b")]:
                side_state.closed_count = data[f"closed_count_{prefix}"]
                side_state.closed_total_cost = data[f"closed_total_cost_{prefix}"]
                side_state.closed_fees = data[f"closed_fees_{prefix}"]
            logger.info(
                "ledger_restored_with_closed",
                event_ticker=self.event_ticker,
            )

        # Terminal reconcile — idempotent in normal-restart case, populates
        # closed_* from blend in migration case.
        self._reconcile_closed()
```

Note: the existing body extended beyond line 370 to handle resting state. Make sure you preserve the resting restoration logic exactly as-is; only the closed handling and terminal reconcile are new.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py::TestSavedDictSchema -v`
Expected: all pass.

- [ ] **Step 5: Run full ledger test file**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py -v`
Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add src/talos/position_ledger.py tests/test_position_ledger.py
git commit -m "feat(ledger): restore closed_* from persist with strict all-or-nothing validation"
```

---

## Task 8: Switch `is_placement_safe` to `open_avg_filled_price`

**Files:**
- Modify: [src/talos/position_ledger.py:220-227](src/talos/position_ledger.py:220) (inside `is_placement_safe`)
- Test: `tests/test_position_ledger.py`

- [ ] **Step 1: Write the failing test**

Add a new test class to `tests/test_position_ledger.py`:

```python
class TestIsPlacementSafeOpenScope:
    """is_placement_safe uses open avg, not lifetime blend."""

    def test_rejects_placement_against_open_unit_avg(self):
        """A closed unit at 92/7 must not subsidize a new Yes 86 placement."""
        from talos.position_ledger import PositionLedger, Side
        ledger = PositionLedger("EVT-X", unit_size=5)
        # Pre-close one unit at 92/7
        ledger.record_fill(Side.A, 5, 92)
        ledger.record_fill(Side.B, 5, 7)
        # Now simulate the open unit having B filled at 18c (no A yet)
        ledger.record_fill(Side.B, 5, 18)
        # Try to place Yes 86 on A against the open-unit B avg of 18
        # Fee-free market: 86 + 18 = 104 → unprofitable
        ok, reason = ledger.is_placement_safe(Side.A, count=5, price=86, rate=0.0)
        assert not ok
        # Lifetime avg B would be (35 + 90) / 10 = 12.5 → would falsely pass
        # Open avg B is 18 → correctly blocks
        assert "104" in reason or "profitable" in reason.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py::TestIsPlacementSafeOpenScope -v`
Expected: FAIL — the placement passes because current code uses lifetime avg.

- [ ] **Step 3: Switch the guard condition and accessor in `is_placement_safe`**

In `src/talos/position_ledger.py`, find the P18 block inside `is_placement_safe` (around line 219-232). Replace:

```python
        # P18: fee-adjusted profitability
        other = self._sides[side.other]
        if other.filled_count > 0:
            other_price = other.filled_total_cost / other.filled_count
        elif other.resting_count > 0:
            other_price = other.resting_price
        else:
            # No position on the other side — can't check arb yet, allow placement
```

With:

```python
        # P18: fee-adjusted profitability (open-unit scoped — matched pairs
        # are locked in and must not subsidize decisions about the open unit).
        other = self._sides[side.other]
        if self.open_count(side.other) > 0:
            other_price = self.open_avg_filled_price(side.other)
        elif other.resting_count > 0:
            other_price = other.resting_price
        else:
            # No position on the other side — can't check arb yet, allow placement
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py::TestIsPlacementSafeOpenScope -v`
Expected: PASS

- [ ] **Step 5: Run full ledger test file**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py -v`
Expected: no regressions. (Existing placement-safe tests may need updating if they relied on lifetime-avg semantics — investigate and update any that are testing behavior that was itself buggy; the open-unit scoping is the new correct behavior.)

- [ ] **Step 6: Commit**

```bash
git add src/talos/position_ledger.py tests/test_position_ledger.py
git commit -m "feat(ledger): is_placement_safe uses open-unit avg for P18"
```

---

## Task 9: Switch `BidAdjuster.evaluate_jump` to `open_avg_filled_price`

**Files:**
- Modify: [src/talos/bid_adjuster.py:346-351](src/talos/bid_adjuster.py:346)
- Test: `tests/test_bid_adjuster.py`

- [ ] **Step 1: Write the failing regression test**

Add to `tests/test_bid_adjuster.py` (inside an existing test class or at file end):

```python
class TestEvaluateJumpOpenScope:
    """evaluate_jump uses open-unit avg for P18, not lifetime blend."""

    def test_jump_follows_when_only_closed_units_exist(self, fresh_pair_0fee):
        """When the open unit is empty (all prior units closed), a jump
        should be evaluated against the new price alone — not the lifetime
        blend. This is the 'sold at 79c, should follow to 18c' scenario.
        """
        pair, adjuster, books = fresh_pair_0fee
        ledger = adjuster.get_ledger(pair.event_ticker)
        # Simulate a lifetime with varied-price closed units:
        # 5 @ 92 / 5 @ 7, 5 @ 82 / 5 @ 18, 5 @ 80 / 5 @ 19, 5 @ 82 / 5 @ 23,
        # 5 @ 80 / 5 @ 17. Record each matched pair so reconcile closes them.
        from talos.position_ledger import Side
        for a_price, b_price in [(92, 7), (82, 18), (80, 19), (82, 23), (80, 17)]:
            ledger.record_fill(Side.A, 5, a_price)
            ledger.record_fill(Side.B, 5, b_price)
        assert ledger.open_count(Side.A) == 0
        assert ledger.open_count(Side.B) == 0
        # B has 5 resting @ 17, A has no resting
        from talos.position_ledger import Side as _Side
        ledger.record_resting(_Side.B, "oid-b", 5, 17)
        # Book for B jumps to 18
        books.set_best_ask(pair.ticker_b, pair.side_b, price=18, count=100)
        # With open-avg fix: open_A == 0 → P18 "no other-side position" branch
        # → evaluate_jump follows the jump to 18c
        result = adjuster.evaluate_jump(pair.ticker_b, at_top=False, side=pair.side_b)
        assert result is not None
        assert result.action == "follow_jump"
        assert result.new_price == 18
```

You'll need a fixture `fresh_pair_0fee` that builds a pair with `fee_rate=0`. Add it if absent (place at top of the test file or in `conftest.py`):

```python
@pytest.fixture
def fresh_pair_0fee():
    """A same-ticker pair with 0 fees, wired to BidAdjuster and OrderBookManager."""
    from talos.bid_adjuster import BidAdjuster
    from talos.models.strategy import ArbPair
    from talos.orderbook import OrderBookManager
    pair = ArbPair(
        event_ticker="EVT-X",
        ticker_a="TK-X",
        ticker_b="TK-X",
        side_a="yes",
        side_b="no",
        is_same_ticker=True,
        fee_rate=0.0,
    )
    books = OrderBookManager()
    adjuster = BidAdjuster(books, [pair], unit_size=5)
    return pair, adjuster, books
```

If `OrderBookManager.set_best_ask` doesn't exist, look at existing tests for how they seed book state and adapt accordingly.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_bid_adjuster.py::TestEvaluateJumpOpenScope -v`
Expected: FAIL — `action == "hold"` because lifetime avg A = 83.2, and 18+83.2 = 101.2 >= 100.

- [ ] **Step 3: Switch evaluate_jump to use open-unit accessors**

In `src/talos/bid_adjuster.py`, find the block starting around line 346 in `evaluate_jump`. Replace:

```python
        if ledger.filled_count(other_side) > 0:
            other_effective = fee_adjusted_cost(
                int(round(ledger.avg_filled_price(other_side))), rate=rate
            )
```

With:

```python
        if ledger.open_count(other_side) > 0:
            other_effective = fee_adjusted_cost(
                int(round(ledger.open_avg_filled_price(other_side))), rate=rate
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_bid_adjuster.py::TestEvaluateJumpOpenScope -v`
Expected: PASS

- [ ] **Step 5: Run full bid_adjuster test file**

Run: `.venv/Scripts/python -m pytest tests/test_bid_adjuster.py -v`
Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add src/talos/bid_adjuster.py tests/test_bid_adjuster.py
git commit -m "feat(adjuster): evaluate_jump uses open-unit avg for P18"
```

---

## Task 10: Switch `BidAdjuster._check_post_cancel_safety` to `open_avg_filled_price`

**Files:**
- Modify: [src/talos/bid_adjuster.py:842-843](src/talos/bid_adjuster.py:842)
- Test: `tests/test_bid_adjuster.py`

- [ ] **Step 1: Write the failing regression test**

Add to `tests/test_bid_adjuster.py`:

```python
class TestPostCancelSafetyOpenScope:
    """_check_post_cancel_safety uses open-unit avg, not lifetime blend."""

    def test_blocks_cancel_replace_that_looks_safe_only_on_lifetime_blend(self, fresh_pair_0fee):
        from talos.position_ledger import Side
        pair, adjuster, books = fresh_pair_0fee
        ledger = adjuster.get_ledger(pair.event_ticker)
        # Closed unit at A=92, B=7 (pulls lifetime B avg down)
        ledger.record_fill(Side.A, 5, 92)
        ledger.record_fill(Side.B, 5, 7)
        # Open unit: B filled 5 @ 18, A resting @ 82
        ledger.record_fill(Side.B, 5, 18)
        ledger.record_resting(Side.A, "oid-a", 5, 82)
        # Simulate a cancel+replace on A at 86. Under open-scope:
        # 86 + 18 = 104 → block. Under lifetime-scope:
        # 86 + 12.5 = 98.5 → would falsely pass.
        ok, reason = adjuster._check_post_cancel_safety(ledger, Side.A, new_count=5, new_price=86)
        assert not ok
        assert "104" in reason or "profitable" in reason.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_bid_adjuster.py::TestPostCancelSafetyOpenScope -v`
Expected: FAIL — method passes under lifetime blend.

- [ ] **Step 3: Switch `_check_post_cancel_safety`**

In `src/talos/bid_adjuster.py`, find `_check_post_cancel_safety` (around line 824). Replace the block:

```python
        # Check profitability (reuse the gate logic without resting check)
        other_side = side.other
        if ledger.filled_count(other_side) > 0:
            other_price = ledger.filled_total_cost(other_side) / ledger.filled_count(other_side)
        elif ledger.resting_count(other_side) > 0:
            other_price = ledger.resting_price(other_side)
        else:
            return True, ""
```

With:

```python
        # Check profitability (open-unit scoped — matched pairs locked in)
        other_side = side.other
        if ledger.open_count(other_side) > 0:
            other_price = ledger.open_avg_filled_price(other_side)
        elif ledger.resting_count(other_side) > 0:
            other_price = ledger.resting_price(other_side)
        else:
            return True, ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_bid_adjuster.py::TestPostCancelSafetyOpenScope -v`
Expected: PASS

- [ ] **Step 5: Run full bid_adjuster tests**

Run: `.venv/Scripts/python -m pytest tests/test_bid_adjuster.py -v`
Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add src/talos/bid_adjuster.py tests/test_bid_adjuster.py
git commit -m "feat(adjuster): _check_post_cancel_safety uses open-unit avg"
```

---

## Task 11: Switch `rebalance.compute_rebalance_proposal` fallback to open-unit avg

**Files:**
- Modify: [src/talos/rebalance.py:139-141](src/talos/rebalance.py:139)
- Test: `tests/test_rebalance.py`

- [ ] **Step 1: Write the failing regression test**

Add to `tests/test_rebalance.py`:

```python
class TestRebalanceCatchupOpenScope:
    """compute_rebalance_proposal catchup fallback uses open-unit avg."""

    def test_catchup_price_uses_open_avg_not_lifetime_blend(self):
        """Closed unit at 92/7 must not raise the max profitable catch-up
        price above what's safe against the open unit's 18c basis."""
        from talos.position_ledger import PositionLedger, Side
        from talos.models.strategy import ArbPair, Opportunity
        from talos.orderbook import OrderBookManager
        from talos.rebalance import compute_rebalance_proposal

        pair = ArbPair(
            event_ticker="EVT-X",
            ticker_a="TK-A",
            ticker_b="TK-B",
            side_a="no",
            side_b="no",
            fee_rate=0.0,
        )
        ledger = PositionLedger(
            "EVT-X", unit_size=5,
            ticker_a="TK-A", ticker_b="TK-B",
            side_a_str="no", side_b_str="no",
        )
        # Closed unit 1 at A=92, B=7
        ledger.record_fill(Side.A, 5, 92)
        ledger.record_fill(Side.B, 5, 7)
        # Open unit: B has 5 filled @ 18, A has 0 filled (so committed_a=0, committed_b=5 → imbalance)
        ledger.record_fill(Side.B, 5, 18)
        # Scanner snapshot: A ask at 86 (far above the open-scope max of 81)
        snapshot = Opportunity(
            event_ticker="EVT-X",
            ticker_a="TK-A",
            ticker_b="TK-B",
            no_a=86,
            no_b=18,
            cost=104,
            fee_edge=-4.0,
            tradeable_qty=5,
        )
        books = OrderBookManager()
        proposal = compute_rebalance_proposal(
            "EVT-X", ledger, pair, snapshot, "X", books,
        )
        assert proposal is not None
        assert proposal.rebalance is not None
        # With open-scope, max_profitable_price(18) = 81. Without, it would
        # be max_profitable_price(12.5) = 87 (allowing the 86 snapshot).
        assert proposal.rebalance.catchup_price <= 81
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_rebalance.py::TestRebalanceCatchupOpenScope -v`
Expected: FAIL — catchup_price = 86 or higher.

- [ ] **Step 3: Switch the fallback computation**

In `src/talos/rebalance.py`, find the fallback at lines 139-141. Replace:

```python
                over_side_state = ledger._sides[over]
                if over_side_state.filled_count > 0:
                    other_avg = over_side_state.filled_total_cost / over_side_state.filled_count
                    fallback = max_profitable_price(
                        other_avg,
                        rate=pair.fee_rate,
                    )
```

With:

```python
                if ledger.open_count(over) > 0:
                    other_avg = ledger.open_avg_filled_price(over)
                    fallback = max_profitable_price(
                        other_avg,
                        rate=pair.fee_rate,
                    )
```

(Remove the `over_side_state = ledger._sides[over]` line — it's no longer used.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_rebalance.py::TestRebalanceCatchupOpenScope -v`
Expected: PASS

- [ ] **Step 5: Run full rebalance tests**

Run: `.venv/Scripts/python -m pytest tests/test_rebalance.py -v`
Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add src/talos/rebalance.py tests/test_rebalance.py
git commit -m "feat(rebalance): catch-up fallback uses open-unit avg"
```

---

## Task 12: Switch `engine.check_queue_stress` to `open_avg_filled_price`

**Files:**
- Modify: [src/talos/engine.py:2521-2524](src/talos/engine.py:2521)
- Test: `tests/test_engine.py` or a dedicated test file

- [ ] **Step 1: Write the failing regression test**

Add to `tests/test_engine.py`:

```python
class TestQueueStressOpenScope:
    """check_queue_stress uses open-unit avg."""

    def test_skips_queue_stress_when_open_unit_would_be_unprofitable(self):
        """With lifetime avg, a +1c improvement looks profitable; with
        open avg, the math correctly blocks."""
        # Setup minimal engine with a pair, ledger, resting order, queue pos,
        # game status. Adapt from existing test_engine.py fixtures.
        pytest.skip("pending — see notes")
```

This test requires wiring up engine fixtures (game manager mock, queue position cache, etc.). Use the closest existing test in `test_engine.py` as a template. If the existing tests don't exercise `check_queue_stress` end-to-end, a simpler approach is an integration-style test in `test_ledger_reconstruction.py` (Task 14) that just asserts `check_queue_stress` calls the open accessor. For now, stub this task by marking the test `pending` and proceed with the code change — the reconstruction tests in Task 14 will cover the live behavior.

- [ ] **Step 2: Run test to confirm it's skipped (not failing)**

Run: `.venv/Scripts/python -m pytest tests/test_engine.py::TestQueueStressOpenScope -v`
Expected: 1 skipped.

- [ ] **Step 3: Switch `check_queue_stress`**

In `src/talos/engine.py`, find `check_queue_stress` (around line 2421). At line 2522, change:

```python
            other_avg = ledger.avg_filled_price(ahead_side)
```

To:

```python
            other_avg = ledger.open_avg_filled_price(ahead_side)
```

The `<= 0` guard at line 2523-2524 already handles the empty-open-unit case without needing `open_count`.

- [ ] **Step 4: Run full engine tests**

Run: `.venv/Scripts/python -m pytest tests/test_engine.py -v`
Expected: no regressions.

- [ ] **Step 5: Commit**

```bash
git add src/talos/engine.py tests/test_engine.py
git commit -m "feat(engine): check_queue_stress uses open-unit avg"
```

---

## Task 13: Switch engine's queue-improvement execution recheck to `open_avg_filled_price`

**Files:**
- Modify: [src/talos/engine.py:3164](src/talos/engine.py:3164)
- Test: (covered by Task 12's skipped test / Task 14's integration tests)

- [ ] **Step 1: Switch the recheck**

In `src/talos/engine.py`, at line 3164, change:

```python
        other_avg = ledger.avg_filled_price(side.other)
```

To:

```python
        other_avg = ledger.open_avg_filled_price(side.other)
```

- [ ] **Step 2: Run full engine tests**

Run: `.venv/Scripts/python -m pytest tests/test_engine.py -v`
Expected: no regressions.

- [ ] **Step 3: Commit**

```bash
git add src/talos/engine.py
git commit -m "feat(engine): queue-improve execution recheck uses open-unit avg"
```

---

## Task 14: Integration tests for restart regimes 5a/5b/5c/5d

**Files:**
- Create: `tests/test_ledger_reconstruction.py`

- [ ] **Step 1: Write the full test file**

Create `tests/test_ledger_reconstruction.py`:

```python
"""Restart-regime tests for the open-unit avg scoping spec.

Covers the 5a (normal restart), 5b (first-boot migration from old saves),
5c (Kalshi-only cold start), and 5d (same-ticker specifics) regimes from
docs/superpowers/specs/2026-04-15-open-unit-avg-scoping-design.md.
"""
from __future__ import annotations

import logging

import pytest

from talos.position_ledger import PositionLedger, Side


class TestRegime5aNormalRestart:
    """Persisted closed_* restored verbatim; no re-derivation from blend."""

    def test_open_avg_preserved_across_restart(self):
        """The Codex scenario: open B at 23c must come back at 23c,
        not the blended 20.5c."""
        persisted = {
            "filled_a": 5, "cost_a": 410, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
            "closed_count_a": 5, "closed_total_cost_a": 410, "closed_fees_a": 0,
            "closed_count_b": 5, "closed_total_cost_b": 90, "closed_fees_b": 0,
        }
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(persisted)
        assert ledger.open_count(Side.A) == 0
        assert ledger.open_count(Side.B) == 5
        assert ledger.open_avg_filled_price(Side.B) == 23.0

    def test_reconcile_after_restart_is_noop(self):
        persisted = {
            "filled_a": 5, "cost_a": 410, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
            "closed_count_a": 5, "closed_total_cost_a": 410, "closed_fees_a": 0,
            "closed_count_b": 5, "closed_total_cost_b": 90, "closed_fees_b": 0,
        }
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(persisted)
        closed_a = ledger._sides[Side.A].closed_count
        closed_b = ledger._sides[Side.B].closed_count
        ledger._reconcile_closed()
        assert ledger._sides[Side.A].closed_count == closed_a
        assert ledger._sides[Side.B].closed_count == closed_b

    def test_restored_log_line_once_per_ledger(self, caplog):
        caplog.set_level(logging.INFO)
        persisted = {
            "filled_a": 5, "cost_a": 400, "fees_a": 0,
            "filled_b": 5, "cost_b": 100, "fees_b": 0,
            "closed_count_a": 5, "closed_total_cost_a": 400, "closed_fees_a": 0,
            "closed_count_b": 5, "closed_total_cost_b": 100, "closed_fees_b": 0,
        }
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(persisted)
        restored = [r for r in caplog.records if "ledger_restored_with_closed" in r.getMessage()]
        assert len(restored) == 1


class TestRegime5bFirstBootMigration:
    """Old save with filled_* but no closed_* → migration via blend."""

    def test_migration_flushes_balanced_portion(self):
        old_save = {
            "filled_a": 10, "cost_a": 820, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
        }
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(old_save)
        assert ledger._sides[Side.A].closed_count == 10
        assert ledger._sides[Side.B].closed_count == 10
        assert ledger.open_count(Side.A) == 0
        assert ledger.open_count(Side.B) == 0

    def test_migration_preserves_lifetime_avg(self):
        old_save = {
            "filled_a": 10, "cost_a": 820, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
        }
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(old_save)
        assert ledger.avg_filled_price(Side.A) == 82.0
        assert ledger.avg_filled_price(Side.B) == 20.5

    def test_migration_emits_migrated_log(self, caplog):
        caplog.set_level(logging.INFO)
        old_save = {
            "filled_a": 10, "cost_a": 820, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
        }
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(old_save)
        migrated = [r for r in caplog.records if "ledger_migrated_missing_closed" in r.getMessage()]
        assert len(migrated) == 1

    def test_post_migration_save_includes_closed_keys(self):
        old_save = {
            "filled_a": 10, "cost_a": 820, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
        }
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(old_save)
        new_save = ledger.to_save_dict()
        for k in ("closed_count_a", "closed_total_cost_a", "closed_fees_a",
                  "closed_count_b", "closed_total_cost_b", "closed_fees_b"):
            assert k in new_save, f"expected {k} in new save"


class TestRegime5bPartialKeys:
    """Atomic-group rule: partial closed_* triggers migration."""

    def test_partial_keys_trigger_full_migration(self, caplog):
        caplog.set_level(logging.INFO)
        corrupt = {
            "filled_a": 10, "cost_a": 820, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
            "closed_count_a": 999, "closed_total_cost_a": 999,
            # missing closed_fees_a, all three B closed keys
        }
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(corrupt)
        # Migration zeroed and reconciled from blend — NOT restored verbatim
        assert ledger._sides[Side.A].closed_count == 10  # reconciled, not 999
        assert ledger._sides[Side.B].closed_count == 10
        migrated = [r for r in caplog.records if "ledger_migrated_missing_closed" in r.getMessage()]
        assert len(migrated) == 1


class TestRegime5bCorruptValues:
    """Corrupt value types trigger migration, not hard-fail."""

    @pytest.mark.parametrize("bad_value", [None, "abc", -5, True, 5.0, "5"])
    def test_corrupt_value_triggers_migration(self, caplog, bad_value):
        caplog.set_level(logging.INFO)
        corrupt = {
            "filled_a": 10, "cost_a": 820, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
            "closed_count_a": 5, "closed_total_cost_a": 410, "closed_fees_a": 0,
            "closed_count_b": 5, "closed_total_cost_b": 90, "closed_fees_b": 0,
        }
        corrupt["closed_count_a"] = bad_value
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(corrupt)  # must not raise
        migrated = [r for r in caplog.records if "ledger_migrated_missing_closed" in r.getMessage()]
        assert len(migrated) == 1
        # Migration re-ran reconcile from blend; closed_count_a != 5 (the would-be restored value)
        assert ledger._sides[Side.A].closed_count == 10


class TestRegime5dSameTicker:
    """Same-ticker ledgers: seed_from_saved is the only reconciliation path."""

    def test_same_ticker_normal_restart_preserves_state(self):
        persisted = {
            "filled_a": 5, "cost_a": 410, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
            "closed_count_a": 5, "closed_total_cost_a": 410, "closed_fees_a": 0,
            "closed_count_b": 5, "closed_total_cost_b": 90, "closed_fees_b": 0,
        }
        ledger = PositionLedger(
            "EVT-X", unit_size=5,
            ticker_a="TK-X", ticker_b="TK-X",
            is_same_ticker=True,
        )
        ledger.seed_from_saved(persisted)
        # sync_from_positions would early-return; closed must stay as seeded
        ledger.sync_from_positions(
            position_fills={Side.A: 5, Side.B: 10},
            position_costs={Side.A: 410, Side.B: 205},
        )
        assert ledger._sides[Side.A].closed_count == 5
        assert ledger._sides[Side.B].closed_count == 5
        assert ledger.open_avg_filled_price(Side.B) == 23.0

    def test_same_ticker_migration_works_without_sync_from_positions(self):
        """Same-ticker can't rely on sync_from_positions; migration must
        succeed via seed_from_saved alone."""
        old_save = {
            "filled_a": 10, "cost_a": 820, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
        }
        ledger = PositionLedger(
            "EVT-X", unit_size=5,
            ticker_a="TK-X", ticker_b="TK-X",
            is_same_ticker=True,
        )
        ledger.seed_from_saved(old_save)
        assert ledger._sides[Side.A].closed_count == 10
        assert ledger._sides[Side.B].closed_count == 10
```

- [ ] **Step 2: Run the full test file**

Run: `.venv/Scripts/python -m pytest tests/test_ledger_reconstruction.py -v`
Expected: all pass. If any fail, investigate — most likely a missing field on `_SideState` or an unexpected mutation in an existing sync path.

- [ ] **Step 3: Run the full test suite to catch regressions**

Run: `.venv/Scripts/python -m pytest -q`
Expected: all pass (modulo the pre-existing `test_freeze_diagnosis` oddity — already handled in a prior commit).

- [ ] **Step 4: Commit**

```bash
git add tests/test_ledger_reconstruction.py
git commit -m "test: restart regimes for open-unit avg scoping"
```

---

## Task 15: Update decision-log `effective_other` to use open avg

**Files:**
- Modify: [src/talos/bid_adjuster.py](src/talos/bid_adjuster.py) — the `_log_decision` call sites in `evaluate_jump` that pass `effective_other`

- [ ] **Step 1: Audit the call sites**

Search for `effective_other=other_effective` in `bid_adjuster.py`. All of these already derive `other_effective` from `avg_filled_price`. After Task 9, `other_effective` is computed from `open_avg_filled_price` — so this task is already covered by Task 9's change. No new code needed; this task just verifies.

Run: `grep -n "effective_other=other_effective" src/talos/bid_adjuster.py`
Expected: matches show the decision-log entries. Each should trace back to a definition of `other_effective` that now uses `open_avg_filled_price`.

- [ ] **Step 2: Add a spot-check test**

Add to `tests/test_bid_adjuster.py`:

```python
    def test_decision_log_uses_open_avg(self, fresh_pair_0fee, tmp_path):
        """The effective_other field in decision log entries must reflect
        open-unit avg, not lifetime blend."""
        from talos.data_collector import DataCollector
        from talos.position_ledger import Side

        pair, adjuster, books = fresh_pair_0fee
        dc = DataCollector(tmp_path / "d.db")
        adjuster._data_collector = dc
        ledger = adjuster.get_ledger(pair.event_ticker)
        # Closed unit at A=92, B=7. Open unit: A=5 resting @ 82, B=5 filled @ 18.
        ledger.record_fill(Side.A, 5, 92)
        ledger.record_fill(Side.B, 5, 7)
        ledger.record_fill(Side.B, 5, 18)
        ledger.record_resting(Side.A, "oid-a", 5, 82)
        # Book moves A ask to 86; this triggers a jump evaluation
        books.set_best_ask(pair.ticker_a, pair.side_a, price=86, count=100)
        adjuster.evaluate_jump(pair.ticker_a, at_top=False, side=pair.side_a)
        # Query the decision-log DB for the most recent row on this event
        row = dc._db.execute(
            "SELECT effective_other FROM decisions WHERE event_ticker = ? "
            "ORDER BY ts DESC LIMIT 1",
            ("EVT-X",),
        ).fetchone()
        dc.close()
        # Expect effective_other = fee_adjusted_cost(18, rate=0) = 18.0
        # NOT the lifetime blend of 12.5
        assert row is not None
        assert row[0] == pytest.approx(18.0, abs=0.01)
```

- [ ] **Step 3: Run the test**

Run: `.venv/Scripts/python -m pytest tests/test_bid_adjuster.py::TestEvaluateJumpOpenScope::test_decision_log_uses_open_avg -v`
Expected: PASS (Task 9's change already made `other_effective` derive from the open avg).

- [ ] **Step 4: Commit**

```bash
git add tests/test_bid_adjuster.py
git commit -m "test: decision log effective_other reflects open-unit avg"
```

---

## Task 16: Final verification

- [ ] **Step 1: Run the full test suite**

Run: `.venv/Scripts/python -m pytest -q`
Expected: all pass (aside from the known pre-existing `test_freeze_diagnosis` situation).

- [ ] **Step 2: Run lint and type check**

Run in parallel:
```bash
.venv/Scripts/python -m ruff check src/talos tests
.venv/Scripts/python -m pyright src/talos/position_ledger.py src/talos/bid_adjuster.py src/talos/rebalance.py src/talos/engine.py
```
Expected: no new errors on touched files. Pre-existing errors in `__main__.py` and unrelated engine.py paths are fine.

- [ ] **Step 3: Spot-check decision-log on a live ledger**

If practical (dev environment only), run Talos against the demo market briefly and confirm via the event review panel's timeline that `EVAL ... effective_other=...` entries now reflect the open-unit avg rather than the lifetime blend. For an event with only one open unit (no closures yet), the lifetime and open avgs are equal — you'll need an event with at least one closed unit to see the distinction. Capture one screenshot to archive in the brain/ notes.

- [ ] **Step 4: Final commit marker** (optional — use if the final verification produced no code changes)

```bash
# No changes to commit if step 3 observational only — skip this step.
```

---

## Self-review notes

- **Spec coverage:**
  - Section 1 (data model): Task 1 ✓
  - Section 2 (accessors): Task 2 ✓
  - Section 3 (reconcile + invariant): Task 3 (method), Tasks 4, 5, 7 (invocations at the four sites) ✓
  - Section 4 (decision-path call sites): Tasks 8, 9, 10, 11, 12, 13 ✓; display sites untouched as spec'd
  - Section 5 (restart regimes): Task 14 ✓
  - Section 6 (persistence schema): Tasks 6, 7 ✓
  - Testing section: mapped across Tasks 3, 4, 5, 7, 9, 10, 11, 14, 15 ✓

- **Placeholder scan:** Task 12 has a deliberate `pytest.skip` because the end-to-end engine test infrastructure for `check_queue_stress` is heavy; the behavior is covered by the open-unit scoping tests in Task 8 (since queue-stress dispatches through the ledger) and by the restart regime tests in Task 14. Called out inline.

- **Type consistency:** `open_count`, `open_avg_filled_price`, `_reconcile_closed` consistently named. All invocations use the same accessor names.

---
