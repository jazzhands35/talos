# DRIP/BLIP Redesign — Insertion-Strategy-Only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor DRIP from a parallel pipeline that owns DRIP events into a sizing parameter consumed by the standard Talos pipeline, restoring jump-following / rebalancing / opportunity-proposal / manual-bid behavior on DRIP events while preserving BLIP overlay and the resting-cap discipline.

**Architecture:** Introduce a single `per_side_max_ahead(ledger, side, drip_config)` helper. Route every "max resting per side" call site (rebalance, top-up, post-cancel safety, reconcile overcommit) through it. Delete the seven `is_drip(...)` early-return gates that block the standard pipeline. Slim `_drive_drip` to a BLIP-only loop reading the standard ledger; replace `DripController` (state-tracking class) with a free function `evaluate_blip(...)`. Mark events dirty on toggle so the next rebalance cycle snaps to the new cap automatically.

**Tech Stack:** Python 3.12, pytest, pytest-asyncio, ruff, pyright. No new dependencies.

**Spec:** [docs/superpowers/specs/2026-04-28-drip-redesign-design.md](../specs/2026-04-28-drip-redesign-design.md)

**Naming corrections vs. spec:**
- The spec calls the rebalance entry point `compute_unit_overcommit_proposal`; the actual function is `compute_overcommit_reduction` (rebalance.py:236). All tasks below use the real name.
- The spec mentions a "post-cancel safety check" at `bid_adjuster.py:762-767`; the function is `_check_post_cancel_safety`.

---

## File Structure

**New:**
- `src/talos/strategy.py` — strategy seam. Houses `per_side_max_ahead(ledger, side, drip_config)` and (later) future strategy configs.
- `tests/test_strategy.py` — unit tests for the helper.

**Modified:**
- `src/talos/drip.py` — `DripConfig.per_side_contract_cap` → `max_ahead_per_side`; delete `DripController` class; add free function `evaluate_blip(config, eta_a_min, eta_b_min, front_a_id, front_b_id) -> Action`.
- `src/talos/rebalance.py` — `compute_overcommit_reduction`, `compute_topup_needs` accept `drip_config: DripConfig | None` and use the helper.
- `src/talos/bid_adjuster.py` — `_check_post_cancel_safety` accepts `drip_config: DripConfig | None` and uses the helper; `BidAdjuster` exposes a `set_drip_config_lookup` callback so `_check_post_cancel_safety`'s callers (already inside the class) can resolve the config.
- `src/talos/opportunity_proposer.py` — drop `drip` param + `block_drip` gate.
- `src/talos/engine.py` — delete seven `is_drip(...)` gates (jumps, imbalance, queue stress, manual bid, proposal generation); remove `DripController` import + state (`_drip_controllers`, `_drip_pending_actions`); slim `_drive_drip` to BLIP-only; mark `_dirty_events` on enable/disable; rewire WS-fill path to skip DRIP branch; pass `drip_config` to rebalance/adjuster calls.

**Deleted (test files):**
- Entire `DripController` test class in `tests/test_drip_controller.py` is restructured to test the free function `evaluate_blip` and the `DripConfig` rename. Fill-tracking tests (`test_record_fill_*`, `test_matched_pair_*`, `test_partial_fill_*`, `test_duplicate_trade_id_*`) are deleted with the class.

---

## Task 1: Add `per_side_max_ahead` strategy helper

**Files:**
- Create: `src/talos/strategy.py`
- Test: `tests/test_strategy.py`

- [ ] **Step 1.1: Write the failing test**

Create `tests/test_strategy.py`:

```python
"""Tests for the per-side max-ahead strategy helper."""

from __future__ import annotations

from talos.drip import DripConfig
from talos.position_ledger import PositionLedger, Side
from talos.strategy import per_side_max_ahead


def _ledger(unit_size: int = 5) -> PositionLedger:
    return PositionLedger(event_ticker="EVT", unit_size=unit_size)


def test_returns_drip_cap_for_drip_events() -> None:
    ledger = _ledger(unit_size=5)
    config = DripConfig(drip_size=2, max_drips=3)  # cap = 6

    assert per_side_max_ahead(ledger, Side.A, config) == 6


def test_returns_unit_room_when_drip_config_is_none() -> None:
    ledger = _ledger(unit_size=5)
    # No fills: full unit available.

    assert per_side_max_ahead(ledger, Side.A, None) == 5


def test_subtracts_filled_in_unit_for_standard_strategy() -> None:
    ledger = _ledger(unit_size=5)
    ledger.record_fill_from_ws(
        Side.A, trade_id="t1", count_fp100=300, price_bps=5000, fees_bps=0
    )

    # 3 filled in current unit, 2 ahead-room remaining
    assert per_side_max_ahead(ledger, Side.A, None) == 2


def test_clamps_to_zero_when_unit_is_full() -> None:
    ledger = _ledger(unit_size=5)
    ledger.record_fill_from_ws(
        Side.A, trade_id="t1", count_fp100=500, price_bps=5000, fees_bps=0
    )

    # filled_in_unit % 5 == 0 → returns full unit (5), not 0
    assert per_side_max_ahead(ledger, Side.A, None) == 5


def test_reflects_updated_unit_size_mid_session() -> None:
    ledger = _ledger(unit_size=5)
    assert per_side_max_ahead(ledger, Side.A, None) == 5

    ledger.unit_size = 10
    assert per_side_max_ahead(ledger, Side.A, None) == 10


def test_drip_cap_ignores_unit_size() -> None:
    ledger = _ledger(unit_size=5)
    config = DripConfig(drip_size=1, max_drips=1)  # cap = 1

    # Fill 3 contracts on side A; helper still returns drip cap, not unit room.
    ledger.record_fill_from_ws(
        Side.A, trade_id="t1", count_fp100=300, price_bps=5000, fees_bps=0
    )
    assert per_side_max_ahead(ledger, Side.A, config) == 1
```

- [ ] **Step 1.2: Run the test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_strategy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'talos.strategy'`.

- [ ] **Step 1.3: Implement the helper**

Create `src/talos/strategy.py`:

```python
"""Strategy seam — per-side sizing dispatch for the standard pipeline.

Today: standard strategy (uses ledger.unit_size) and DRIP (uses
DripConfig.max_ahead_per_side). Future strategies plug in by extending
``per_side_max_ahead`` with a new config branch.
"""

from __future__ import annotations

from talos.drip import DripConfig
from talos.position_ledger import PositionLedger, Side


def per_side_max_ahead(
    ledger: PositionLedger,
    side: Side,
    drip_config: DripConfig | None,
) -> int:
    """Strategy-aware 'allowed resting' for one side, pre-catch-up.

    DRIP events return their absolute resting cap (drip_size × max_drips).
    Non-DRIP events return the standard 'room left in the current unit',
    falling back to a full unit when filled_in_unit == 0.

    The catch-up exception (max(this, fill_gap)) lives in the call sites
    so it stays uniform across strategies.
    """
    if drip_config is not None:
        return drip_config.max_ahead_per_side

    filled_in_unit = ledger.filled_count(side) % ledger.unit_size
    return ledger.unit_size - filled_in_unit
```

- [ ] **Step 1.4: Run the test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_strategy.py -v`
Expected: 6 passed.

- [ ] **Step 1.5: Commit**

```bash
git add src/talos/strategy.py tests/test_strategy.py
git commit -m "feat(strategy): add per_side_max_ahead helper as DRIP/standard seam"
```

---

## Task 2: Rename `DripConfig.per_side_contract_cap` → `max_ahead_per_side`

**Files:**
- Modify: `src/talos/drip.py:32-34`
- Modify: `tests/test_drip_controller.py:16` (and any other assertion sites)

- [ ] **Step 2.1: Run grep to find all callers**

Run: `.venv/Scripts/python -c "import subprocess; subprocess.run(['grep', '-rn', 'per_side_contract_cap', 'src/', 'tests/'], check=False)"`

Or use the Grep tool: `pattern="per_side_contract_cap"`, `path="."`.

Expected hits (verify before editing):
- `src/talos/drip.py:33` — property definition
- `src/talos/engine.py:3223` — `if ledger.resting_count(side) >= config.per_side_contract_cap:`
- `tests/test_drip_controller.py:16` — `assert cfg.per_side_contract_cap == 1`

If grep finds additional hits, rename them all in this task — do NOT leave a partial rename.

- [ ] **Step 2.2: Rename in `drip.py`**

In `src/talos/drip.py:32-34`, replace:

```python
    @property
    def per_side_contract_cap(self) -> int:
        return self.drip_size * self.max_drips
```

with:

```python
    @property
    def max_ahead_per_side(self) -> int:
        return self.drip_size * self.max_drips
```

- [ ] **Step 2.3: Update `engine.py:3223` caller**

In `src/talos/engine.py:3223`, replace:

```python
        if ledger.resting_count(side) >= config.per_side_contract_cap:
```

with:

```python
        if ledger.resting_count(side) >= config.max_ahead_per_side:
```

(This call site is removed entirely in Task 11; the rename is for grep cleanliness in the interim.)

