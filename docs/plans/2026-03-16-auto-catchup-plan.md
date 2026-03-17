# Auto Catch-Up Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-execute rebalance catch-ups with full gap closure in one shot, removing manual approval delay.

**Architecture:** Three modules change: `position_ledger.py` gains a `catchup` flag on the safety gate, `rebalance.py` fixes detection logic and hardens execution, `engine.py` makes `check_imbalances()` async with auto-execution. TDD throughout — every behavior change starts with a failing test.

**Tech Stack:** Python 3.12+, pytest, pytest-asyncio, structlog

**Spec:** `docs/plans/2026-03-16-auto-catchup-design.md`

---

### Task 1: Add `catchup` parameter to `is_placement_safe()`

**Files:**
- Modify: `src/talos/position_ledger.py:141-182`
- Test: `tests/test_position_ledger.py`

- [ ] **Step 1: Write failing tests for `catchup=True` behavior**

```python
# In tests/test_position_ledger.py, add to existing test class:

class TestPlacementSafetyCatchup:
    def test_catchup_bypasses_unit_gate(self):
        """catchup=True skips P16 unit-boundary check."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.B, 15, 48)
        # 15 filled_in_unit + 0 resting + 25 new = 40 > 20 → blocked normally
        ok, reason = ledger.is_placement_safe(Side.B, 25, 48, catchup=True)
        assert ok, f"catchup should bypass unit gate: {reason}"

    def test_catchup_still_enforces_profitability(self):
        """catchup=True still checks P18 profitability."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, 20, 55)  # other side at 55c
        # 55 + 55 = 110 >= 100 → unprofitable
        ok, reason = ledger.is_placement_safe(Side.B, 20, 55, catchup=True)
        assert not ok
        assert "not profitable" in reason

    def test_default_catchup_false_preserves_unit_gate(self):
        """Default catchup=False still enforces P16 (no regression)."""
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.B, 15, 48)
        ok, reason = ledger.is_placement_safe(Side.B, 25, 48)
        assert not ok
        assert "exceed unit" in reason
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py::TestPlacementSafetyCatchup -v`
Expected: FAIL — `is_placement_safe()` doesn't accept `catchup` kwarg

- [ ] **Step 3: Add `catchup` parameter to `is_placement_safe()`**

In `src/talos/position_ledger.py`, change the method signature and add the skip:

```python
def is_placement_safe(
    self, side: Side, count: int, price: int, *, rate: float = MAKER_FEE_RATE,
    catchup: bool = False,
) -> tuple[bool, str]:
    s = self._sides[side]

    # P16: unit boundary (skipped for catch-up — closing a gap, not speculative)
    if not catchup:
        filled_in_unit = s.filled_count % self.unit_size
        if filled_in_unit + s.resting_count + count > self.unit_size:
            return (
                False,
                f"would exceed unit: filled_in_unit={filled_in_unit} + "
                f"resting={s.resting_count} + new={count} > {self.unit_size}",
            )

    # P18: fee-adjusted profitability (always enforced)
    other = self._sides[side.other]
    if other.filled_count > 0:
        other_price = other.filled_total_cost / other.filled_count
    elif other.resting_count > 0:
        other_price = other.resting_price
    else:
        return True, ""

    effective_this = fee_adjusted_cost(price, rate=rate)
    effective_other = fee_adjusted_cost(int(round(other_price)), rate=rate)
    if effective_this + effective_other >= 100:
        return (
            False,
            f"arb not profitable after fees: "
            f"{effective_this:.2f} + {effective_other:.2f} = "
            f"{effective_this + effective_other:.2f} >= 100",
        )

    return True, ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py -v`
Expected: ALL PASS (new tests + existing tests — no regressions)

- [ ] **Step 5: Commit**

```bash
git add src/talos/position_ledger.py tests/test_position_ledger.py
git commit -m "feat: add catchup flag to is_placement_safe() to bypass P16 for catch-up orders"
```

---

### Task 2: Fix `compute_rebalance_proposal` detection logic

**Files:**
- Modify: `src/talos/rebalance.py:42-182`
- Test: `tests/test_rebalance.py`

- [ ] **Step 1: Write failing tests for new detection behavior**

