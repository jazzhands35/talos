# Exit-Only Mode Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Per-event exit-only mode that stops new bidding and gracefully winds down positions before game start. Auto-triggers on game status, manually toggleable with `e` key.

**Architecture:** Seven modules change: `automation_config.py` gains `exit_only_minutes`, `engine.py` gains exit-only state + check/enforce/toggle methods, `opportunity_proposer.py` and `bid_adjuster.py` gain exit-only gates, `ui/app.py` gains the `e` keybinding, `ui/widgets.py` gains EXIT status display variants, and `engine.py:place_bids` gains a hard block. TDD throughout — every behavior change starts with a failing test.

**Tech Stack:** Python 3.12+, pytest, pytest-asyncio, structlog

**Spec:** `docs/plans/2026-03-15-exit-only-design.md`

---

### Task 1: Add `exit_only_minutes` to AutomationConfig

**Files:**
- Modify: `src/talos/automation_config.py`
- Test: `tests/test_exit_only.py`

- [x] **Step 1: Write test for default and custom config values**

```python
# tests/test_exit_only.py

class TestExitOnlyConfig:
    def test_default_exit_only_minutes(self):
        config = AutomationConfig()
        assert config.exit_only_minutes == 30.0

    def test_custom_exit_only_minutes(self):
        config = AutomationConfig(exit_only_minutes=15.0)
        assert config.exit_only_minutes == 15.0
```

- [x] **Step 2: Add field to AutomationConfig**

```python
# In src/talos/automation_config.py, add to the Pydantic model:
exit_only_minutes: float = 30.0
```

- [x] **Step 3: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_exit_only.py::TestExitOnlyConfig -v`
Expected: ALL PASS

---

### Task 2: Engine state — exit-only set, toggle, query

**Files:**
- Modify: `src/talos/engine.py`
- Test: `tests/test_exit_only.py`

- [x] **Step 1: Add `_exit_only_events` set and `is_exit_only()` method**

```python
# In TradingEngine.__init__:
self._exit_only_events: set[str] = set()

# New method:
def is_exit_only(self, event_ticker: str) -> bool:
    return event_ticker in self._exit_only_events
```

- [x] **Step 2: Add `toggle_exit_only()` method**

```python
def toggle_exit_only(self, event_ticker: str) -> bool:
    """Toggle exit-only mode for an event. Returns new state."""
    if event_ticker in self._exit_only_events:
        self._exit_only_events.discard(event_ticker)
        name = self._display_name(event_ticker)
        self._notify(f"Exit-only OFF: {name}")
        logger.info("exit_only_off", event_ticker=event_ticker)
        return False
    else:
        self._exit_only_events.add(event_ticker)
        name = self._display_name(event_ticker)
        self._notify(f"Exit-only ON: {name}", "warning")
        logger.info("exit_only_on", event_ticker=event_ticker)
        self._enforce_exit_only_sync(event_ticker)
        return True
```

- [x] **Step 3: Add `_enforce_exit_only_sync()` — expire pending proposals**

```python
def _enforce_exit_only_sync(self, event_ticker: str) -> None:
    """Synchronous part of exit-only enforcement — expire proposals."""
    for proposal in list(self._proposal_queue.pending()):
        if proposal.key.event_ticker == event_ticker and proposal.kind == "bid":
            self._proposal_queue.reject(proposal.key)
```

- [x] **Step 4: Clean up `remove_game()` to discard exit-only state**

```python
async def remove_game(self, event_ticker: str) -> None:
    self._exit_only_events.discard(event_ticker)  # add this line
    # ... rest of existing remove_game
```

---

### Task 3: OpportunityProposer gate — block new bids

**Files:**
- Modify: `src/talos/opportunity_proposer.py`
- Test: `tests/test_exit_only.py`

- [x] **Step 1: Write failing tests for proposer gate**

```python
class TestProposerExitOnlyGate:
    def test_exit_only_blocks_new_bids(self):
        config = AutomationConfig(edge_threshold_cents=1.0, stability_seconds=0)
        proposer = OpportunityProposer(config)
        pair = _pair()
        opp = _opportunity(edge=5.0)
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)

        result = proposer.evaluate(pair, opp, ledger, pending_keys=set(), exit_only=True)
        assert result is None

    def test_normal_mode_allows_bids(self):
        config = AutomationConfig(edge_threshold_cents=1.0, stability_seconds=0)
        proposer = OpportunityProposer(config)
        pair = _pair()
        opp = _opportunity(edge=5.0)
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)

        result = proposer.evaluate(pair, opp, ledger, pending_keys=set(), exit_only=False)
        assert result is not None
        assert result.kind == "bid"