- [ ] **Step 2.4: Update `tests/test_drip_controller.py:16`**

In `tests/test_drip_controller.py:16`, replace:

```python
    assert cfg.per_side_contract_cap == 1
```

with:

```python
    assert cfg.max_ahead_per_side == 1
```

- [ ] **Step 2.5: Run tests + grep**

Run: `.venv/Scripts/python -m pytest tests/test_drip_controller.py -v`
Expected: existing tests pass.

Run grep again for `per_side_contract_cap` — must return zero hits.

- [ ] **Step 2.6: Commit**

```bash
git add src/talos/drip.py src/talos/engine.py tests/test_drip_controller.py
git commit -m "refactor(drip): rename per_side_contract_cap to max_ahead_per_side"
```

---

## Task 3: Wire `per_side_max_ahead` into `compute_overcommit_reduction`

**Files:**
- Modify: `src/talos/rebalance.py:236-316`
- Test: `tests/test_rebalance.py` (new test added here)

This is the load-bearing site for snap-to-cap (Spec Section 3). After this task, a DRIP event with surplus resting will be cancelled down to the cap on the next overcommit-reduction sweep.

- [ ] **Step 3.1: Write the failing test**

Add to `tests/test_rebalance.py` (or create if missing — check first with `Read tests/test_rebalance.py`):

```python
def test_overcommit_reduction_uses_drip_cap_when_config_provided() -> None:
    from talos.drip import DripConfig
    from talos.models.strategy import ArbPair
    from talos.position_ledger import PositionLedger, Side
    from talos.rebalance import compute_overcommit_reduction

    ledger = PositionLedger(event_ticker="EVT", unit_size=5)
    # 5 resting on side A, no fills, no cross-side gap.
    ledger.record_placement_bps(Side.A, order_id="ord-a", count_fp100=500, price_bps=5000)
    pair = ArbPair(
        event_ticker="EVT",
        ticker_a="EVT-A",
        ticker_b="EVT-B",
        side_a="no",
        side_b="no",
        fee_rate=0.07,
    )

    drip_config = DripConfig(drip_size=1, max_drips=1)  # cap = 1
    proposal = compute_overcommit_reduction(
        event_ticker="EVT",
        ledger=ledger,
        pair=pair,
        display_name="EVT",
        drip_config=drip_config,
    )

    assert proposal is not None
    assert proposal.target_resting == 1  # cap, not unit_size=5
    assert proposal.current_resting == 5


def test_overcommit_reduction_falls_back_to_unit_when_no_drip_config() -> None:
    from talos.models.strategy import ArbPair
    from talos.position_ledger import PositionLedger, Side
    from talos.rebalance import compute_overcommit_reduction

    ledger = PositionLedger(event_ticker="EVT", unit_size=5)
    # 5 resting on A is exactly unit-sized → not overcommitted.
    ledger.record_placement_bps(Side.A, order_id="ord-a", count_fp100=500, price_bps=5000)
    pair = ArbPair(
        event_ticker="EVT",
        ticker_a="EVT-A",
        ticker_b="EVT-B",
        side_a="no",
        side_b="no",
        fee_rate=0.07,
    )

    proposal = compute_overcommit_reduction(
        event_ticker="EVT", ledger=ledger, pair=pair, display_name="EVT"
    )
    assert proposal is None  # within unit cap


def test_overcommit_reduction_drip_preserves_catchup_exception() -> None:
    """Behind side with a fill gap keeps fill_gap-many resting even under DRIP cap."""
    from talos.drip import DripConfig
    from talos.models.strategy import ArbPair
    from talos.position_ledger import PositionLedger, Side
    from talos.rebalance import compute_overcommit_reduction

    ledger = PositionLedger(event_ticker="EVT", unit_size=5)
    # Side A has 3 fills, side B has 0 fills + 5 resting (catch-up).
    ledger.record_fill_from_ws(
        Side.A, trade_id="t1", count_fp100=300, price_bps=5000, fees_bps=0
    )
    ledger.record_placement_bps(Side.B, order_id="ord-b", count_fp100=500, price_bps=5000)
    pair = ArbPair(
        event_ticker="EVT",
        ticker_a="EVT-A",
        ticker_b="EVT-B",
        side_a="no",
        side_b="no",
        fee_rate=0.07,
    )

    drip_config = DripConfig(drip_size=1, max_drips=1)  # cap = 1
    proposal = compute_overcommit_reduction(
        event_ticker="EVT",
        ledger=ledger,
        pair=pair,
        display_name="EVT",
        drip_config=drip_config,
    )

    assert proposal is not None
    assert proposal.side == "B"
    assert proposal.target_resting == 3  # max(drip_cap=1, fill_gap=3) → 3
```

- [ ] **Step 3.2: Run the failing test**

Run: `.venv/Scripts/python -m pytest tests/test_rebalance.py::test_overcommit_reduction_uses_drip_cap_when_config_provided -v`
Expected: FAIL with `TypeError: compute_overcommit_reduction() got an unexpected keyword argument 'drip_config'`.

- [ ] **Step 3.3: Add `drip_config` parameter and route through helper**

In `src/talos/rebalance.py`, find the import block at the top and add:

```python
from talos.drip import DripConfig
from talos.strategy import per_side_max_ahead
```

Then replace the `compute_overcommit_reduction` signature and body (lines 236-316). The change is:
- Add `drip_config: DripConfig | None = None` parameter (keyword-only, after `display_name`)
- Replace the `allowed_resting = max(ledger.unit_size - filled_in_unit, fill_gap)` line with `allowed_resting = max(per_side_max_ahead(ledger, side, drip_config), fill_gap)`

```python
def compute_overcommit_reduction(
    event_ticker: str,
    ledger: PositionLedger,
    pair: ArbPair,
    display_name: str,
    reconciled_targets: dict[str, int] | None = None,
    *,
    drip_config: DripConfig | None = None,
) -> ProposedRebalance | None:
    """Compute resting reduction for a single-side overcommit with no cross-side imbalance.

    This handles the case where committed counts are balanced (delta = 0)
    but one side violates the strategy's per-side cap (filled_in_unit + resting > cap).

    drip_config: when provided, the per-side cap comes from the DRIP cap
    (drip_size × max_drips) instead of the standard unit_size derivation.
    The catch-up exception (max(cap, fill_gap)) still applies — DRIP does
    not weaken the cross-side gap-closing behavior.

    reconciled_targets: optional {side_value → allowed_resting} from the
    reconciliation check. When provided, uses these authoritative targets
    instead of re-deriving from ledger.

    Returns a reduce-only ProposedRebalance (no catch-up) for the first
    overcommitted side found. After reduction, the resulting cross-side
    imbalance is handled by compute_rebalance_proposal in the next cycle.
    """
    for side in (Side.A, Side.B):
        filled = ledger.filled_count(side)
        resting = ledger.resting_count(side)
        filled_in_unit = filled % ledger.unit_size

        if reconciled_targets and side.value in reconciled_targets:
            allowed_resting = reconciled_targets[side.value]
        else:
            other = Side.B if side == Side.A else Side.A
            fill_gap = max(0, ledger.filled_count(other) - filled)
            allowed_resting = max(
                per_side_max_ahead(ledger, side, drip_config), fill_gap
            )

        if resting <= allowed_resting:
            continue

        target_resting = allowed_resting
        order_id = ledger.resting_order_id(side)
        ticker = pair.ticker_a if side == Side.A else pair.ticker_b

        if order_id is None:
            continue

        logger.warning(
            "overcommit_reduction",
            event_ticker=event_ticker,
            side=side.value,
            filled_in_unit=filled_in_unit,
            resting=resting,
            target_resting=target_resting,
            from_reconciliation=bool(
                reconciled_targets and side.value in reconciled_targets
            ),
            from_drip=drip_config is not None,
        )

        kalshi_side = pair.side_a if side == Side.A else pair.side_b

        return ProposedRebalance(
            event_ticker=event_ticker,
            side=side.value,
            order_id=order_id,
            ticker=ticker,
            current_resting=resting,
            target_resting=target_resting,
            resting_price=ledger.resting_price(side),
            filled_count=filled,
            catchup_ticker=None,
            catchup_price=0,
            catchup_qty=0,
            reduce_side=kalshi_side,
        )

    return None
```

- [ ] **Step 3.4: Update engine callers to pass `drip_config`**

Grep for `compute_overcommit_reduction(` in `src/talos/engine.py`:

```bash
grep -n "compute_overcommit_reduction(" src/talos/engine.py
```

For each caller, add `drip_config=self._drip_events.get(event_ticker)` (or the equivalent local variable for the event ticker in scope). Example pattern:

```python
overcommit = compute_overcommit_reduction(
    event_ticker,
    ledger,
    pair,
    self._display_name(event_ticker),
    drip_config=self._drip_events.get(event_ticker),
)
```

If a caller uses `pair.event_ticker` instead of a bare `event_ticker`, use `self._drip_events.get(pair.event_ticker)`.

- [ ] **Step 3.5: Run the rebalance tests**