```python
# In tests/test_rebalance.py, update and add tests:

def test_catchup_full_gap_not_capped(self):
    """Catch-up quantity is the full gap, not capped at unit_size."""
    pair = _make_pair()
    ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
    ledger.record_fill(Side.A, 40, 45)
    ledger.record_fill(Side.B, 15, 48)
    snapshot = _make_snapshot(no_a=45, no_b=48)

    result = compute_rebalance_proposal(
        "EVT-1", ledger, pair, snapshot, "Test", _books_with_data()
    )
    assert result is not None
    assert result.rebalance is not None
    assert result.rebalance.catchup_qty == 25  # full gap, not capped at 20

def test_target_is_over_filled_not_max(self):
    """Target = over_filled. Over-side resting is always cancelled."""
    pair = _make_pair()
    ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
    ledger.record_fill(Side.A, 30, 45)
    ledger.record_resting(Side.A, "ord-a", 20, 45)  # 50 committed
    ledger.record_fill(Side.B, 10, 48)
    snapshot = _make_snapshot(no_a=45, no_b=48)

    result = compute_rebalance_proposal(
        "EVT-1", ledger, pair, snapshot, "Test", OrderBookManager()
    )
    assert result is not None
    assert result.rebalance is not None
    # Step 1: cancel ALL 20 resting on A (target = 30 filled, not max(30,10)=30)
    assert result.rebalance.target_resting == 0
    assert result.rebalance.current_resting == 20
    # Step 2: catch-up B from 10 to 30 = 20 contracts
    assert result.rebalance.catchup_qty == 20

def test_under_resting_reduces_effective_gap(self):
    """Existing resting on under-side reduces effective catch-up needed."""
    pair = _make_pair()
    ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
    ledger.record_fill(Side.A, 40, 45)
    ledger.record_fill(Side.B, 15, 48)
    ledger.record_resting(Side.B, "ord-b", 10, 48)  # 25 committed
    snapshot = _make_snapshot(no_a=45, no_b=48)

    result = compute_rebalance_proposal(
        "EVT-1", ledger, pair, snapshot, "Test", _books_with_data()
    )
    assert result is not None
    assert result.rebalance is not None
    # gap = 40 - 25 = 15, minus 10 resting = effective 15
    assert result.rebalance.catchup_qty == 15
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_rebalance.py::TestComputeRebalanceProposal::test_catchup_full_gap_not_capped tests/test_rebalance.py::TestComputeRebalanceProposal::test_target_is_over_filled_not_max tests/test_rebalance.py::TestComputeRebalanceProposal::test_under_resting_reduces_effective_gap -v`
Expected: FAIL — old logic caps at unit_size, target uses max()

- [ ] **Step 3: Fix detection logic in `compute_rebalance_proposal`**

In `src/talos/rebalance.py`, change lines 97-109:

```python
    # Target = over_filled. Over-side resting is always cancelled (reduce
    # exposure first), then under-side catches up to match fills.
    target = over_filled
    target_over_resting = max(0, target - over_filled)  # always 0
    reduce_by = over_resting - target_over_resting

    # Step 2: catch-up on under-side (full gap, no unit cap)
    gap = target - under_committed
    effective_gap = max(0, gap - under_resting)
    catchup_qty = 0
    catchup_price = 0
    catchup_ticker: str | None = None
    if effective_gap > 0:
        catchup_qty = effective_gap  # full gap — no min(effective_gap, unit_size) cap
        catchup_ticker = under_ticker
```

- [ ] **Step 4: Update `test_catchup_capped_at_unit_size` to match new behavior**

The old test asserted `catchup_qty == 10` (capped). Rename it and update:

```python
def test_catchup_bridges_full_gap(self):
    """Catch-up quantity covers the full gap (no unit cap)."""
    pair = _make_pair()
    ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
    ledger.record_fill(Side.A, 50, 45)
    ledger.record_fill(Side.B, 20, 48)
    snapshot = _make_snapshot(no_a=45, no_b=48)

    result = compute_rebalance_proposal(
        "EVT-1", ledger, pair, snapshot, "Test", _books_with_data()
    )
    assert result is not None
    assert result.rebalance is not None
    assert result.rebalance.catchup_qty == 30  # full gap: 50 - 20
```

- [ ] **Step 5: Update `test_two_step_cancel_then_catchup` for new target logic**

The existing test has A=30f+10r, B=20f. Old target was `max(30,20)=30`, new target is `30` (over_filled). The catchup qty was 10, now the same (30-20=10). This test should still pass as-is, but verify.

- [ ] **Step 6: Update `test_partial_reduce_when_under_committed_exceeds_over_filled`**