```

- [x] **Step 2: Add `exit_only` parameter to `OpportunityProposer.evaluate()`**

Early return at the top of evaluate():
```python
def evaluate(self, pair, opp, ledger, pending_keys, *, display_name="", exit_only=False):
    if exit_only:
        return None
    # ... rest of evaluate
```

- [x] **Step 3: Wire in engine — pass `exit_only` to proposer**

In `engine.py:evaluate_opportunities()`:
```python
proposal = self._proposer.evaluate(
    pair, snapshot, ledger, pending_keys,
    display_name=self._display_name(pair.event_ticker),
    exit_only=self.is_exit_only(pair.event_ticker),
)
```

- [x] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_exit_only.py::TestProposerExitOnlyGate -v`
Expected: ALL PASS

---

### Task 4: BidAdjuster gate — block ahead-side adjustments

**Files:**
- Modify: `src/talos/bid_adjuster.py`
- Test: `tests/test_exit_only.py`

- [x] **Step 1: Write failing tests for adjuster gate**

```python
class TestAdjusterExitOnlyGate:
    def test_balanced_blocks_all_adjustments(self):
        """When balanced (filled_a == filled_b), exit-only blocks all adjustments."""
        pair = _pair()
        adjuster = self._make_adjuster(pair)
        ledger = adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=5, price=48)
        ledger.record_fill(Side.B, count=5, price=49)
        ledger.record_resting(Side.A, "ord-1", count=5, price=48)

        result = adjuster.evaluate_jump("EVT-1-A", at_top=False, exit_only=True)
        assert result is None

    def test_imbalanced_blocks_ahead_side(self):
        """When A is ahead (more fills), exit-only blocks A adjustments."""
        pair = _pair()
        adjuster = self._make_adjuster(pair)
        ledger = adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=8, price=48)
        ledger.record_fill(Side.B, count=3, price=49)
        ledger.record_resting(Side.A, "ord-a", count=2, price=48)

        result = adjuster.evaluate_jump("EVT-1-A", at_top=False, exit_only=True)
        assert result is None
```

- [x] **Step 2: Add `exit_only` parameter to `evaluate_jump()`**

```python
def evaluate_jump(self, ticker: str, at_top: bool, *, exit_only: bool = False):
    # ... resolve pair, ledger
    if exit_only:
        filled_a = ledger.filled_count(Side.A)
        filled_b = ledger.filled_count(Side.B)
        if filled_a == filled_b:
            return None  # balanced — block all adjustments
        ahead = Side.A if filled_a > filled_b else Side.B
        if side == ahead:
            return None  # block ahead-side adjustment
    # ... rest of evaluate_jump
```

- [x] **Step 3: Wire in engine — resolve event, pass exit_only**

In `engine.py:_generate_jump_proposal()`:
```python
evt_ticker = self._adjuster.resolve_event(ticker)
exit_only = self.is_exit_only(evt_ticker) if evt_ticker else False
proposal = self._adjuster.evaluate_jump(ticker, at_top, exit_only=exit_only)
```

- [x] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_exit_only.py::TestAdjusterExitOnlyGate -v`
Expected: ALL PASS

---

### Task 5: Async enforcement — cancel resting orders

**Files:**
- Modify: `src/talos/engine.py`
- Test: `tests/test_exit_only.py`

- [x] **Step 1: Write enforcement logic tests**

```python
class TestExitOnlyEnforcement:
    def test_balanced_should_cancel_both_sides(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=5, price=48)
        ledger.record_fill(Side.B, count=5, price=49)
        ledger.record_resting(Side.A, "ord-a", count=5, price=48)
        ledger.record_resting(Side.B, "ord-b", count=5, price=49)
        assert ledger.filled_count(Side.A) == ledger.filled_count(Side.B)
        assert ledger.resting_order_id(Side.A) is not None
        assert ledger.resting_order_id(Side.B) is not None

    def test_imbalanced_identifies_ahead_side(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=8, price=48)
        ledger.record_fill(Side.B, count=3, price=49)
        ahead = Side.A if ledger.filled_count(Side.A) > ledger.filled_count(Side.B) else Side.B
        assert ahead is Side.A

    def test_zero_zero_is_balanced(self):
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        assert ledger.filled_count(Side.A) == 0
        assert ledger.filled_count(Side.B) == 0