Run: `.venv/Scripts/python -m pytest tests/test_rebalance.py -v`
Expected: all three new tests pass; existing tests still pass.

- [ ] **Step 3.6: Run the engine test suite**

Run: `.venv/Scripts/python -m pytest tests/test_engine.py -v -x`
Expected: pass. If a test fails because it asserts the old `unit_size`-based target on a DRIP path, update the assertion in this task — those tests now describe behavior that is correct only without DRIP.

- [ ] **Step 3.7: Commit**

```bash
git add src/talos/rebalance.py src/talos/engine.py tests/test_rebalance.py
git commit -m "feat(rebalance): route overcommit reduction through per_side_max_ahead"
```

---

## Task 4: Wire `per_side_max_ahead` into `compute_topup_needs`

**Files:**
- Modify: `src/talos/rebalance.py:319-382`

The top-up qty in `compute_topup_needs` (line 361: `qty = ledger.unit_size - filled_in_unit`) is a per-side target. Under DRIP, top-ups should be sized to the drip cap, not the unit.

- [ ] **Step 4.1: Write the failing test**

Add to `tests/test_rebalance.py`:

```python
def test_topup_needs_uses_drip_cap_when_config_provided() -> None:
    from talos.drip import DripConfig
    from talos.models.strategy import ArbPair, Opportunity
    from talos.position_ledger import PositionLedger, Side
    from talos.rebalance import compute_topup_needs

    ledger = PositionLedger(event_ticker="EVT", unit_size=5)
    # 1 fill on each side, no resting.
    ledger.record_fill_from_ws(
        Side.A, trade_id="ta", count_fp100=100, price_bps=5000, fees_bps=0
    )
    ledger.record_fill_from_ws(
        Side.B, trade_id="tb", count_fp100=100, price_bps=5000, fees_bps=0
    )
    pair = ArbPair(
        event_ticker="EVT",
        ticker_a="EVT-A",
        ticker_b="EVT-B",
        side_a="no",
        side_b="no",
        fee_rate=0.07,
    )
    opp = Opportunity(
        event_ticker="EVT",
        no_a=50,
        no_b=49,
        fee_edge=1.0,
    )

    drip_config = DripConfig(drip_size=1, max_drips=1)  # cap = 1
    needs = compute_topup_needs(ledger, pair, opp, drip_config=drip_config)

    # With DRIP cap=1 and 1 already filled per side, top-up qty = 1
    # (cap is absolute, not "remaining").
    for side in (Side.A, Side.B):
        assert needs[side][0] == 1
```

- [ ] **Step 4.2: Run the failing test**

Run: `.venv/Scripts/python -m pytest tests/test_rebalance.py::test_topup_needs_uses_drip_cap_when_config_provided -v`
Expected: FAIL with `TypeError` on the unknown `drip_config` kwarg.

- [ ] **Step 4.3: Add `drip_config` parameter and route through helper**

In `src/talos/rebalance.py`, replace the `compute_topup_needs` signature and the qty derivation:

```python
def compute_topup_needs(
    ledger: PositionLedger,
    pair: ArbPair,
    snapshot: Opportunity | None,
    *,
    drip_config: DripConfig | None = None,
) -> dict[Side, tuple[int, int]]:
    """Compute top-up needs for mid-unit sides with no resting bids.

    Returns dict mapping Side → (qty, price) for each side needing top-up.
    Only fires when committed counts are equal (catch-up handles imbalances).

    drip_config: when provided, the per-side target comes from the DRIP cap.
    Pure function — no I/O.
    """
    if snapshot is None:
        return {}

    if snapshot.fee_edge <= 0:
        return {}

    filled_a = ledger.filled_count(Side.A)
    filled_b = ledger.filled_count(Side.B)

    if filled_a // ledger.unit_size != filled_b // ledger.unit_size:
        return {}

    needs: dict[Side, tuple[int, int]] = {}
    for side in (Side.A, Side.B):
        filled = ledger.filled_count(side)
        resting = ledger.resting_count(side)

        if filled == 0:
            continue
        if resting > 0:
            continue

        if drip_config is not None:
            qty = drip_config.max_ahead_per_side
        else:
            filled_in_unit = filled % ledger.unit_size
            if filled_in_unit == 0:
                continue
            qty = ledger.unit_size - filled_in_unit

        if qty <= 0:
            continue

        price = snapshot.no_a if side == Side.A else snapshot.no_b
        if price <= 0:
            continue
        ok, _ = ledger.is_placement_safe(side, qty, price, rate=pair.fee_rate)
        if not ok:
            continue
        needs[side] = (qty, price)

    if len(needs) == 1:
        side = next(iter(needs))
        qty, _ = needs[side]
        other = Side.B if side == Side.A else Side.A
        if ledger.total_committed(side) + qty > ledger.total_committed(other):
            return {}

    return needs
```

- [ ] **Step 4.4: Update engine callers**

Grep for `compute_topup_needs(` in `src/talos/engine.py` and pass `drip_config=self._drip_events.get(<event_ticker>)`.

- [ ] **Step 4.5: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_rebalance.py tests/test_engine.py -v`
Expected: pass.

- [ ] **Step 4.6: Commit**

```bash
git add src/talos/rebalance.py src/talos/engine.py tests/test_rebalance.py
git commit -m "feat(rebalance): route topup_needs through per_side_max_ahead"
```

---

## Task 5: Wire `per_side_max_ahead` into `_check_post_cancel_safety`

**Files:**
- Modify: `src/talos/bid_adjuster.py:748-789` (`_check_post_cancel_safety`)
- Modify: `src/talos/engine.py` — wire a drip-config lookup callback when constructing `BidAdjuster`.

The post-cancel safety check enforces "would this placement exceed the per-side cap?". Today it computes from `unit_size`. Under DRIP, it must use the DRIP cap.

- [ ] **Step 5.1: Inspect current BidAdjuster construction site**

Find where `BidAdjuster` is instantiated in `engine.py`:

```bash
grep -n "BidAdjuster(" src/talos/engine.py
```

Read the constructor and a few lines around the construction site to understand what's already wired.

- [ ] **Step 5.2: Add a drip-config lookup callback to `BidAdjuster`**

In `src/talos/bid_adjuster.py`, find the `BidAdjuster.__init__` method and add a new optional parameter `drip_config_lookup: Callable[[str], DripConfig | None] | None = None`. Store it as `self._drip_config_lookup`. Add a helper method:

```python
    def _drip_config_for(self, event_ticker: str) -> DripConfig | None:
        if self._drip_config_lookup is None:
            return None
        return self._drip_config_lookup(event_ticker)
```

You'll need to add imports at the top of `bid_adjuster.py`:

```python
from collections.abc import Callable
from talos.drip import DripConfig
from talos.strategy import per_side_max_ahead
```

- [ ] **Step 5.3: Update `_check_post_cancel_safety` to use the helper**

Replace the body (lines 748-789):

```python
    def _check_post_cancel_safety(
        self,
        ledger: PositionLedger,
        side: Side,
        new_count: int,
        new_price: int,
    ) -> tuple[bool, str]:
        """Check safety as if the existing resting order were already cancelled.

        ``new_price`` is integer cents (Kalshi submission boundary). Internal
        profitability math runs in bps and rounds back to whole cents for the
        per-contract fee formula.
        """
        drip_config = self._drip_config_for(ledger.event_ticker)
        max_ahead = per_side_max_ahead(ledger, side, drip_config)
        if new_count > max_ahead:
            scope = "drip cap" if drip_config is not None else "unit"
            return (
                False,
                f"would exceed {scope} after cancel: new={new_count} > {max_ahead}",
            )
        # Check profitability (open-unit scoped — same as is_placement_safe P18)
        other_side = side.other
        if ledger.open_count(other_side) > 0:
            other_avg_bps = ledger.avg_filled_price_bps(other_side)
            other_price_bps = int(round(other_avg_bps / ONE_CENT_BPS)) * ONE_CENT_BPS
        elif ledger.resting_count(other_side) > 0:
            other_price_bps = ledger.resting_price_bps(other_side)
        else:
            return True, ""

        rate = self._fee_rate_for(ledger.event_ticker)
        effective_this_bps = fee_adjusted_cost_bps(cents_to_bps(new_price), rate=rate)
        effective_other_bps = fee_adjusted_cost_bps(other_price_bps, rate=rate)
        if effective_this_bps + effective_other_bps >= ONE_DOLLAR_BPS:
            effective_this = effective_this_bps / ONE_CENT_BPS
            effective_other = effective_other_bps / ONE_CENT_BPS
            return (
                False,
                f"arb not profitable: {effective_this:.2f}+{effective_other:.2f} >= 100",
            )
        return True, ""