Old: `target = max(30, 40) = 40`, `target_resting = 10`. New: `target = 30` (over_filled, since A is the over-side? Actually B has more committed). Let's re-examine: A=30f+20r=50 committed, B=40f=40 committed. A is over. `over_filled = 30`, `target = 30`. `target_over_resting = 0`. `reduce_by = 20`. Catchup for B: gap = 30 - 40 = negative → no catchup. But wait — B has MORE fills than A's fills (40 vs 30). Under the new logic, we'd cancel A's 20 resting (A → 30 committed), and then B already exceeds A, so the sides flip — B becomes over-extended with delta = 10. This case is fine: we cancel A's resting in this cycle, and next cycle B is the over-side with no resting and no catch-up needed (balanced or settled). Update test:

```python
def test_reduce_over_side_when_under_has_more_fills(self):
    """When under-side has more fills than over-filled, cancel all over resting."""
    pair = _make_pair()
    ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
    ledger.record_fill(Side.A, 30, 45)
    ledger.record_resting(Side.A, "ord-a", 20, 45)  # 50 committed
    ledger.record_fill(Side.B, 40, 48)  # 40 committed

    result = compute_rebalance_proposal(
        "EVT-1", ledger, pair, None, "Test", _books_with_data()
    )
    assert result is not None
    assert result.rebalance is not None
    # target = over_filled = 30, cancel all 20 resting
    assert result.rebalance.target_resting == 0
    assert result.rebalance.current_resting == 20
    # No catch-up: under (B) already has 40 fills > target 30
    assert result.rebalance.catchup_qty == 0
```

- [ ] **Step 7: Update `test_reduce_only_when_under_has_resting`**

A=40f+10r=50, B=20f+10r=30. A is over. `over_filled=40`, `target=40`. `target_over_resting=0`, `reduce_by=10`. Catchup: gap=40-30=10, B has 10 resting, so `effective_gap=0`. No catchup. Update:

```python
def test_reduce_only_when_under_has_resting(self):
    """If under-side resting already covers the gap, only reduce over-side."""
    pair = _make_pair()
    ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
    ledger.record_fill(Side.A, 40, 45)
    ledger.record_resting(Side.A, "ord-a", 10, 45)
    ledger.record_fill(Side.B, 20, 48)
    ledger.record_resting(Side.B, "ord-b", 10, 48)
    snapshot = _make_snapshot(no_a=45, no_b=48)

    result = compute_rebalance_proposal(
        "EVT-1", ledger, pair, snapshot, "Test", OrderBookManager()
    )
    assert result is not None
    assert result.rebalance is not None
    assert result.rebalance.order_id == "ord-a"
    assert result.rebalance.target_resting == 0  # cancel all A resting
    # Under-side B has 10 resting, gap = 40 - 30 = 10, effective_gap = 0
    assert result.rebalance.catchup_qty == 0
```

Wait — gap = 40 (target) - 30 (under_committed) = 10. effective_gap = max(0, 10 - 10) = 0. Yes, no catchup. But now B only has 30 committed vs A's 40 filled. B's 10 resting will fill to make B=30, but A has 40 fills. After B fills 10, B=30f, A=40f. Gap of 10 still. Next cycle catches it. This is correct — B's resting is working on it.

- [ ] **Step 8: Run all rebalance tests**

Run: `.venv/Scripts/python -m pytest tests/test_rebalance.py -v`
Expected: ALL PASS

- [ ] **Step 9: Commit**

```bash
git add src/talos/rebalance.py tests/test_rebalance.py
git commit -m "feat: fix rebalance detection — target=over_filled, full gap catchup"
```

---

### Task 3: Harden `execute_rebalance` — `get_all_orders` + recalculate qty

**Files:**
- Modify: `src/talos/rebalance.py:188-380`
- Test: `tests/test_rebalance.py`

- [ ] **Step 1: Write failing test for `get_all_orders` usage**

```python
# In tests/test_rebalance.py TestExecuteRebalance class:

@pytest.mark.asyncio
async def test_fresh_sync_uses_get_all_orders(self):
    """Fresh sync before catch-up uses get_all_orders (not truncated get_orders)."""
    scanner, adjuster, rest = _make_exec_context()
    ledger = adjuster.get_ledger("EVT-1")
    ledger.record_fill(Side.A, 30, 45)
    ledger.record_fill(Side.B, 20, 48)

    rest.get_all_orders = AsyncMock(
        return_value=[
            _make_order("TK-A", order_id="oa", fill_count=30, no_price=45, status="canceled"),
            _make_order("TK-B", order_id="ob", fill_count=20, no_price=48, status="canceled"),
        ]
    )
    rest.create_order = AsyncMock(return_value=_make_order("TK-B", order_id="new-b"))

    rebalance = ProposedRebalance(
        event_ticker="EVT-1", side="A",
        catchup_ticker="TK-B", catchup_price=48, catchup_qty=10,
    )
    await execute_rebalance(
        rebalance, rest_client=rest, adjuster=adjuster, scanner=scanner,
        notify=lambda msg, sev: None,
    )

    rest.get_all_orders.assert_called_once()
    # Old get_orders should NOT be called
    rest.get_orders.assert_not_called()
```