```

- [x] **Step 2: Implement `_enforce_exit_only()` async method**

```python
async def _enforce_exit_only(self, event_ticker: str) -> None:
    """Cancel resting orders per exit-only rules.
    Balanced → cancel all resting on both sides.
    Imbalanced → cancel resting on the ahead side only.
    """
    try:
        ledger = self._adjuster.get_ledger(event_ticker)
    except KeyError:
        return

    filled_a = ledger.filled_count(Side.A)
    filled_b = ledger.filled_count(Side.B)

    if filled_a == filled_b:
        for side in (Side.A, Side.B):
            order_id = ledger.resting_order_id(side)
            if order_id is not None:
                try:
                    await self._rest.cancel_order(order_id)
                except Exception:
                    logger.warning("exit_only_cancel_failed", exc_info=True)
    else:
        ahead = Side.A if filled_a > filled_b else Side.B
        order_id = ledger.resting_order_id(ahead)
        if order_id is not None:
            try:
                await self._rest.cancel_order(order_id)
            except Exception:
                logger.warning("exit_only_cancel_failed", exc_info=True)

    await self._verify_after_action(event_ticker)
```

- [x] **Step 3: Implement `_enforce_all_exit_only()` — loop over all flagged events**

```python
async def _enforce_all_exit_only(self) -> None:
    """Enforce exit-only rules on all flagged events. Called from refresh cycle."""
    for event_ticker in list(self._exit_only_events):
        try:
            ledger = self._adjuster.get_ledger(event_ticker)
        except KeyError:
            self._exit_only_events.discard(event_ticker)
            continue

        filled_a = ledger.filled_count(Side.A)
        filled_b = ledger.filled_count(Side.B)
        resting_a = ledger.resting_count(Side.A)
        resting_b = ledger.resting_count(Side.B)

        if filled_a == filled_b and resting_a == 0 and resting_b == 0:
            # Balanced and no resting → auto-remove game
            self._exit_only_events.discard(event_ticker)
            await self.remove_game(event_ticker)
            continue

        await self._enforce_exit_only(event_ticker)
```

- [x] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_exit_only.py::TestExitOnlyEnforcement -v`
Expected: ALL PASS

---

### Task 6: Async enforcement — `decrease_order` for imbalanced behind side

**Files:**
- Modify: `src/talos/engine.py`
- Test: `tests/test_exit_only.py`

- [x] **Step 1: Write async enforcement tests with mock REST**

```python
class TestExitOnlyEnforcementAsync:
    @pytest.mark.asyncio
    async def test_imbalanced_cancels_ahead_and_reduces_behind(self):
        """Exit-only: cancel ahead resting, reduce behind resting to match."""
        engine, rest = self._make_engine_with_pair()
        ledger = engine.adjuster.get_ledger("EVT-1")
        ledger.record_fill(Side.A, count=5, price=48)
        ledger.record_resting(Side.A, "ord-a", count=15, price=48)
        ledger.record_fill(Side.B, count=1, price=49)
        ledger.record_resting(Side.B, "ord-b", count=19, price=49)

        rest.cancel_order = AsyncMock()
        rest.get_order = AsyncMock(return_value=type("O", (), {"remaining_count": 19})())
        rest.decrease_order = AsyncMock()
        rest.get_all_orders = AsyncMock(return_value=[])

        await engine._enforce_exit_only("EVT-1")

        rest.cancel_order.assert_called_once_with("ord-a")
        rest.decrease_order.assert_called_once_with("ord-b", reduce_to=4)

    @pytest.mark.asyncio
    async def test_imbalanced_behind_no_resting_no_decrease(self):
        """If behind side has no resting, only cancel ahead side."""
        # ...cancel ahead, decrease_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_imbalanced_behind_resting_already_at_target(self):
        """If behind resting is already <= target, no decrease needed."""
        # ...target = 5 - 2 = 3, behind has exactly 3 → no decrease
```