```

Note the semantic shift: under the new helper, `max_ahead` is the absolute per-side cap (the helper bakes in `unit_size - filled_in_unit` for the standard case). The check becomes `new_count > max_ahead`, not `filled_in_unit + new_count > unit_size`. These are equivalent for the standard case.

- [ ] **Step 5.4: Wire the lookup callback at engine construction**

In `src/talos/engine.py`, find the `BidAdjuster(...)` construction call. Add:

```python
self._adjuster = BidAdjuster(
    ...,
    drip_config_lookup=lambda evt: self._drip_events.get(evt),
)
```

(Replace `...` with the existing kwargs — do not delete them.)

- [ ] **Step 5.5: Write a focused test**

Add to `tests/test_bid_adjuster.py`:

```python
def test_post_cancel_safety_uses_drip_cap_when_lookup_returns_config() -> None:
    from talos.bid_adjuster import BidAdjuster
    from talos.drip import DripConfig
    from talos.position_ledger import PositionLedger, Side

    drip_config = DripConfig(drip_size=1, max_drips=1)
    adjuster = BidAdjuster(
        # ... preserve any other required args from existing test fixtures.
        drip_config_lookup=lambda evt: drip_config if evt == "EVT" else None,
    )
    ledger = PositionLedger(event_ticker="EVT", unit_size=5)

    # 2 contracts > drip cap of 1 → blocked even though within unit_size.
    ok, reason = adjuster._check_post_cancel_safety(ledger, Side.A, new_count=2, new_price=50)
    assert ok is False
    assert "drip cap" in reason
```

If `BidAdjuster` requires more constructor args, copy them from an existing fixture in `tests/test_bid_adjuster.py`.

- [ ] **Step 5.6: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_bid_adjuster.py tests/test_engine.py -v`
Expected: pass.

- [ ] **Step 5.7: Commit**

```bash
git add src/talos/bid_adjuster.py src/talos/engine.py tests/test_bid_adjuster.py
git commit -m "feat(adjuster): route post-cancel safety through per_side_max_ahead"
```

---

## Task 6: Wire `per_side_max_ahead` into engine reconcile site

**Files:**
- Modify: `src/talos/engine.py:2436-2456` (the reconciliation overcommit check)

Today (line 2442): `allowed = max(ledger.unit_size - filled_in_unit, fill_gap)`. Under DRIP, this must use the DRIP cap.

- [ ] **Step 6.1: Locate the site**

Read `src/talos/engine.py` lines 2410-2475 to confirm the `allowed = max(...)` line is still at line 2442 (it may shift after earlier tasks).

- [ ] **Step 6.2: Update the reconcile site**

Replace the block:

```python
                filled_in_unit = auth_fills[side] % ledger.unit_size
                other_side = Side.B if side == Side.A else Side.A
                fill_gap = max(0, auth_fills[other_side] - auth_fills[side])
                allowed = max(ledger.unit_size - filled_in_unit, fill_gap)
```

with:

```python
                filled_in_unit = auth_fills[side] % ledger.unit_size
                other_side = Side.B if side == Side.A else Side.A
                fill_gap = max(0, auth_fills[other_side] - auth_fills[side])
                allowed = max(
                    per_side_max_ahead(
                        ledger, side, self._drip_events.get(pair.event_ticker)
                    ),
                    fill_gap,
                )
```

Important: `per_side_max_ahead` reads `ledger.filled_count(side)` for the standard case, but here the reconciliation has computed an authoritative `auth_fills[side]` that may exceed `ledger.filled_count(side)`. To preserve correctness, when DRIP is NOT enabled, fall through to the existing `ledger.unit_size - filled_in_unit` math (which uses `auth_fills`). When DRIP IS enabled, the cap is absolute and `auth_fills` is irrelevant. So the actual replacement is:

```python
                filled_in_unit = auth_fills[side] % ledger.unit_size
                other_side = Side.B if side == Side.A else Side.A
                fill_gap = max(0, auth_fills[other_side] - auth_fills[side])
                drip_config = self._drip_events.get(pair.event_ticker)
                if drip_config is not None:
                    base_allowed = drip_config.max_ahead_per_side
                else:
                    base_allowed = ledger.unit_size - filled_in_unit
                allowed = max(base_allowed, fill_gap)
```

This preserves the `auth_fills`-based math for the standard case (where the helper would use stale `ledger.filled_count`) and switches to the absolute cap for DRIP.

- [ ] **Step 6.3: Add the import**

At the top of `src/talos/engine.py`, add:

```python
from talos.strategy import per_side_max_ahead
```