- [ ] **Step 2: Write failing test for recalculated qty**

```python
@pytest.mark.asyncio
async def test_catchup_qty_recalculated_from_fresh_sync(self):
    """Catch-up qty is recalculated from fresh ledger, not stale proposal."""
    scanner, adjuster, rest = _make_exec_context()

    # Proposal says catchup_qty=25 (stale data: A=40, B=15)
    # But fresh sync shows B caught up to 30 → real gap is 10
    rest.get_all_orders = AsyncMock(
        return_value=[
            _make_order("TK-A", order_id="oa", fill_count=40, no_price=45, status="canceled"),
            _make_order("TK-B", order_id="ob", fill_count=30, no_price=48, status="canceled"),
        ]
    )
    rest.create_order = AsyncMock(return_value=_make_order("TK-B", order_id="new-b"))

    rebalance = ProposedRebalance(
        event_ticker="EVT-1", side="A",
        catchup_ticker="TK-B", catchup_price=48, catchup_qty=25,  # stale
    )
    await execute_rebalance(
        rebalance, rest_client=rest, adjuster=adjuster, scanner=scanner,
        notify=lambda msg, sev: None,
    )

    # Should place 10 (recalculated), not 25 (stale)
    rest.create_order.assert_called_once()
    assert rest.create_order.call_args.kwargs["count"] == 10

@pytest.mark.asyncio
async def test_catchup_skipped_when_recalculated_qty_zero(self):
    """If fresh sync closes the gap entirely, skip catch-up."""
    scanner, adjuster, rest = _make_exec_context()

    rest.get_all_orders = AsyncMock(
        return_value=[
            _make_order("TK-A", order_id="oa", fill_count=30, no_price=45, status="canceled"),
            _make_order("TK-B", order_id="ob", fill_count=30, no_price=48, status="canceled"),
        ]
    )
    rest.create_order = AsyncMock()

    rebalance = ProposedRebalance(
        event_ticker="EVT-1", side="A",
        catchup_ticker="TK-B", catchup_price=48, catchup_qty=10,
    )
    notifications: list[tuple[str, str]] = []
    await execute_rebalance(
        rebalance, rest_client=rest, adjuster=adjuster, scanner=scanner,
        notify=lambda msg, sev: notifications.append((msg, sev)),
    )

    rest.create_order.assert_not_called()
    assert any("skipped" in msg.lower() or "balanced" in msg.lower() for msg, _ in notifications)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_rebalance.py::TestExecuteRebalance::test_fresh_sync_uses_get_all_orders tests/test_rebalance.py::TestExecuteRebalance::test_catchup_qty_recalculated_from_fresh_sync tests/test_rebalance.py::TestExecuteRebalance::test_catchup_skipped_when_recalculated_qty_zero -v`
Expected: FAIL

- [ ] **Step 4: Update `execute_rebalance` in `rebalance.py`**

Change step 2 (lines 276-379). Key changes:
1. Replace `rest_client.get_orders(limit=200)` with `rest_client.get_all_orders()`
2. After fresh sync, recalculate catchup qty from fresh ledger state
3. Use `catchup=True` in safety gate call