- [x] **Step 2: Enhance `_enforce_exit_only()` with behind-side reduction**

When imbalanced, after cancelling the ahead side:
- Calculate `target_behind_resting = ahead_filled - behind_filled`
- If behind side has resting and `resting_count > target_behind_resting`:
  - Call `rest_client.decrease_order(order_id, reduce_to=target_behind_resting)`
  - This keeps only enough resting to catch up to delta neutral

- [x] **Step 3: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_exit_only.py::TestExitOnlyEnforcementAsync -v`
Expected: ALL PASS

---

### Task 7: Auto-trigger — `_check_exit_only()` timing logic

**Files:**
- Modify: `src/talos/engine.py`
- Test: `tests/test_exit_only.py`

- [x] **Step 1: Write auto-trigger timing tests**

```python
class TestExitOnlyAutoTrigger:
    def test_live_game_triggers(self):
        gs = GameStatus(state="live", scheduled_start=datetime.now(UTC))
        assert gs.state == "live"

    def test_approaching_game_triggers(self):
        now = datetime.now(UTC)
        start = now + timedelta(minutes=20)
        gs = GameStatus(state="pre", scheduled_start=start)
        minutes_to_start = (gs.scheduled_start - now).total_seconds() / 60
        assert minutes_to_start < 30

    def test_far_game_doesnt_trigger(self):
        now = datetime.now(UTC)
        start = now + timedelta(hours=3)
        gs = GameStatus(state="pre", scheduled_start=start)
        minutes_to_start = (gs.scheduled_start - now).total_seconds() / 60
        assert minutes_to_start > 30

    def test_unknown_state_doesnt_trigger(self):
        gs = GameStatus(state="unknown")
        assert gs.state == "unknown"
```

- [x] **Step 2: Implement `_check_exit_only()` method**

```python
def _check_exit_only(self) -> None:
    """Auto-trigger exit-only based on game status."""
    exit_minutes = self._auto_config.exit_only_minutes
    now = datetime.now(UTC)

    for pair in self._scanner.pairs:
        event_ticker = pair.event_ticker
        if event_ticker in self._exit_only_events:
            continue

        gs = self._game_status_resolver.get(event_ticker)
        if gs is None:
            continue

        if gs.state == "live":
            self._exit_only_events.add(event_ticker)
            self._enforce_exit_only_sync(event_ticker)
            self._notify(f"Exit-only AUTO: {name} (live)", "warning")
        elif gs.state == "post":
            # FINAL games enter exit-only pipeline for auto-remove
            self._exit_only_events.add(event_ticker)
            self._enforce_exit_only_sync(event_ticker)
            self._notify(f"Exit-only AUTO: {name} (final)", "warning")
        elif (
            gs.state == "pre"
            and gs.scheduled_start is not None
            and (gs.scheduled_start - now).total_seconds() < exit_minutes * 60
        ):
            self._exit_only_events.add(event_ticker)
            self._enforce_exit_only_sync(event_ticker)
```

- [x] **Step 3: Wire into refresh cycle**

In `engine.py:refresh_account()`, after `evaluate_opportunities()`:
```python
self._check_exit_only()
await self._enforce_all_exit_only()
```

- [x] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_exit_only.py::TestExitOnlyAutoTrigger -v`
Expected: ALL PASS

---

### Task 8: place_bids hard block — reject orders on exit-only events

**Files:**
- Modify: `src/talos/engine.py`

- [x] **Step 1: Add exit-only block at top of `place_bids()`**

```python
async def place_bids(self, bid):
    evt_for_bid = self._adjuster.resolve_event(bid.ticker_a)
    if evt_for_bid and self.is_exit_only(evt_for_bid):
        label = self._display_name(evt_for_bid)
        self._notify(f"Bid BLOCKED {label}: exit-only mode (press E to disable)", "error")
        logger.error("bid_blocked_exit_only", event_ticker=evt_for_bid)
        return
    # ... rest of place_bids
```

This is the last-resort safety net — even if a proposal somehow bypasses the proposer/adjuster gates, the actual placement is blocked.

---

### Task 9: UI wiring — `e` keybinding and status display

**Files:**
- Modify: `src/talos/ui/app.py`
- Modify: `src/talos/ui/widgets.py`
- Test: `tests/test_exit_only.py`