(Even though the reconcile site doesn't use the helper directly, the next task will, and centralizing the import here is cleaner. If lint complains about unused import, defer adding it until Task 11.)

- [ ] **Step 6.4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_engine.py -v -x`
Expected: pass.

- [ ] **Step 6.5: Commit**

```bash
git add src/talos/engine.py
git commit -m "feat(engine): route reconcile overcommit check through DRIP cap"
```

---

## Task 7: Snap-to-cap on toggle — mark `_dirty_events` in `enable_drip`

**Files:**
- Modify: `src/talos/engine.py:555-575` (`enable_drip`)
- Modify: `src/talos/engine.py:649-671` (`disable_drip`)

Per spec Section 3, `enable_drip` should mark the event dirty so the next `check_imbalances` cycle evaluates and snaps surplus down to the cap. `disable_drip` should also mark dirty so the cap snaps back to `unit_size` (and likewise no longer cancels orders directly — the rebalancer will handle any reduction needed if `unit_size < cap`).

- [ ] **Step 7.1: Write the failing test**

Add to `tests/test_engine.py` (or a new `tests/test_drip_snap_to_cap.py`):

```python
async def test_enable_drip_marks_event_dirty() -> None:
    """enable_drip should add the event to _dirty_events so the next
    check_imbalances cycle evaluates and snaps to the new cap."""
    # Use the standard engine fixture; pseudocode below — adapt to your
    # existing fixture pattern from test_engine.py.
    from talos.drip import DripConfig

    engine = make_engine_with_pair("EVT")  # existing helper / fixture

    assert "EVT" not in engine._dirty_events

    engine.enable_drip("EVT", DripConfig(drip_size=1, max_drips=1))

    assert "EVT" in engine._dirty_events


async def test_disable_drip_marks_event_dirty() -> None:
    from talos.drip import DripConfig

    engine = make_engine_with_pair("EVT")
    engine.enable_drip("EVT", DripConfig(drip_size=1, max_drips=1))
    engine._dirty_events.discard("EVT")  # simulate cycle consumed it

    await engine.disable_drip("EVT")

    assert "EVT" in engine._dirty_events
```

If your test file uses synchronous fixtures, adapt the helper accordingly. Look at existing tests in `test_engine.py` for the canonical engine-construction pattern.

- [ ] **Step 7.2: Run the failing test**

Run: `.venv/Scripts/python -m pytest tests/test_engine.py -k "marks_event_dirty" -v`
Expected: FAIL — assertion error on `_dirty_events`.

- [ ] **Step 7.3: Mark dirty in `enable_drip`**

In `src/talos/engine.py`, modify `enable_drip`:

```python
    def enable_drip(self, event_ticker: str, config: DripConfig) -> bool:
        """Enable DRIP/BLIP for an event. Returns True when enabled."""
        if event_ticker in self._exit_only_events:
            self._notify("DRIP blocked: event is in exit-only mode", "warning", toast=True)
            return False
        if config.max_drips != 1:
            self._notify("DRIP POC supports MAX_DRIPS=1 only", "warning", toast=True)
            return False

        self._drip_events[event_ticker] = config
        # Force next check_imbalances cycle to evaluate this event so the
        # standard rebalancer can snap surplus resting down to the new cap.
        self._dirty_events.add(event_ticker)
        logger.info(
            "drip_enabled",
            event_ticker=event_ticker,
            drip_size=config.drip_size,
            max_drips=config.max_drips,
            blip_delta_min=config.blip_delta_min,
        )
        self._notify(f"DRIP ON: {self._display_name(event_ticker)}", "warning", toast=True)
        return True
```

(Note: `self._drip_controllers.setdefault(...)` and `self._enforce_drip_sync(event_ticker)` are removed — both go away in Task 11. If you're executing tasks in strict order, leave them in place for now and remove in Task 11.)

- [ ] **Step 7.4: Mark dirty in `disable_drip` and remove order cancellation**

The current `disable_drip` cancels resting orders. Under the new model, resting orders belong to the standard pipeline — the next rebalance cycle will reconcile to `max(unit_size - filled_in_unit, fill_gap)` automatically, which may keep them all (cap raised). Cancellation here is wrong because it would destroy in-progress catch-up.

Replace `disable_drip`:

```python
    async def disable_drip(self, event_ticker: str) -> bool:
        """Disable DRIP for an event. Resting orders are NOT cancelled —
        the standard rebalancer reconciles them to the standard cap on the
        next cycle (typically a no-op since unit_size >= drip_cap)."""
        if event_ticker not in self._drip_events:
            return False
        self._drip_events.pop(event_ticker, None)
        self._drip_blip_last_at = {
            key: value for key, value in self._drip_blip_last_at.items() if key[0] != event_ticker
        }
        # Force next cycle to re-evaluate against the standard cap.
        self._dirty_events.add(event_ticker)
        logger.info("drip_disabled", event_ticker=event_ticker)
        self._notify(f"DRIP OFF: {self._display_name(event_ticker)}", toast=True)
        return True
```

(References to `_drip_controllers` and `_drip_pending_actions` are removed — Task 11 deletes those attributes entirely. If executing tasks in order and they still exist, also pop from them here.)

- [ ] **Step 7.5: Run the test**

Run: `.venv/Scripts/python -m pytest tests/test_engine.py -k "marks_event_dirty" -v`
Expected: PASS.

- [ ] **Step 7.6: Run full engine suite**

Run: `.venv/Scripts/python -m pytest tests/test_engine.py -v`
Expected: pass. If a test asserts the old "disable_drip cancels orders" behavior, rewrite it to assert "disable_drip does NOT cancel orders" — that's the correct new behavior.

- [ ] **Step 7.7: Commit**

```bash
git add src/talos/engine.py tests/test_engine.py
git commit -m "feat(drip): mark event dirty on toggle for snap-to-cap behavior"
```

---

## Task 8: Replace `DripController.evaluate_blip` with free function

**Files:**
- Modify: `src/talos/drip.py:84-160` (delete `DripController`, add free `evaluate_blip`)
- Modify: `tests/test_drip_controller.py` (delete state-tracking tests, restructure BLIP tests)

- [ ] **Step 8.1: Replace the controller class with a free function**

In `src/talos/drip.py`, delete the entire `DripController` class (lines 84-160) and replace with a free function that has the same BLIP semantics:

```python
def evaluate_blip(
    config: DripConfig,
    *,
    eta_a_min: float | None,
    eta_b_min: float | None,
    front_a_id: str | None,
    front_b_id: str | None,
) -> Action:
    """BLIP ahead side when ETA_behind - ETA_ahead exceeds threshold.

    Pure function — replaces the former DripController.evaluate_blip
    method. Fill tracking now lives in the standard PositionLedger; this
    function only consumes ETA + front-order signals.
    """
    ahead = _identify_ahead_side(eta_a_min, eta_b_min)
    if ahead is None:
        return NoOp("no_eta_signal")

    if ahead == "A":
        eta_ahead = eta_a_min
        eta_behind = eta_b_min
        order_id = front_a_id
    else:
        eta_ahead = eta_b_min
        eta_behind = eta_a_min
        order_id = front_b_id

    if eta_ahead is None:
        return NoOp("no_ahead_eta")
    if order_id is None:
        return NoOp("no_front_order")

    if _eta_delta(eta_ahead, eta_behind) > config.blip_delta_min:
        return BlipAction(ahead, order_id)
    return NoOp("blip_below_threshold")
```

Note the return type changes from `list[Action]` to `Action` (single action). The list was a vestigial shape from when `record_fill` could emit multiple PlaceOrder actions; BLIP only ever returns one. If preserving the list shape is cheaper for callers, keep `list[Action]` and wrap each return in `[...]`. Pick one — the consuming engine code in Task 11 will adapt.

For this plan I'll go with **single `Action` return** since it's cleaner and Task 11 has only one caller.

- [ ] **Step 8.2: Replace `tests/test_drip_controller.py` contents**

Delete the entire file and replace with:

```python
"""Tests for DRIP/BLIP free-function behavior.

The former DripController class was removed in the insertion-strategy-only
redesign — fills now flow through the standard PositionLedger, and BLIP
is a pure function over ETA / front-order signals.
"""

from __future__ import annotations

import pytest

from talos.drip import BlipAction, DripConfig, NoOp, evaluate_blip


def test_drip_config_defaults() -> None:
    cfg = DripConfig()

    assert cfg.drip_size == 1
    assert cfg.max_drips == 1
    assert cfg.blip_delta_min == 5.0
    assert cfg.max_ahead_per_side == 1


def test_drip_config_validates_positive_values() -> None:
    with pytest.raises(ValueError):
        DripConfig(drip_size=0)
    with pytest.raises(ValueError):
        DripConfig(max_drips=0)
    with pytest.raises(ValueError):
        DripConfig(blip_delta_min=-0.1)


def test_blip_fires_on_ahead_side_when_eta_delta_exceeds_threshold() -> None:
    cfg = DripConfig(blip_delta_min=5.0)

    action = evaluate_blip(
        cfg,
        eta_a_min=2.0,
        eta_b_min=10.0,
        front_a_id="order-a",
        front_b_id="order-b",
    )

    assert action == BlipAction("A", "order-a")


def test_blip_does_not_fire_within_threshold() -> None:
    cfg = DripConfig(blip_delta_min=5.0)

    action = evaluate_blip(
        cfg,
        eta_a_min=2.0,
        eta_b_min=4.0,
        front_a_id="order-a",
        front_b_id="order-b",
    )

    assert action == NoOp("blip_below_threshold")


def test_blip_treats_behind_none_as_infinite_eta() -> None:
    cfg = DripConfig(blip_delta_min=5.0)

    action = evaluate_blip(
        cfg,
        eta_a_min=2.0,
        eta_b_min=None,
        front_a_id="order-a",
        front_b_id="order-b",
    )

    assert action == BlipAction("A", "order-a")


def test_blip_noops_without_any_eta_signal() -> None:
    cfg = DripConfig(blip_delta_min=5.0)

    action = evaluate_blip(
        cfg,
        eta_a_min=None,
        eta_b_min=None,
        front_a_id="order-a",
        front_b_id="order-b",
    )

    assert action == NoOp("no_eta_signal")


def test_blip_noops_when_front_order_missing() -> None:
    cfg = DripConfig(blip_delta_min=5.0)

    action = evaluate_blip(
        cfg,
        eta_a_min=2.0,
        eta_b_min=10.0,
        front_a_id=None,  # ahead side has no resting order
        front_b_id="order-b",
    )

    assert action == NoOp("no_front_order")
```

The five deleted tests (`test_record_fill_*`, `test_matched_pair_*`, `test_partial_fill_*`, `test_duplicate_trade_id_*`, etc.) are intentionally gone — the behavior they covered (fill tracking, pair completion, trade-id dedup) now lives entirely in `PositionLedger.record_fill_from_ws`, which has its own coverage in `tests/test_position_ledger.py`.

- [ ] **Step 8.3: Run the new tests**

Run: `.venv/Scripts/python -m pytest tests/test_drip_controller.py -v`
Expected: 7 passed. (`drip.py` has not yet had its imports cleaned up; that happens when engine.py is updated in Task 11.)

- [ ] **Step 8.4: Verify no other code imports `DripController`**

```bash
grep -rn "DripController" src/ tests/
```

Expected hits: only `src/talos/engine.py` (lines 26, 179, and `_drip_controllers` references). These are all torn out in Task 11. No tests should still import it.

- [ ] **Step 8.5: Commit**

```bash
git add src/talos/drip.py tests/test_drip_controller.py
git commit -m "refactor(drip): replace DripController with free evaluate_blip function"
```

---

## Task 9: Delete `block_drip` gate in opportunity_proposer

**Files:**
- Modify: `src/talos/opportunity_proposer.py:89-114` (remove `drip` param + gate)
- Modify: any test in `tests/test_opportunity_proposer.py` asserting `block_drip` behavior

- [ ] **Step 9.1: Find existing assertions**

```bash
grep -n "block_drip\|drip=True\|drip=False" tests/test_opportunity_proposer.py
```

For each test that asserts `block_drip` (the proposer should NOT propose for DRIP events), invert it: now the proposer SHOULD propose, so the assertion becomes "proposal is not None when DRIP is enabled."

- [ ] **Step 9.2: Write a regression test**

Add to `tests/test_opportunity_proposer.py`:

```python
def test_proposer_generates_proposal_when_drip_enabled_no_longer_blocks() -> None:
    """After the insertion-strategy redesign, the proposer no longer takes
    a drip flag — DRIP events flow through the normal proposal path."""
    # Build the same fixture as a passing-proposal test (find one in this file
    # and copy the setup), then call .evaluate() WITHOUT a drip kwarg.
    config = AutomationConfig(edge_threshold_cents=1.0, stability_seconds=0)
    proposer = OpportunityProposer(config)
    pair = make_pair("EVT")  # use existing test helper
    opp = make_opportunity(no_a=49, no_b=49, fee_edge=2.0)  # use existing helper
    ledger = PositionLedger(event_ticker="EVT", unit_size=5)

    proposal = proposer.evaluate(
        pair, opp, ledger, pending_keys=set(),
        display_name="EVT",
        # NOTE: no `drip=` kwarg — it's been removed from the signature.
    )

    assert proposal is not None
    assert proposal.kind == "bid"
```

If your existing tests use a different helper convention, adapt to match.

- [ ] **Step 9.3: Run the failing test**

Run: `.venv/Scripts/python -m pytest tests/test_opportunity_proposer.py -v -k "drip"`
Expected: existing `block_drip` test still passes (gate is in place); new test passes only after the gate is removed AND the `drip` kwarg is removed.

- [ ] **Step 9.4: Remove the gate and the `drip` parameter**

In `src/talos/opportunity_proposer.py`, modify `evaluate`:

```python
    def evaluate(
        self,
        pair: ArbPair,
        opportunity: Opportunity,
        ledger: PositionLedger,
        pending_keys: set[ProposalKey],
        now: datetime | None = None,
        display_name: str = "",
        exit_only: bool = False,
        pair_volume_24h: int | None = None,
    ) -> Proposal | None:
        """Return a bid proposal if all gates pass, None otherwise."""
        if now is None:
            now = datetime.now(UTC)

        event = pair.event_ticker

        # Gate 0: exit-only — no new bids
        if exit_only:
            self._emit(event, "block_exit_only", "exit-only mode, no new bids")
            return None

        # (DRIP gate removed — DRIP is now an insertion-strategy parameter,
        # not an event-ownership flag. Per-side caps are enforced by the
        # standard pre-place safety check via per_side_max_ahead.)
```

Delete the `if drip:` block entirely (lines 111-113).

- [ ] **Step 9.5: Update tests asserting `block_drip`**

For every test in `tests/test_opportunity_proposer.py` that calls `.evaluate(..., drip=True)`:
- Drop the `drip=True` kwarg (signature no longer accepts it).
- If the test asserted `proposal is None`, remove the test or invert the assertion to `proposal is not None` (assuming the rest of the gates pass).
- If the test asserted `block_drip` log emission, delete the test — that decision-log row no longer exists.

- [ ] **Step 9.6: Run the proposer suite**

Run: `.venv/Scripts/python -m pytest tests/test_opportunity_proposer.py -v`
Expected: pass. The `drip=True` callers are gone; the new positive test passes.

- [ ] **Step 9.7: Commit**

```bash
git add src/talos/opportunity_proposer.py tests/test_opportunity_proposer.py
git commit -m "feat(proposer): remove block_drip gate; standard pipeline runs on DRIP events"
```

---

## Task 10: Delete `is_drip` early-return gates in engine.py

**Files:**
- Modify: `src/talos/engine.py:2572` (`_generate_jump_proposal`)
- Modify: `src/talos/engine.py:2660` (`_reevaluate_jumps_for`)
- Modify: `src/talos/engine.py:2714` (`_check_imbalance_for`)
- Modify: `src/talos/engine.py:2787` (`reevaluate_jumps`)
- Modify: `src/talos/engine.py:2871` (`check_imbalances`)
- Modify: `src/talos/engine.py:3066` (proposer call — remove `drip=...` kwarg)
- Modify: `src/talos/engine.py:3311` (`check_queue_stress`)
- Modify: `src/talos/engine.py:3520-3526` (manual-bid gate)

Each of these locks the standard pipeline out of DRIP events. Per spec Section 5, all are deleted.

Note: line numbers may shift after earlier tasks. Locate each by content:

| Search pattern | What to remove |
|---|---|
| `if evt_ticker and self.is_drip(evt_ticker):` (in `_generate_jump_proposal`) | The two-line `if ... return` block |
| `if self.is_drip(event_ticker):` (in `_reevaluate_jumps_for`) | Same |
| `if self.is_drip(event_ticker):` (in `_check_imbalance_for`) | Same |
| `if self.is_drip(pair.event_ticker):` followed by `continue` (in `reevaluate_jumps`) | Two-line block |
| `if self.is_drip(pair.event_ticker):` followed by `continue` (in `check_imbalances`) | Two-line block |
| `drip=self.is_drip(pair.event_ticker),` (in proposer call) | Remove this kwarg line |
| `if event_ticker in self._exit_only_events or self.is_drip(event_ticker):` (in `check_queue_stress`) | Drop the `or self.is_drip(...)` clause |
| `if evt_for_bid and self.is_drip(evt_for_bid):` block (in manual-bid path, lines 3520-3526) | Delete the entire 7-line block |

- [ ] **Step 10.1: Write a regression test (one is enough)**

Add to `tests/test_engine.py`:

```python
async def test_check_imbalances_runs_on_drip_events() -> None:
    """Regression: under the insertion-strategy redesign, DRIP events must
    flow through check_imbalances exactly like non-DRIP events."""
    from talos.drip import DripConfig

    engine = make_engine_with_pair("EVT")  # use your existing fixture
    engine.enable_drip("EVT", DripConfig(drip_size=1, max_drips=1))
    engine._dirty_events.clear()  # clear the dirty bit added by enable
    engine._dirty_events.add("EVT")

    # Patch compute_rebalance_proposal to assert it WAS called for the DRIP event.
    called_for: list[str] = []
    original = sys.modules["talos.rebalance"].compute_rebalance_proposal

    def spy(event_ticker, *args, **kwargs):
        called_for.append(event_ticker)
        return original(event_ticker, *args, **kwargs)

    monkeypatch.setattr("talos.rebalance.compute_rebalance_proposal", spy)
    await engine.check_imbalances()

    assert "EVT" in called_for
```

(If your test file already has a fixture for `monkeypatch`, use it. If not, structure as a pytest function with the `monkeypatch` parameter.)

- [ ] **Step 10.2: Run the failing test**

Run: `.venv/Scripts/python -m pytest tests/test_engine.py -k "runs_on_drip_events" -v`
Expected: FAIL — `compute_rebalance_proposal` is never called because the gate skips DRIP events.

- [ ] **Step 10.3: Delete the eight gates**

For each pattern in the table above, find the line in `src/talos/engine.py` and delete it (and the associated `return` / `continue` / `or self.is_drip(...)` clause).

The proposer-call site (line ~3066) needs more care — remove `drip=self.is_drip(pair.event_ticker),` and verify the surrounding kwargs still parse:

```python
            proposal = self._proposer.evaluate(
                pair,
                opp,
                ledger,
                pending_keys,
                display_name=self._display_name(pair.event_ticker),
                exit_only=self.is_exit_only(pair.event_ticker),
                pair_volume_24h=pair_volume,
            )
```

- [ ] **Step 10.4: Run the regression test**

Run: `.venv/Scripts/python -m pytest tests/test_engine.py -k "runs_on_drip_events" -v`
Expected: PASS.

- [ ] **Step 10.5: Run the full suite**

Run: `.venv/Scripts/python -m pytest -x`
Expected: pass. Tests that asserted "DRIP blocks X" must be inverted or deleted in this task — fix them now.

Common failures to expect and fix:
- Tests asserting `is_drip` blocks `reevaluate_jumps` → invert to "fires on DRIP events too."
- Tests asserting the manual-bid gate fires → delete (the gate is gone).
- Tests asserting `block_drip` decision row → delete.

- [ ] **Step 10.6: Commit**

```bash
git add src/talos/engine.py tests/test_engine.py
git commit -m "feat(engine): delete is_drip gates blocking standard pipeline"
```

---

## Task 11: Slim `_drive_drip` to BLIP-only; tear out controller plumbing

**Files:**
- Modify: `src/talos/engine.py` — multiple sections.

This is the biggest task. After it: no `DripController`, no `_drip_pending_actions`, no DRIP seed/replenish/place-bid logic in the engine. `_drive_drip` runs `evaluate_blip` and executes a single optional `BlipAction`. The DRIP branch in the WS-fill handler is gone.

- [ ] **Step 11.1: Remove DRIP controller imports and state**

In `src/talos/engine.py`:

1. Line 26 — change the import from:

   ```python
   from talos.drip import Action, BlipAction, DripConfig, DripController, NoOp, PlaceOrder
   ```

   to:

   ```python
   from talos.drip import BlipAction, DripConfig, NoOp, evaluate_blip
   ```

   (Drop `Action`, `DripController`, `PlaceOrder` — none used after this task.)

2. Lines 179-180 — delete:

   ```python
       self._drip_controllers: dict[str, DripController] = {}
       self._drip_pending_actions: dict[str, list[Action]] = {}
   ```

3. In `enable_drip` (already simplified in Task 7), confirm `self._drip_controllers.setdefault(...)` is gone.

4. In `restore_drip_from_saved` (lines 600-647), delete the `self._drip_controllers.setdefault(...)` line.

5. In `disable_drip` (already simplified in Task 7), confirm `self._drip_controllers.pop(...)` and `self._drip_pending_actions.pop(...)` are gone.

6. Delete `_enforce_drip_sync` method (lines 677-682) and its sole call site (already removed in Task 7's `enable_drip` simplification).

- [ ] **Step 11.2: Remove the DRIP branch in the WS-fill handler**

Lines 2023-2034 today:

```python
                    if self.is_drip(event_ticker):
                        controller = self._drip_controllers.get(event_ticker)
                        if controller is not None:
                            drip_side = "A" if side == Side.A else "B"
                            actions = controller.record_fill(
                                drip_side,
                                msg.count_fp100,
                                trade_id=msg.trade_id,
                            )
                            self._drip_pending_actions.setdefault(event_ticker, []).extend(
                                actions
                            )
```

Delete this entire block. Fills now hit only the standard ledger via `record_fill_from_ws` (already called above on line 2016).

- [ ] **Step 11.3: Replace `_drive_drip` with a BLIP-only loop**

Locate `_drive_drip` (~line 3073) and replace the whole method:

```python
    async def _drive_drip(self, event_ticker: str) -> None:
        """Evaluate BLIP for a DRIP-enabled event and execute one BlipAction.

        Seeding / replenishment / market-following / cap enforcement now
        flow through the standard pipeline (rebalance, top-up, opportunity
        proposer). This method only handles the BLIP overlay — sending the
        ahead side to the back of the queue when the per-side ETA gap
        exceeds blip_delta_min.

        Runs at the end of refresh_account, AFTER the standard pipeline.
        That ordering matters: BLIP must not fire on an order the
        rebalancer is about to cancel.
        """
        config = self._drip_events.get(event_ticker)
        pair = self.find_pair(event_ticker)
        if config is None or pair is None:
            return
        if not self._initial_sync_done:
            return

        eta_a, front_a = self._drip_eta_and_front(event_ticker, pair, Side.A)
        eta_b, front_b = self._drip_eta_and_front(event_ticker, pair, Side.B)
        action = evaluate_blip(
            config,
            eta_a_min=eta_a,
            eta_b_min=eta_b,
            front_a_id=front_a,
            front_b_id=front_b,
        )
        await self._execute_blip(event_ticker, pair, action)
```

- [ ] **Step 11.4: Replace `_execute_drip_action` with BLIP-only `_execute_blip`**

Find `_execute_drip_action` (~line 3155) and the `_drip_place_bid` helper (~line 3202) and `_drip_current_price_bps` (~line 3287). Delete all three. Add a single replacement:

```python
    async def _execute_blip(
        self,
        event_ticker: str,
        pair: ArbPair,
        action: Action,  # type: ignore[name-defined]
    ) -> None:
        """Execute a single BLIP action — cancel and re-place at the same price.

        Cancel-then-place is canonical 'back of queue at this price level'
        on Kalshi (FIFO). Re-placement runs through the same REST path as
        any standard order; the standard ledger records it.
        """
        if isinstance(action, NoOp):
            return
        if not isinstance(action, BlipAction):
            return

        key = (event_ticker, action.side)
        now = time.monotonic()
        last = self._drip_blip_last_at.get(key)
        if last is not None and now - last < _DRIP_BLIP_COOLDOWN_S:
            logger.info(
                "drip_blip_skip_cooldown",
                event_ticker=event_ticker,
                side=action.side,
            )
            return

        side = Side.A if action.side == "A" else Side.B
        try:
            ledger = self._adjuster.get_ledger(event_ticker)
        except KeyError:
            return
        price_bps = ledger.resting_price_bps(side)
        if price_bps <= 0:
            return

        # Determine the count from the existing resting order so we re-place
        # at the same size we cancelled (preserves DRIP resting cap).
        resting_count_fp100 = ledger.resting_count_fp100(side)
        if resting_count_fp100 <= 0:
            return

        await self.cancel_order_with_verify(action.order_id, pair)
        self._drip_blip_last_at[key] = now

        ticker = pair.ticker_a if side == Side.A else pair.ticker_b
        pair_side = pair.side_a if side == Side.A else pair.side_b
        order = await self._rest.create_order(
            ticker=ticker,
            action="buy",
            side=pair_side,
            yes_price_bps=price_bps if pair_side == "yes" else None,
            no_price_bps=price_bps if pair_side == "no" else None,
            count_fp100=resting_count_fp100,
            post_only=True,
        )
        ledger.record_placement_bps(
            side,
            order_id=order.order_id,
            count_fp100=order.remaining_count_fp100,
            price_bps=price_bps,
        )
        self._orders_cache.append(order)
        self._order_placed_at[order.order_id] = time.monotonic()
        logger.info(
            "drip_blip_executed",
            event_ticker=event_ticker,
            side=action.side,
            old_order_id=action.order_id,
            new_order_id=order.order_id,
        )
```

The `Action` type alias (`PlaceOrder | BlipAction | NoOp`) is no longer imported. Either:
- (a) keep the parameter as `Action`, re-import it, and accept that `PlaceOrder` is unreachable (defensive); or
- (b) tighten the type to `BlipAction | NoOp` and drop the `PlaceOrder` branch entirely.

Choose **(b)** — it's the clean expression of the new model:

```python
    async def _execute_blip(
        self,
        event_ticker: str,
        pair: ArbPair,
        action: BlipAction | NoOp,
    ) -> None:
        if isinstance(action, NoOp):
            return
        # BlipAction
        ...
```

If you prefer (a) for paranoid forward-compat, that's fine too — but the redesign explicitly removes `PlaceOrder` from DRIP's vocabulary.

- [ ] **Step 11.5: Confirm `enforce_drip` still has a sensible role**

`enforce_drip` (line 673-675) calls `_drive_drip`. Under the new model, the standard pipeline does the seeding on the next `check_imbalances` cycle. `_drive_drip` only does BLIP, which is meaningless on the very first cycle (no resting order to BLIP yet). So `enforce_drip` becomes a no-op — delete it AND its sole caller. Find the caller:

```bash
grep -n "enforce_drip\b" src/talos/
```

Common caller: a UI binding that runs after `enable_drip`. Replace any `await engine.enforce_drip(event_ticker)` calls with a no-op (or just delete the call — the dirty-bit added in Task 7 handles the work).

- [ ] **Step 11.6: Run the full suite**

Run: `.venv/Scripts/python -m pytest -x`
Expected: pass. Failures here are likely:
- Tests calling `enforce_drip` → delete the call.
- Tests calling `_execute_drip_action` directly → restructure to call `_execute_blip` with `BlipAction`.
- Tests asserting `_drip_pending_actions` exists → delete the assertion.
- Tests asserting `_drip_controllers` exists → delete.
- Tests asserting `_enforce_drip_sync` is called → delete.

Fix each as you encounter it.

- [ ] **Step 11.7: Run lint + typecheck**

Run: `.venv/Scripts/python -m ruff check src/ tests/ && .venv/Scripts/python -m pyright`
Expected: clean. If `pyright` complains about `Action` being undefined or `PlaceOrder` unused, the import block at line 26 (Step 11.1) needs the corresponding adjustment.

- [ ] **Step 11.8: Commit**

```bash
git add src/talos/engine.py tests/
git commit -m "refactor(engine): slim _drive_drip to BLIP-only; remove controller plumbing"
```

---

## Task 12: Update remaining tests + verify behavior end-to-end

**Files:**
- Modify: `tests/test_drip_modal.py` — DRIP modal UI test still works; verify property-name assertions.
- Modify: any test still referencing `DripController`, `_drip_pending_actions`, `_drip_controllers`, `block_drip`, `per_side_contract_cap`, or `is_drip` blocking.

- [ ] **Step 12.1: Find stragglers**

Run grep across both src and tests:

```bash
grep -rn "DripController\|_drip_pending_actions\|_drip_controllers\|per_side_contract_cap\|enforce_drip\|_enforce_drip_sync\|block_drip" src/ tests/
```

Expected: zero hits in `src/`, possibly a few in `tests/` if any test reference slipped through. For each test hit:
- If the test asserted the old behavior, delete or invert.
- If the test merely imported a removed symbol, remove the import.

- [ ] **Step 12.2: Verify UI stability**

`tests/test_drip_modal.py` exercises the keyboard-toggle + modal flow. Run it:

```bash
.venv/Scripts/python -m pytest tests/test_drip_modal.py -v
```

Expected: pass. The modal collects `drip_size` / `max_drips` / `blip_delta_min` and calls `engine.enable_drip(...)` — none of that surface area changed. If a test asserts `cfg.per_side_contract_cap` (caught earlier in Task 2 grep, but double-check), update to `max_ahead_per_side`.

- [ ] **Step 12.3: Run the entire suite, no marker filter**

```bash
.venv/Scripts/python -m pytest -m ""
```

Expected: all 1611 tests pass. ~96s runtime per CLAUDE.md.

- [ ] **Step 12.4: Run lint + typecheck**

```bash
.venv/Scripts/python -m ruff check src/ tests/
.venv/Scripts/python -m pyright
```

Expected: clean.

- [ ] **Step 12.5: Mandatory safety walks (per CLAUDE.md)**

This change touches `position_ledger`-adjacent code via the new helper, and changes order placement / cap enforcement. Both safety walks apply.

Write into the commit message body (or a separate note file in `brain/`) two short walks:

**Position-scenarios walk** — for each of these, explain how the new helper + slimmed `_drive_drip` behave correctly:

1. **Cold start** — DRIP enabled, ledger has zero fills, zero resting. Standard pipeline's opportunity proposer fires (gate is gone). Proposer's qty-derivation uses `unit_remaining`, but the post-cancel safety check rejects placements over `max_ahead_per_side`. Result: first standard proposal places `min(unit_remaining, drip_cap)` contracts.

   *Wait — this is an open question.* The proposer in `evaluate` (line 271-289) computes `qty = min(need_a, need_b)` from `unit_remaining - resting_count`. It does NOT consult `per_side_max_ahead`. Under DRIP cap=1, the proposer would propose qty=`unit_size` (e.g., 5), and `is_placement_safe` would let it through; the post-cancel safety in the adjuster only fires on the CANCEL-AND-REPLACE path, not initial placements.

   **Action:** add Step 12.6 to wire the proposer's qty-derivation through the helper.

2. **YES ahead by N** — fills A=3, B=0; DRIP cap=1. `per_side_max_ahead(A) = 1`, but `fill_gap = max(0, 0 - 3) = 0` (A is the leader, not behind). Side A is allowed `max(1, 0) = 1` resting. Side B catch-up: `per_side_max_ahead(B) = 1`, `fill_gap = max(0, 3 - 0) = 3`, so B is allowed `max(1, 3) = 3` resting (catch-up exception). Confirm: matches Spec Section 3 row 2.

3. **NO ahead by N** — symmetric to #2.

4. **WS-drop window** — fills land via reconcile (REST), not WS. Reconcile uses `per_side_max_ahead` via the engine's reconcile site (Task 6). DRIP cap is enforced from REST data exactly as from WS.

5. **Mid-session restart** — `restore_drip_from_saved` repopulates `_drip_events`; the next `refresh_account` cycle marks the event in `_dirty_events`? Verify: `_dirty_events` is NOT seeded by restore. The first cycle after restart picks up DRIP via the standard pipeline, which routes through the helper. Snap-to-cap happens on the first cycle that flags the event dirty (likely a fill or top-of-book change). If the event is steady, snap-to-cap waits for the periodic full sweep (every 10 cycles). **This is correct** — the restored DRIP cap is in effect from cycle 1 for any new placements; existing surplus resting (rare on restart) gets reconciled within ~5 minutes.

6. **Dedup overlap** — fills via WS use `record_fill_from_ws` which trade-id-dedupes inside the ledger. The DRIP-specific dedup in `DripController._seen_trade_ids` is gone, but it was redundant with the ledger's dedup. Confirm: re-applying the same WS fill twice still credits only one fill in the ledger.

**Safety-audit walk** — list each invariant in `brain/principles.md` and explain how the change preserves it. Focus on Principles 7 and 15 (Kalshi as source of truth) and any cap-enforcement principle.

- [ ] **Step 12.6: Wire proposer's qty derivation through the helper**

Per the cold-start finding above. In `src/talos/opportunity_proposer.py`, add a `drip_config` parameter to `.evaluate()` (default `None`) and use it in qty derivation. Also pass it to the adjuster's safety check.

In `src/talos/opportunity_proposer.py`, modify `.evaluate()` signature:

```python
    def evaluate(
        self,
        pair: ArbPair,
        opportunity: Opportunity,
        ledger: PositionLedger,
        pending_keys: set[ProposalKey],
        now: datetime | None = None,
        display_name: str = "",
        exit_only: bool = False,
        pair_volume_24h: int | None = None,
        *,
        drip_config: DripConfig | None = None,
    ) -> Proposal | None:
```

Add the import at the top:

```python
from talos.drip import DripConfig
from talos.strategy import per_side_max_ahead
```

Replace the qty derivation block (lines 270-289):

```python
        # Qty = remaining capacity bounded by the strategy cap.
        if ledger.both_sides_complete() and ledger.filled_count(Side.A) == ledger.filled_count(
            Side.B
        ):
            base_qty = ledger.unit_size
        else:
            need_a = ledger.unit_remaining(Side.A) - ledger.resting_count(Side.A)
            need_b = ledger.unit_remaining(Side.B) - ledger.resting_count(Side.B)
            base_qty = min(need_a, need_b)
            if base_qty <= 0:
                self._emit(
                    event,
                    "block_no_qty",
                    f"no remaining capacity: need_a={need_a} need_b={need_b}",
                    fee_edge=opportunity.fee_edge,
                )
                return None

        cap_a = per_side_max_ahead(ledger, Side.A, drip_config) - ledger.resting_count(Side.A)
        cap_b = per_side_max_ahead(ledger, Side.B, drip_config) - ledger.resting_count(Side.B)
        qty = min(base_qty, max(0, cap_a), max(0, cap_b))
        if qty <= 0:
            self._emit(
                event,
                "block_strategy_cap",
                f"strategy cap leaves no room: cap_a={cap_a} cap_b={cap_b}",
                fee_edge=opportunity.fee_edge,
            )
            return None
```

In `src/talos/engine.py`, update the proposer call (already trimmed of `drip=...` in Task 10) to pass `drip_config`:

```python
            proposal = self._proposer.evaluate(
                pair,
                opp,
                ledger,
                pending_keys,
                display_name=self._display_name(pair.event_ticker),
                exit_only=self.is_exit_only(pair.event_ticker),
                pair_volume_24h=pair_volume,
                drip_config=self._drip_events.get(pair.event_ticker),
            )
```

Add a test:

```python
def test_proposer_caps_qty_at_drip_max_ahead() -> None:
    from talos.drip import DripConfig

    config = AutomationConfig(edge_threshold_cents=1.0, stability_seconds=0)
    proposer = OpportunityProposer(config)
    pair = make_pair("EVT")  # existing helper
    opp = make_opportunity(no_a=49, no_b=49, fee_edge=2.0)
    ledger = PositionLedger(event_ticker="EVT", unit_size=5)

    drip_config = DripConfig(drip_size=1, max_drips=1)  # cap = 1
    proposal = proposer.evaluate(
        pair, opp, ledger, pending_keys=set(),
        display_name="EVT",
        drip_config=drip_config,
    )

    assert proposal is not None
    assert proposal.bid is not None
    assert proposal.bid.qty == 1  # capped at drip cap, not unit_size=5
```

Run:

```bash
.venv/Scripts/python -m pytest tests/test_opportunity_proposer.py tests/test_engine.py -v
```

Expected: pass.

- [ ] **Step 12.7: Final lint + typecheck + full suite**

```bash
.venv/Scripts/python -m ruff check --fix src/ tests/
.venv/Scripts/python -m ruff format src/ tests/
.venv/Scripts/python -m pyright
.venv/Scripts/python -m pytest -m ""
```

Expected: all clean and all green.

- [ ] **Step 12.8: Commit and update brain/decisions.md**

Append a decision-record entry to `brain/decisions.md`:

```markdown
## 2026-04-28 — DRIP redesigned as insertion-strategy parameter

PR: (link after PR creation)

Replaced the parallel DRIP pipeline (DripController + _drive_drip owning
seed/replenish/place + 7 is_drip gates blocking the standard pipeline)
with a single `per_side_max_ahead(ledger, side, drip_config)` helper
routed through every per-side cap site (rebalance, top-up, post-cancel
safety, reconcile, proposer qty). DRIP events now flow through the
standard pipeline; `_drive_drip` is BLIP-only.

Snap-to-cap on toggle handled by adding the event to `_dirty_events`;
the next `check_imbalances` cycle calls `compute_overcommit_reduction`,
which reads the new cap via the helper and cancels surplus resting
down to it.

DripController class removed; `evaluate_blip` is a free function in
drip.py. `DripConfig.per_side_contract_cap` renamed to
`max_ahead_per_side` for cross-strategy consistency.

Frozen-row symptom from 2026-04-28 (KXTRUMP-…-PELO row 143) resolved:
under the new model, the standard pipeline's catch-up exception
covers the queue-bumped behind-side independent of ETA-gap signals.
```

Commit:

```bash
git add brain/decisions.md src/ tests/
git commit -m "feat: DRIP redesigned as insertion-strategy parameter (closes <issue>)"
```

---

## Self-Review Checklist (run after writing — performed)

**Spec coverage:**

- ✅ Section 1 (helper as seam) → Task 1
- ✅ Section 2 (helper plumbing & call sites) → Tasks 3, 4, 5, 6, 12.6 (covers all four sites in the spec table plus the proposer qty site discovered in Step 12.5)
- ✅ Section 3 (snap-to-cap on toggle) → Task 7 + Task 3 (rebalancer reads new cap)
- ✅ Section 4 (slimmed `_drive_drip`) → Task 11
- ✅ Section 5 (deletion list) → Task 9 (proposer gate), Task 10 (seven engine gates), Task 11 (controller, pending actions, seed, place_bid, drip_current_price)
- ✅ Testing strategy → coverage included in each task; final regression run in Step 12.7

**Naming corrections:**
- Spec's `compute_unit_overcommit_proposal` → real `compute_overcommit_reduction` — flagged at top of plan, used in Task 3.
- `per_side_contract_cap` → `max_ahead_per_side` — Task 2.

**Type consistency:**
- `evaluate_blip` returns `Action` (single) in Task 8; consumed as a single value in Task 11. Consistent.
- `drip_config: DripConfig | None` parameter shape consistent across `compute_overcommit_reduction`, `compute_topup_needs`, `_check_post_cancel_safety` (via lookup callback), `proposer.evaluate`. Consistent.
- `per_side_max_ahead(ledger, side, drip_config)` signature stable across all call sites. Consistent.

**Open question caught and addressed:** the proposer's qty derivation does not consult the helper in the spec — Step 12.5's cold-start walk surfaced this gap and Step 12.6 fixes it.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-28-drip-redesign-plan.md`.

Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — I execute tasks in this session, batching tasks with checkpoints for your review.

Which approach?