```python
    # Step 2: Catch-up bid on under-side
    if rebalance.catchup_ticker and rebalance.catchup_qty > 0:
        under_side = Side.A if rebalance.side == "B" else Side.B

        # Fresh sync from Kalshi before placing (P7/P21)
        pair = _find_pair(scanner, rebalance.event_ticker)
        if pair is None:
            notify("Catch-up BLOCKED: pair not found", "error")
            return

        try:
            orders = await rest_client.get_all_orders()
            ledger = adjuster.get_ledger(rebalance.event_ticker)
            ledger.sync_from_orders(
                orders, ticker_a=pair.ticker_a, ticker_b=pair.ticker_b
            )
        except Exception:
            logger.warning(
                "rebalance_fresh_sync_failed",
                event_ticker=rebalance.event_ticker,
                exc_info=True,
            )
            notify("Catch-up BLOCKED: fresh sync failed", "error")
            return

        # Re-check with fresh data — recalculate qty
        over_side = Side.A if rebalance.side == "A" else Side.B
        fresh_over_filled = ledger.filled_count(over_side)
        fresh_under_committed = ledger.total_committed(under_side)
        fresh_catchup_qty = max(0, fresh_over_filled - fresh_under_committed)
        if fresh_catchup_qty <= 0:
            notify(
                f"Catch-up skipped — fresh sync shows gap closed (balanced)",
                "information",
            )
            logger.info(
                "rebalance_catchup_skipped_after_sync",
                event_ticker=rebalance.event_ticker,
                fresh_over_filled=fresh_over_filled,
                fresh_under_committed=fresh_under_committed,
            )
            return

        # Safety gate — catchup=True bypasses P16 unit boundary, keeps P18
        ok, reason = ledger.is_placement_safe(
            under_side,
            fresh_catchup_qty,
            rebalance.catchup_price,
            rate=pair.fee_rate,
            catchup=True,
        )
        if not ok:
            notify(
                f"Catch-up BLOCKED ({under_side.value}): {reason}",
                "warning",
            )
            logger.warning(
                "rebalance_catchup_blocked",
                event_ticker=rebalance.event_ticker,
                side=under_side.value,
                reason=reason,
            )
            return

        catchup_group = await _create_order_group(
            rest_client,
            rebalance.event_ticker,
            under_side.value,
            fresh_catchup_qty,
        )
        try:
            await rest_client.create_order(
                ticker=rebalance.catchup_ticker,
                action="buy",
                side="no",
                no_price=rebalance.catchup_price,
                count=fresh_catchup_qty,
                order_group_id=catchup_group,
            )
            notify(
                f"Rebalance step 2: catch-up {rebalance.catchup_ticker}"
                f" {fresh_catchup_qty} @ {rebalance.catchup_price}c",
                "information",
            )
            logger.info(
                "rebalance_catchup_placed",
                event_ticker=rebalance.event_ticker,
                ticker=rebalance.catchup_ticker,
                qty=fresh_catchup_qty,
                price=rebalance.catchup_price,
            )
        except Exception as e:
            notify(
                f"Catch-up FAILED: {type(e).__name__}: {e}",
                "error",
            )
            logger.exception(
                "rebalance_catchup_error",
                event_ticker=rebalance.event_ticker,
                ticker=rebalance.catchup_ticker,
            )
```

- [ ] **Step 5: Update existing execution tests to use `get_all_orders` mock**

All existing tests that mock `rest.get_orders` must switch to `rest.get_all_orders`. Here are the specific tests and changes:

1. `TestExecuteRebalance::test_cancel_and_catchup` — change `rest.get_orders = AsyncMock(...)` to `rest.get_all_orders = AsyncMock(...)`. Assertion: `count=10` still correct (fresh: A=30f, B=20f, recalculated qty = 30-20 = 10).
2. `TestExecuteRebalance::test_catchup_blocked_by_safety` — change `rest.get_orders` to `rest.get_all_orders`. Assertion: `create_order.assert_not_called()` still holds (B has resting, safety gate blocks).
3. `TestFreshSyncBeforeCatchup::test_catchup_skipped_when_fresh_sync_resolves_imbalance` — change `rest.get_orders` to `rest.get_all_orders`. Fresh data: A=30f, B=25f+5r=30 committed. Recalculated qty = 30 - 30 = 0 → still skipped. Assertion holds.
4. `TestFreshSyncBeforeCatchup::test_catchup_blocked_when_fresh_sync_fails` — change `rest.get_orders` to `rest.get_all_orders`. Side effect is `RuntimeError` — behavior unchanged.
5. `TestFreshSyncBeforeCatchup::test_fresh_sync_confirms_imbalance_catchup_proceeds` — change `rest.get_orders` to `rest.get_all_orders`. Fresh data: A=30f, B=20f. Recalculated qty = 30 - 20 = 10. Assertion `count=10` still holds.

- [ ] **Step 6: Run all rebalance tests**

Run: `.venv/Scripts/python -m pytest tests/test_rebalance.py -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add src/talos/rebalance.py tests/test_rebalance.py
git commit -m "feat: harden execute_rebalance — get_all_orders + recalculate qty"
```

---

### Task 4: Make `check_imbalances()` async with auto-execution

**Files:**
- Modify: `src/talos/engine.py:1281-1311` and `src/talos/engine.py:649`
- Test: `tests/test_engine.py`

- [ ] **Step 1: Write failing test for auto-execution**