- [x] **Step 1: Write status display tests**

```python
class TestExitOnlyStatusDisplay:
    def test_fmt_status_exit(self):
        from talos.ui.widgets import _fmt_status
        result = _fmt_status("EXIT")
        assert "EXIT" in str(result)

    def test_fmt_status_exit_behind(self):
        from talos.ui.widgets import _fmt_status
        result = _fmt_status("EXIT -5 B")
        assert "EXIT -5 B" in str(result)

    def test_fmt_status_exiting(self):
        from talos.ui.widgets import _fmt_status
        result = _fmt_status("EXITING")
        assert "EXITING" in str(result)
```

- [x] **Step 2: Add EXIT variants to `_fmt_status` in widgets.py**

| State | Display | Color |
|-------|---------|-------|
| Balanced, done | `EXIT` | dim |
| Imbalanced, catching up | `EXIT -N S` | warning |
| Just activated | `EXITING` | warning |

- [x] **Step 3: Add `_compute_event_status()` EXIT logic in engine.py**

```python
if self.is_exit_only(event_ticker):
    if filled_a == filled_b and resting_a == 0 and resting_b == 0:
        return "EXIT"
    if resting_a > 0 or resting_b > 0:
        if filled_a != filled_b:
            diff = abs(filled_a - filled_b)
            behind = "B" if filled_a > filled_b else "A"
            return f"EXIT -{diff} {behind}"
        return "EXITING"
    if filled_a != filled_b:
        diff = abs(filled_a - filled_b)
        behind = "B" if filled_a > filled_b else "A"
        return f"EXIT -{diff} {behind}"
    return "EXIT"
```

- [x] **Step 4: Add `e` keybinding to TalosApp**

```python
# In BINDINGS:
Binding("e", "toggle_exit_only", "Exit-only"),

# Handler:
def action_toggle_exit_only(self) -> None:
    """Toggle exit-only mode for the highlighted event."""
    table = self.query_one(OpportunitiesTable)
    event_ticker = table.highlighted_event_ticker
    if event_ticker and self._engine:
        self._engine.toggle_exit_only(event_ticker)
```

- [x] **Step 5: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_exit_only.py -v`
Expected: ALL PASS

---

### Task 10: Final validation — lint, types, full test suite

**Files:** All modified files

- [x] **Step 1: Run linter**

Run: `.venv/Scripts/python -m ruff check src/ tests/`

- [x] **Step 2: Run formatter**

Run: `.venv/Scripts/python -m ruff format src/ tests/`

- [x] **Step 3: Run type checker**

Run: `.venv/Scripts/python -m pyright`
Expected: No new errors (existing `reportMissingImports` on `talos.*` is a known false positive).

- [x] **Step 4: Run full test suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: ALL PASS

- [x] **Step 5: Commit**

```bash
git add src/talos/automation_config.py src/talos/bid_adjuster.py src/talos/engine.py \
  src/talos/opportunity_proposer.py src/talos/ui/app.py src/talos/ui/widgets.py \
  tests/test_exit_only.py
git commit -m "feat: add per-event exit-only mode for graceful pre-game wind-down"
```

---

## Summary of Changes

| File | Changes |
|------|---------|
| `src/talos/automation_config.py` | `exit_only_minutes: float = 30.0` |
| `src/talos/engine.py` | `_exit_only_events` set, `is_exit_only()`, `toggle_exit_only()`, `_enforce_exit_only()`, `_enforce_exit_only_sync()`, `_enforce_all_exit_only()`, `_check_exit_only()`, EXIT status logic, `place_bids` hard block |
| `src/talos/opportunity_proposer.py` | `exit_only` param on `evaluate()` — early return None |
| `src/talos/bid_adjuster.py` | `exit_only` param on `evaluate_jump()` — block balanced/ahead |
| `src/talos/ui/app.py` | `e` keybinding → `action_toggle_exit_only()` |
| `src/talos/ui/widgets.py` | `_fmt_status()` EXIT/EXITING rendering |
| `tests/test_exit_only.py` | 370+ lines: config, proposer gate, adjuster gate, enforcement logic, auto-trigger timing, status display, async enforcement with decrease_order |

**Commit:** `7d0cfdc feat: add per-event exit-only mode for graceful pre-game wind-down`