```python
# In tests/test_engine.py:

class TestAutoRebalance:
    @pytest.mark.asyncio
    async def test_check_imbalances_auto_executes(self):
        """check_imbalances detects imbalance and auto-executes catch-up."""
        engine, rest = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 40, 45)
        ledger.record_fill(Side.B, 15, 48)

        # Scanner snapshot needed for price
        engine.scanner._all_snapshots["EVT-1"] = Opportunity(
            event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B",
            no_a=45, no_b=48, qty_a=100, qty_b=100,
            raw_edge=7, fee_edge=0.0, tradeable_qty=100,
            timestamp="2026-03-16T00:00:00Z",
        )

        # Mock for fresh sync in execute_rebalance
        rest.get_all_orders = AsyncMock(return_value=[
            _make_order("TK-A", order_id="oa", fill_count=40, no_price=45, status="canceled"),
            _make_order("TK-B", order_id="ob", fill_count=15, no_price=48, status="canceled"),
        ])
        rest.create_order = AsyncMock(return_value=_make_order("TK-B", order_id="new-b"))
        rest.create_order_group = AsyncMock(return_value="grp-test")

        notifications: list[tuple[str, str]] = []
        engine.on_notification = lambda msg, sev: notifications.append((msg, sev))

        await engine.check_imbalances()

        # Should have auto-placed catch-up, NOT added to proposal queue
        rest.create_order.assert_called_once()
        assert rest.create_order.call_args.kwargs["ticker"] == "TK-B"
        assert rest.create_order.call_args.kwargs["count"] == 25  # full gap
        assert len(engine.proposal_queue.pending()) == 0  # no queued proposals

    @pytest.mark.asyncio
    async def test_check_imbalances_skips_exit_only(self):
        """Events in exit-only mode are skipped by check_imbalances."""
        engine, rest = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 40, 45)
        ledger.record_fill(Side.B, 15, 48)

        engine._exit_only_events.add("EVT-1")

        rest.create_order = AsyncMock()

        await engine.check_imbalances()

        rest.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_imbalances_double_fire_guard(self):
        """Same event is not rebalanced twice in one check_imbalances call."""
        engine, rest = _engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, 40, 45)
        ledger.record_fill(Side.B, 15, 48)

        engine.scanner._all_snapshots["EVT-1"] = Opportunity(
            event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B",
            no_a=45, no_b=48, qty_a=100, qty_b=100,
            raw_edge=7, fee_edge=0.0, tradeable_qty=100,
            timestamp="2026-03-16T00:00:00Z",
        )

        rest.get_all_orders = AsyncMock(return_value=[
            _make_order("TK-A", order_id="oa", fill_count=40, no_price=45, status="canceled"),
            _make_order("TK-B", order_id="ob", fill_count=15, no_price=48, status="canceled"),
        ])
        rest.create_order = AsyncMock(return_value=_make_order("TK-B", order_id="new-b"))
        rest.create_order_group = AsyncMock(return_value="grp-test")

        await engine.check_imbalances()

        # Should execute exactly once, not duplicate
        assert rest.create_order.call_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_engine.py::TestAutoRebalance -v`
Expected: FAIL — `check_imbalances()` is not async, does not auto-execute

- [ ] **Step 3: Rewrite `check_imbalances()` as async with auto-execution**

In `src/talos/engine.py`, replace `check_imbalances()` (lines 1281-1311):

```python
    async def check_imbalances(self) -> None:
        """Detect and auto-execute rebalance catch-ups (P16).

        Delegates to compute_rebalance_proposal() for pure detection,
        then auto-executes via execute_rebalance() without operator approval.
        Rebalance is risk-reducing (closing exposure), so it bypasses the
        ProposalQueue (P2 progression: supervised → autonomous for catch-up).

        NOTE: The old pending_keys/ProposalQueue check is intentionally removed.
        Auto-execution replaces manual proposal approval — the executed_this_cycle
        set prevents double-firing within a single call.
        """
        executed_this_cycle: set[str] = set()
        for pair in self._scanner.pairs:
            if pair.event_ticker in executed_this_cycle:
                continue

            # Exit-only events have their own cancellation flow
            if self.is_exit_only(pair.event_ticker):
                continue

            try:
                ledger = self._adjuster.get_ledger(pair.event_ticker)
            except KeyError:
                continue

            snapshot = self._scanner.all_snapshots.get(pair.event_ticker)
            proposal = compute_rebalance_proposal(
                pair.event_ticker,
                ledger,
                pair,
                snapshot,
                self._display_name(pair.event_ticker),
                self._feed.book_manager,
            )
            if proposal is None or proposal.rebalance is None:
                continue

            # Auto-execute — no ProposalQueue
            await _execute_rebalance(
                proposal.rebalance,
                rest_client=self._rest,
                adjuster=self._adjuster,
                scanner=self._scanner,
                notify=self._notify,
            )
            executed_this_cycle.add(pair.event_ticker)
```

- [ ] **Step 4: Update `refresh_account()` to await `check_imbalances()`**

In `src/talos/engine.py`, change line 649 from:
```python
            self.check_imbalances()
```
to:
```python
            await self.check_imbalances()
```

- [ ] **Step 5: Run all engine tests**

Run: `.venv/Scripts/python -m pytest tests/test_engine.py -v`
Expected: ALL PASS

- [ ] **Step 6: Run full test suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add src/talos/engine.py tests/test_engine.py
git commit -m "feat: auto-execute rebalance catch-ups — bypass ProposalQueue"
```

---

### Task 5: Add top-up logic for mid-unit gaps

**Files:**
- Modify: `src/talos/engine.py` (inside `check_imbalances`)
- Modify: `src/talos/rebalance.py` (new pure function)
- Test: `tests/test_rebalance.py`
- Test: `tests/test_engine.py`

- [ ] **Step 1: Write failing tests for top-up detection**

```python
# In tests/test_rebalance.py, new class:

class TestTopUpDetection:
    def test_both_sides_need_topup(self):
        """Both sides mid-unit with no resting → both get top-up proposals."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, 15, 45)
        ledger.record_fill(Side.B, 12, 48)
        snapshot = _make_snapshot(no_a=45, no_b=48)

        result = compute_topup_needs(ledger, pair, snapshot)
        assert result == {Side.A: (5, 45), Side.B: (8, 48)}

    def test_one_side_has_resting_skipped(self):
        """Side with resting orders doesn't need top-up."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, 15, 45)
        ledger.record_resting(Side.A, "ord-a", 5, 45)
        ledger.record_fill(Side.B, 15, 48)
        snapshot = _make_snapshot(no_a=45, no_b=48)

        result = compute_topup_needs(ledger, pair, snapshot)
        # A has resting → skip A. B needs 5.
        assert Side.A not in result
        assert result == {Side.B: (5, 48)}

    def test_complete_unit_no_topup(self):
        """Side at unit boundary (20 filled) doesn't need top-up."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, 20, 45)
        ledger.record_fill(Side.B, 20, 48)
        snapshot = _make_snapshot(no_a=45, no_b=48)

        result = compute_topup_needs(ledger, pair, snapshot)
        assert result == {}

    def test_no_snapshot_no_topup(self):
        """No scanner snapshot → no top-up (can't determine price)."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, 15, 45)
        ledger.record_fill(Side.B, 12, 48)

        result = compute_topup_needs(ledger, pair, None)
        assert result == {}

    def test_imbalanced_committed_no_topup(self):
        """Unequal committed counts → catch-up handles it, not top-up."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        ledger.record_fill(Side.A, 30, 45)
        ledger.record_fill(Side.B, 15, 48)
        snapshot = _make_snapshot(no_a=45, no_b=48)

        result = compute_topup_needs(ledger, pair, snapshot)
        assert result == {}  # catch-up handles this, not top-up

    def test_zero_fills_no_topup(self):
        """No fills at all → no top-up needed."""
        pair = _make_pair()
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=20)
        snapshot = _make_snapshot(no_a=45, no_b=48)

        result = compute_topup_needs(ledger, pair, snapshot)
        assert result == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_rebalance.py::TestTopUpDetection -v`
Expected: FAIL — `compute_topup_needs` doesn't exist

- [ ] **Step 3: Implement `compute_topup_needs` pure function**

Add to `src/talos/rebalance.py`:

```python
def compute_topup_needs(
    ledger: PositionLedger,
    pair: ArbPair,
    snapshot: Opportunity | None,
) -> dict[Side, tuple[int, int]]:
    """Compute top-up needs for mid-unit sides with no resting bids.

    Returns dict mapping Side → (qty, price) for each side needing top-up.
    Only fires when committed counts are equal (catch-up handles imbalances).
    Pure function — no I/O.
    """
    if snapshot is None:
        return {}

    committed_a = ledger.total_committed(Side.A)
    committed_b = ledger.total_committed(Side.B)

    # Only fire when balanced — catch-up handles imbalances
    if committed_a != committed_b:
        return {}

    needs: dict[Side, tuple[int, int]] = {}
    for side in (Side.A, Side.B):
        filled = ledger.filled_count(side)
        resting = ledger.resting_count(side)

        if filled == 0:
            continue  # no position yet
        if resting > 0:
            continue  # already has bids out

        filled_in_unit = filled % ledger.unit_size
        if filled_in_unit == 0:
            continue  # at unit boundary

        qty = ledger.unit_size - filled_in_unit
        price = snapshot.no_a if side == Side.A else snapshot.no_b
        if price <= 0:
            continue
        needs[side] = (qty, price)

    return needs
```

- [ ] **Step 4: Run top-up detection tests**

Run: `.venv/Scripts/python -m pytest tests/test_rebalance.py::TestTopUpDetection -v`
Expected: ALL PASS

- [ ] **Step 5: Write test for top-up execution in engine**

```python
# In tests/test_engine.py TestAutoRebalance class:

@pytest.mark.asyncio
async def test_topup_places_orders_for_both_sides(self):
    """Top-up places orders on both sides when mid-unit with no resting."""
    engine, rest = _engine_with_pair()
    ledger = engine.adjuster.get_ledger("EVT-1")
    ledger.record_fill(Side.A, 15, 45)
    ledger.record_fill(Side.B, 15, 48)

    engine.scanner._all_snapshots["EVT-1"] = Opportunity(
        event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B",
        no_a=45, no_b=48, qty_a=100, qty_b=100,
        raw_edge=7, fee_edge=0.0, tradeable_qty=100,
        timestamp="2026-03-16T00:00:00Z",
    )

    rest.create_order = AsyncMock(return_value=_make_order("TK-A", order_id="new"))
    rest.create_order_group = AsyncMock(return_value="grp-test")

    await engine.check_imbalances()

    # Should place top-up on both sides (5 each to reach 20)
    assert rest.create_order.call_count == 2
```

- [ ] **Step 6: Add top-up execution to `check_imbalances()` in engine.py**

After the catch-up logic in `check_imbalances()`, add:

```python
            # Top-up: if no catch-up was needed, check for mid-unit gaps
            if proposal is None or proposal.rebalance is None:
                topup_needs = compute_topup_needs(ledger, pair, snapshot)
                if self.is_exit_only(pair.event_ticker):
                    topup_needs = {}
                for side, (qty, price) in topup_needs.items():
                    ok, reason = ledger.is_placement_safe(
                        side, qty, price, rate=pair.fee_rate
                    )
                    if not ok:
                        self._notify(
                            f"Top-up BLOCKED ({side.value}): {reason}",
                            "warning",
                        )
                        continue
                    ticker = pair.ticker_a if side == Side.A else pair.ticker_b
                    group = await _create_order_group(
                        self._rest, pair.event_ticker, side.value, qty
                    )
                    try:
                        await self._rest.create_order(
                            ticker=ticker,
                            action="buy",
                            side="no",
                            no_price=price,
                            count=qty,
                            order_group_id=group,
                        )
                        self._notify(
                            f"Top-up {pair.event_ticker} {side.value}:"
                            f" {qty} @ {price}c",
                            "information",
                        )
                        logger.info(
                            "topup_placed",
                            event_ticker=pair.event_ticker,
                            side=side.value,
                            qty=qty,
                            price=price,
                        )
                    except Exception as e:
                        self._notify(
                            f"Top-up FAILED ({side.value}):"
                            f" {type(e).__name__}: {e}",
                            "error",
                        )
                        logger.exception(
                            "topup_error",
                            event_ticker=pair.event_ticker,
                            side=side.value,
                        )
```

Add the imports at the top of engine.py:
```python
from talos.rebalance import compute_topup_needs
from talos.rebalance import _create_order_group
```

- [ ] **Step 7: Run all tests**

Run: `.venv/Scripts/python -m pytest -v`
Expected: ALL PASS

- [ ] **Step 8: Commit**

```bash
git add src/talos/rebalance.py src/talos/engine.py tests/test_rebalance.py tests/test_engine.py
git commit -m "feat: add top-up logic for mid-unit gaps with no resting bids"
```

---

### Task 6: Final validation — lint, types, full test suite

**Files:** All modified files

- [ ] **Step 1: Run linter**

Run: `.venv/Scripts/python -m ruff check src/ tests/`
Fix any issues.

- [ ] **Step 2: Run formatter**

Run: `.venv/Scripts/python -m ruff format src/ tests/`

- [ ] **Step 3: Run type checker**

Run: `.venv/Scripts/python -m pyright`
Expected: No new errors (existing `reportMissingImports` on `talos.*` is a known false positive).

- [ ] **Step 4: Run full test suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: ALL PASS

- [ ] **Step 5: Commit any lint/format fixes**

```bash
git add -u
git commit -m "style: lint and format fixes for auto-catchup"
```
