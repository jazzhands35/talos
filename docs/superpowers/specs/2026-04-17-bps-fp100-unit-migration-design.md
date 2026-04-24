# BPS / FP100 Unit Migration

**Date:** 2026-04-17 (design converged) / 2026-04-21 (spec written)
**Status:** Draft — awaiting operator review
**Branch:** `feat/bps-fp100-migration`
**Design authority:** locked decisions from 2026-04-17 brainstorming + codex review session (memory: `project_bps_migration_pending.md`). This spec translates those decisions into a concrete implementation plan. It does **not** re-derive the design.

---

## Problem

Talos's internal money-unit invariant (integer cents for prices, integer contracts for counts) breaks on two increasingly common Kalshi market classes:

1. **Sub-cent-tick markets.** E.g. DJT at `3.8¢ / 96.1¢`. `src/talos/scanner.py:182` gates on `raw_edge = 100 - no_a.price - no_b.price`, where `price` has already been rounded to an integer cent by `dollars_to_cents`. Markets whose true inside price falls between integer cents are silently dropped from the opportunity stream because their rounded prices collapse to a non-profitable integer.

2. **Fractional maker fills.** A whole-contract limit order at a price that attracts a fractional aggressor will receive a fractional fill. Observed live on 2026-04-21 on `KXTRUMPSAYNICKNAME-26JUL01-MARJ`: a 5-lot maker at 49¢ YES filled 1.89 contracts at 48.88¢ (cost $0.92). `src/talos/models/_converters.py:39` uses `int(float(val))` which truncates `"1.89"` → `1`. The ledger then records the fill as `count=1` at `price=maker_fill_cost=92¢` — a silent inventory loss of 0.89 contracts AND a cost-basis inflation from ~52¢ to 60¢ avg on the side, which propagates into `HOLD_UNPROFITABLE` and exposure/locked-P&L displays.

Both failures stem from the same architectural invariant: **integer cents and integer contracts are embedded everywhere in the core trading path.** The memory identifies six layers from `_converters.py` parsing through `rest_client.py` request payload formatting, plus money-critical touch-sites in `fees.py`, `position_ledger.py`, and `bid_adjuster.py`.

## Goal

Make Talos capable of correctly representing, storing, and acting on every Kalshi market shape — including `fractional_trading_enabled` markets and sub-cent tick markets — without loss of fidelity at the parsing boundary or during ledger persistence.

## Non-goals

- **Active tick-snapping** on outbound prices when the bid adjuster proposes prices that aren't reflected from the book. Follow-up spec when `bid_adjuster` begins proposing off-book prices on sub-cent markets.
- **Fractional-contract order submission.** Only fractional *parsing* is in scope; Talos still only submits whole-contract orders. (Fractional arrivals via maker fills must still be represented correctly once received.)
- **Historical closed-event ledger backfill.** Existing closed-bucket state on disk migrates in place by unit scale; archival of historical per-fill detail is not in scope.
- **Production opt-in.** Demo-env only until a separate hardening PR.

## Supersession

This spec reverses two out-of-scope declarations in `brain/plans/02-kalshi-fp-migration/overview.md` lines 22–26:

> - Fractional trading support (Talos uses whole contracts only)
> - Subpenny pricing support (not relevant to current strategy)

Both are explicitly reversed. When this spec lands, a supersession note in the old overview must point here.

## Phase split

### Phase 0 — fractional-market gating (separate PR, independently useful)

Before migration, prevent NEW trades from entering markets that will expose the truncation bug.

**Ingress audit.** Markets enter Talos's trading path through multiple code paths, not just the scanner:

| Ingress path | Current code | Covered by scanner-only guard? |
|---|---|---|
| Automatic scanner discovery | `src/talos/scanner.py` | ✅ |
| Manual "add market" UI | `src/talos/ui/app.py:~772` (manual add) | ❌ |
| Market-picker UI | `src/talos/ui/app.py:~881` | ❌ |
| Tree-commit (bulk admit from opportunity tree) | `src/talos/engine.py:~3135` | ❌ |
| Startup restore of persisted games | `src/talos/engine.py:~1107` | ❌ |

A scanner-only guard leaves four ingress paths bypassing it. An operator could manually add a fractional market and hit the bug directly. This was the F30 finding in codex round 11.

**Centralized admission guard (F30 fix).** Extract a shared validator that every ingress path invokes:

```python
# src/talos/game_manager.py (or equivalent admission choke-point)

class MarketAdmissionError(Exception):
    """Raised when a market is rejected at admission because it violates
    the shape invariants the current trading path can safely handle."""
    pass

def validate_market_for_admission(market_a: Market, market_b: Market) -> None:
    """Raise MarketAdmissionError if either market has a shape Talos cannot
    currently handle safely. Enforced at EVERY ingress path — not just scanner.

    Phase 0 checks: reject fractional_trading_enabled and sub-cent-tick markets
    until Phase 1+2 bps/fp100 migration lands. Phase 1+2 relaxes or removes
    these specific checks; other shape invariants may be added here over time.
    """
    for m in (market_a, market_b):
        if m.fractional_trading_enabled:
            raise MarketAdmissionError(
                f"{m.ticker}: fractional_trading_enabled markets are not supported "
                f"until the bps/fp100 migration lands (Phase 1+2). "
                f"See docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md"
            )
        if m.tick_bps() < ONE_CENT_BPS:
            raise MarketAdmissionError(
                f"{m.ticker}: sub-cent-tick markets are not supported until Phase 1+2"
            )
```

**Required call sites (every ingress path):**

1. Scanner: inside the pair-construction loop, before producing an `Opportunity`. On rejection, log once per ticker at WARNING.
2. Manual-add UI handler: call before constructing the `ArbPair`. On rejection, surface a modal with the error message; do not add the pair.
3. Market-picker UI: same pattern as manual add.
4. Tree-commit handler: call per-row before admitting. **Structured rejection contract (F34 + F35 fix):** do NOT merely log-and-skip, and do NOT defer the contract to `_commit_worker` — by that point `TreeScreen.commit()` has already applied staging-clear and post-add metadata on rejected rows. The contract must be enforced at the `TreeScreen.commit()` layer:

   - `add_pairs_from_selection(...)` (inner helper) returns a `CommitResult` dataclass with fields `admitted: list[PairSelection]` and `rejected: list[tuple[PairSelection, MarketAdmissionError]]`.
   - `TreeScreen.commit()` (~`src/talos/ui/tree_screen.py:~941`) consumes the `CommitResult` and applies post-add bookkeeping **only to admitted rows**:
     - Staging-clear (the `to_clear_unticked` sweep at `~src/talos/ui/tree_screen.py:~1121–1143`) runs only for admitted rows. Rejected rows remain ticked/staged.
     - Any metadata application (labels, subtitles, etc.) runs only for admitted rows.
     - `TreeScreen.commit()`'s return value distinguishes clean-success from partial-rejection so `_commit_worker` can render the right message.
   - `_commit_worker` (~`src/talos/ui/tree_screen.py:~1507–1509`): shows the "Commit complete." toast ONLY when the commit was clean (all rows admitted). On any rejection, shows the partial-failure dialog enumerating rejected rows with their `MarketAdmissionError.message`. Rejected rows remain in the staging area so the operator sees them and decides (deselect, wait for Phase 1+2, or investigate the specific ticker).
   - Log level stays WARNING per rejected row for operational visibility; the authoritative UX signal is the structured dialog, not the log.

   **End-to-end test required (F35 regression):** the test must exercise `TreeScreen.commit() → _commit_worker`, not just `add_pairs_from_selection` in isolation. Covers the path where staging-clear runs AFTER CommitResult is inspected. A unit test on `add_pairs_from_selection` alone would have let the v12→v13 "silent partial success" bug survive unchanged.
5. **Startup restore path: quarantined restore, not rejection.** The restore flow registers the pair, seeds the ledger, and applies persisted engine state via `_apply_persisted_engine_state(pair)` (`src/talos/engine.py:~3701`). Admission rejection BEFORE registration would leave nothing to mark as quarantined and would effectively drop the pair — losing operator visibility into open positions on a now-invalid market. Wrong behavior. Instead:

   a. Always register persisted pairs, regardless of admission result — the pair object, ledger, and feed state must exist so open exposure can be managed.
   b. Run `validate_market_for_admission` AFTER `restore_game(...)` completes.
   c. On admission failure, force `pair.engine_state = "exit_only"` (or `"winding_down"` if the pair already had that state persisted), add to `_exit_only_events`, and surface an operator-visible notification on startup that enumerates the admission reason. The pair is now quarantined: cancels work, position-exit flows work, but `create_order` / `amend_order` for new entry are blocked by engine_state gating (not by the confirmation gate — that's a separate mechanism).
   d. **Durably persist the quarantine state immediately (F37 fix).** `engine_state` is a safety-critical persisted field (`src/talos/game_manager.py:~536`, `src/talos/persistence.py:~254`). A quarantine that exists only in memory would get overwritten back to `active` on the next crash/restart cycle when the original persisted `engine_state="active"` is rehydrated — recreating the exact unsupported-market startup window the fix is meant to close. After forcing the quarantine state, immediately call `Engine._persist_games_now(None, None)` (whole-file write, no per-pair proposed snapshot) to persist the updated engine_state before the engine proceeds. This is an O(<10ms) extra disk write per admission failure; acceptable since admission failures on restore should be rare in practice.
   e. Do NOT silently drop the pair. Do NOT unregister it. Do NOT reject before registration. The quarantine keeps the operator in control of open exposure on a market that has become Phase-0-incompatible.

   This is the **F32 fix.** New admission (scanner, manual add, market-picker, tree commit) rejects outright before any ledger/feed/pair object gets created. Restore admission is permissive on registration, restrictive on future entry.

**Model changes:**

1. Extend `Market` model (`src/talos/models/market.py`) with:
   - `fractional_trading_enabled: bool = False`
   - `price_level_structure: str = ""`
   - `price_ranges: list[PriceRange] = []` (tuple of min/max/tick if structured; else empty)
2. Add `Market.tick_bps() -> int` helper.

**Testing (Phase 0):**

- Unit: `validate_market_for_admission` accepts cent-only non-fractional markets; rejects fractional; rejects sub-cent; rejects combinations.
- Integration: each of the five ingress paths feeds a fractional market through its own entry point and asserts `MarketAdmissionError` is raised OR the pair is not admitted to the game manager. No scanner-only coverage — each path has its own regression test.
- Startup-restore test: persist a pair whose market_a now has `fractional_trading_enabled=True`; restart Talos; assert the pair is loaded in `winding_down` state and an operator notification fires.

Phase 0 is independently useful and can ship without Phase 1+2. Ordering rationale: Phase 0 stops the bleeding on ALL ingress paths; Phase 1+2 fixes the wound and removes the Phase 0 guards (at which point admission accepts fractional and sub-cent markets).

### Phase 1+2 — full unit migration (one PR)

Everything in the nine design sections below. Single PR because the unit invariant crosses module boundaries — splitting creates a transient state where some modules use bps and others use cents, with ledger corruption as the failure mode.

---

## Design

### Section 1 — Units

**Prices and money** are represented as **integer basis points of a dollar** (`_bps` suffix). `1 bps = $0.0001`. `$1.00 = 10_000 bps`. `1¢ = 100 bps`. Chosen over tenths-of-a-cent for forward-compatibility: Kalshi's published docs describe `_dollars` strings with up to 4 decimals and fee rounding to `$0.0001`.

**Contract counts** are represented as **integer fp100** (`_fp100` suffix). `1 fp100 = 0.01 contract`. `1 contract = 100 fp100`. Counts must migrate too: Kalshi returns fractional `count_fp` values on `fractional_trading_enabled` markets even when the operator submitted a whole-contract order, so any int representation silently loses inventory on a partial maker fill.

**unit_size** remains a policy parameter expressed in whole contracts (e.g., 5). Internal pair-matching arithmetic operates on `unit_size_fp100 = unit_size * ONE_CONTRACT_FP100`. Safety gates that compute submittable headroom for new orders round down to whole-contract boundaries: `floor(headroom_fp100 / ONE_CONTRACT_FP100) * ONE_CONTRACT_FP100`.

### Section 2 — `src/talos/units.py` (new module, single source of truth)

```python
"""Unit arithmetic and boundary conversions. Single source of truth.

No other module in src/talos/ is permitted to:
  - use a literal 100 / 10_000 as an arithmetic operand on a price/count
  - use :.2f / :.4f format specs on price/cost/bps variables
  - call float() on a Kalshi _dollars or _fp payload

Violations are caught by tests/test_unit_discipline.py.
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
from typing import Any

# ── Constants ─────────────────────────────────────────────────────
ONE_DOLLAR_BPS = 10_000          # $1 = 10_000 bps
ONE_CENT_BPS = 100               # 1¢ = 100 bps
ONE_CONTRACT_FP100 = 100         # 1 contract = 100 fp100

# Kalshi wire precision (decimals)
DOLLARS_WIRE_DECIMALS = 4
FP_WIRE_DECIMALS = 2

_BPS_SCALE = Decimal(ONE_DOLLAR_BPS)
_FP100_SCALE = Decimal(ONE_CONTRACT_FP100)

# ── Parsing (boundary: wire → internal) ────────────────────────────
#
# Contract: fail closed on any input that doesn't scale to an exact
# integer at internal precision. Rounding at the trust boundary would
# silently shift price/count/fee by 1+ bps/fp100 on a malformed payload,
# which is exactly the failure mode this migration exists to eliminate.
# If Kalshi ever emits sub-bps or sub-fp100 precision, we want a loud
# error at the parse site, not a silent drift downstream.

def dollars_str_to_bps(val: Any) -> int:
    """'0.0488' → 488 bps. None → 0. Raises on non-integral input.

    Examples:
      '0.53'    → 5300  (whole cents)
      '0.0488'  → 488   (sub-cent, 4 decimals — Kalshi fractional ticker)
      '0.53001' → raises ValueError (extra precision — not a bps value)
    """
    if val is None:
        return 0
    try:
        d = Decimal(str(val))
    except InvalidOperation as exc:
        raise ValueError(f"invalid _dollars payload: {val!r}") from exc
    scaled = d * _BPS_SCALE
    if scaled % 1 != 0:
        raise ValueError(
            f"_dollars payload has sub-bps precision: {val!r} "
            f"(scaled={scaled}) — refusing to round at trust boundary"
        )
    return int(scaled)

def fp_str_to_fp100(val: Any) -> int:
    """'1.89' → 189 fp100. None → 0. Raises on non-integral input.

    Examples:
      '10.00'   → 1000  (whole contracts)
      '1.89'    → 189   (fractional — partial maker fill)
      '1.891'   → raises ValueError (extra precision — not an fp100 value)
    """
    if val is None:
        return 0
    try:
        d = Decimal(str(val))
    except InvalidOperation as exc:
        raise ValueError(f"invalid _fp payload: {val!r}") from exc
    scaled = d * _FP100_SCALE
    if scaled % 1 != 0:
        raise ValueError(
            f"_fp payload has sub-fp100 precision: {val!r} "
            f"(scaled={scaled}) — refusing to round at trust boundary"
        )
    return int(scaled)

# ── Formatting (boundary: internal → wire) ─────────────────────────
def bps_to_dollars_str(bps: int) -> str:
    """Format an internal bps value as a Kalshi ``_dollars`` wire string.

    Whole-cent values serialize to 2 decimals (``5300 → '0.53'``) matching
    the proven pre-migration wire format that cent-tick markets accept.
    Sub-cent values serialize to 4 decimals (``488 → '0.0488'``), required
    by sub-cent-tick markets (e.g. DJT at 3.8¢/96.1¢).

    This conditional is intentional and is the settled request contract:
    it avoids betting migration-day on whether Kalshi accepts 4-decimal
    payloads on cent-only markets.
    """
    if bps % ONE_CENT_BPS == 0:
        return f"{Decimal(bps) / _BPS_SCALE:.2f}"
    return f"{Decimal(bps) / _BPS_SCALE:.{DOLLARS_WIRE_DECIMALS}f}"

def fp100_to_fp_str(fp100: int) -> str:
    """189 fp100 → '1.89' (2-decimal Kalshi wire format)."""
    return f"{Decimal(fp100) / _FP100_SCALE:.{FP_WIRE_DECIMALS}f}"

# ── Helpers ────────────────────────────────────────────────────────
def complement_bps(price_bps: int) -> int:
    """NO = 1 - YES in dollar space. 488 bps → 9_512 bps."""
    return ONE_DOLLAR_BPS - price_bps

def cents_to_bps(cents: int) -> int:
    """Operator-facing cents value → internal bps. Lossless."""
    return cents * ONE_CENT_BPS

def bps_to_cents_round(bps: int) -> int:
    """Internal bps → display cents (half-even round). Lossy."""
    return int(Decimal(bps).quantize(Decimal(ONE_CENT_BPS)) / ONE_CENT_BPS)

def contracts_to_fp100(contracts: int) -> int:
    """Operator-facing whole-contract quantity → internal fp100."""
    return contracts * ONE_CONTRACT_FP100

def fp100_to_whole_contracts_floor(fp100: int) -> int:
    """Submittable whole-contract quantity from a fractional fp100 count."""
    return fp100 // ONE_CONTRACT_FP100

# ── Display formatters ─────────────────────────────────────────────
def format_bps_as_cents(bps: int) -> str:
    """488 bps → '4.88¢'. Display only."""
    return f"{Decimal(bps) / Decimal(ONE_CENT_BPS):.2f}¢"

def format_bps_as_dollars_display(bps: int) -> str:
    """488 bps → '$0.05'. Display only (2-decimal, rounded)."""
    return f"${Decimal(bps) / _BPS_SCALE:.2f}"

def format_fp100_as_contracts(fp100: int) -> str:
    """189 fp100 → '1.89'. Display only."""
    return f"{Decimal(fp100) / _FP100_SCALE:.2f}"

# ── Fee arithmetic (moved from fees.py, uses internal bps) ─────────
def quadratic_fee_bps(price_bps: int, *, rate: float) -> int:
    """Per-contract maker fee in bps. Rounded half-even.

    Formula in dollar space: fee = rate × price × (1 − price).
    In bps: fee_bps = rate × price_bps × (ONE_DOLLAR_BPS − price_bps) / ONE_DOLLAR_BPS.
    """
    fee_d = Decimal(str(rate)) * Decimal(price_bps) * Decimal(complement_bps(price_bps)) / _BPS_SCALE
    return int(fee_d.to_integral_value())
```

**Allowed literals inside `units.py`**: `100`, `10_000`, `4`, `2`. Everywhere else, these are AST-test errors.

### Section 3 — Boundary parsing (`src/talos/models/_converters.py`)

Replace the two existing helpers. The names change to make the AST test's call-graph discipline easy:

```python
# OLD — removed
# def dollars_to_cents(val): return round(float(val) * 100)
# def fp_to_int(val): return int(float(val))

# NEW — re-exports from units.py for backward-compat during review, then deleted
from talos.units import dollars_str_to_bps as dollars_to_bps  # noqa: F401
from talos.units import fp_str_to_fp100 as fp_to_fp100        # noqa: F401
```

During the migration transition, the old names remain as deprecated re-exports for a single review pass. The AST test flags any caller that hasn't been migrated; once all callers use the new names, the `_converters.py` module is deleted entirely and callers import from `units.py` directly.

**Precision rationale:** `float("0.038") * 100 == 3.8000000000000003`. At 4-decimal wire precision this is a landmine — `round()` happens to handle `3.80...0003` correctly today but won't on edge-case values. Decimal arithmetic is exact through the full `_dollars` precision range.

### Section 4 — REST client wire format

`src/talos/rest_client.py` currently formats prices with `f"{no_price / 100:.2f}"` (lines 309, 311, 371, 373). Replace with:

```python
from talos.units import bps_to_dollars_str, fp100_to_fp_str

# create_order / amend_order body construction:
if no_price_bps is not None:
    body["no_price_dollars"] = bps_to_dollars_str(no_price_bps)
if yes_price_bps is not None:
    body["yes_price_dollars"] = bps_to_dollars_str(yes_price_bps)
body["count_fp"] = fp100_to_fp_str(count_fp100)
```

**Wire-format contract (settled):** `bps_to_dollars_str` (defined in Section 2) returns a 2-decimal string when the price is a whole-cent value (`bps % ONE_CENT_BPS == 0`) and a 4-decimal string otherwise. Rationale:

- Every order Talos has ever placed went through the 2-decimal path. That path is proven on every cent-tick market in the portfolio.
- Sub-cent markets (DJT-class, fractional-enabled) require ≥4-decimal precision — the 2-decimal path would round `3.8¢` to `4¢` and fail.
- The conditional rule preserves zero risk of migration-day order rejections on cent-only markets while unblocking sub-cent market coverage.
- The rule is deterministic and testable: `test_units.py` parameterizes over integer bps from 0 to 10000 and the fractional boundary cases (3800, 4888, 9610, etc.).

Conditional serialization is not a hack or a fallback; it is the settled contract. Always-4-decimal was considered and rejected because it would make the migration PR's blast radius dependent on Kalshi API validator behavior we cannot verify without live traffic, which violates the "don't bet money-critical correctness on unverified third-party behavior" principle.

### Section 5 — Internal rename (core trading path)

Every monetary field in the core trading path is renamed with a `_bps` or `_fp100` suffix. The rename is exhaustive by design: partial rename leaves type-unsafe "does this field mean cents or bps?" landmines in ambiguous positions.

**Files in rename scope, in rough dependency order:**

| File | Fields that change | Notes |
|------|-------------------|-------|
| `src/talos/models/order.py` | `Order.yes_price → yes_price_bps`; `Order.no_price → no_price_bps`; `Order.initial_count/remaining_count/fill_count → *_fp100`; `Order.taker_fees/maker_fees → *_bps`; `Order.maker_fill_cost/taker_fill_cost → *_bps`; `Fill.yes_price/no_price → *_bps`; `Fill.count → count_fp100`; `Fill.fee_cost → fee_cost_bps` | Validators use Decimal; `_FILL_FP_FIELDS`/`_ORDER_FP_FIELDS` lists updated |
| `src/talos/models/ws.py` | `OrderBookSnapshot` / `OrderBookDelta.price/delta` → `_bps`/`_fp100`; `TickerMessage.yes_bid/yes_ask/no_bid/no_ask/last_price/volume` → `*_bps` / `volume_fp100`; `FillMessage` + `UserOrderMessage` parallel to `Fill`/`Order` | `100 - yes_ask` complement calls replaced with `complement_bps(yes_ask_bps)` |
| `src/talos/models/market.py` | `Market` price fields → `_bps`; `volume` / `open_interest` → `_fp100`; `data["price"] = round(p * 100)` → `dollars_str_to_bps(p)` | |
| `src/talos/models/portfolio.py` | `Position.position` → `position_fp100`; `total_traded` / `market_exposure` → `_bps` | |
| `src/talos/orderbook.py` | `OrderBookLevel.price` → `price_bps`; `quantity` → `quantity_fp100`; derivation comment `100 - level.price` → `complement_bps(level.price_bps)` | |
| `src/talos/scanner.py` | `no_a.price + no_b.price` arithmetic in bps; `raw_edge = complement_bps(pa + pb)` | Lines 125, 157, 182 |
| `src/talos/fees.py` | All `no_price: int` → `no_price_bps: int`; rewrite formulas to bps space (`100 - X` → `complement_bps(X)`, trailing `/ 100` → `/ ONE_DOLLAR_BPS`); `scenario_pnl` uses `filled_*_fp100` and returns `net_*_bps`; `fee_adjusted_cost` returns bps | Core rewrite — 14+ touch sites |
| `src/talos/position_ledger.py` | `filled_count → filled_count_fp100`; `filled_total_cost → filled_total_cost_bps`; `filled_fees → filled_fees_bps`; `closed_count → closed_count_fp100`; all pair variants; `resting_price → resting_price_bps`; `resting_count → resting_count_fp100`; `record_fill(count_fp100, price_bps, *, fees_bps=0)`; `avg_filled_price` returns bps; `_reconcile_closed` matches on fp100 granularity | Money-critical: `safety-audit` + `position-scenarios` skills run after |
| `src/talos/bid_adjuster.py` | All `*_price` parameters → `*_price_bps`; ledger reads return bps; `new_price: int` → `new_price_bps: int`; profit-comparison formatter uses `format_bps_as_cents` | |
| `src/talos/opportunity_proposer.py` | `edge_threshold_cents` config stays named; converts via `cents_to_bps` at module entry; internal comparisons in bps | Operator input stays in cents — renaming would silently redefine operator intent |
| `src/talos/rebalance.py` | Analogous to bid_adjuster | |
| `src/talos/engine.py` | Ledger reads in bps; revenue calc `filled * 100` → `filled_fp100 * ONE_CENT_BPS / ONE_CONTRACT_FP100 * CENTS_PER_PAYOUT` (or equivalent clean form using helpers); log-line formatters use `format_bps_as_*` | |
| `src/talos/cpm.py` | Display-only in cents currently; convert from bps at entry | |
| `src/talos/settlement_tracker.py` | `min(yes_count, no_count) * 100` → `min(yes_fp100, no_fp100) * ONE_CENT_BPS / ONE_CONTRACT_FP100` per payout-in-bps convention | |
| `src/talos/rest_client.py` | See Section 4 | |

**What explicitly does NOT change:**

- `src/talos/automation_config.py` field names: `edge_threshold_cents`, `min_edge_cents`, etc. Operator config stays in cents. Conversion to bps happens at the first internal consumer. Renaming would silently redefine operator-supplied values.
- `src/talos/ui/**`: display files stay in cents / dollars for output, convert **from** bps at the display boundary using `units.py` formatters. Internal UI state (e.g., textual column widths) is unaffected.
- `src/talos/persistence.py` cache keys and file paths — see Section 7 for the versioned-loader addition.
- Any file outside `src/talos/` (tools, tests, brain).

### Section 6 — Display layer

All files under `src/talos/ui/` currently do inline `/ 100` and `:.2f` on price/cost/pnl variables. This is migrated to use `units.py` formatters only:

| Display file | Inline pattern today | Replacement |
|---|---|---|
| `ui/widgets.py` | `f"${cents / 100:,.2f}"` (many) | `format_bps_as_dollars_display` / table formatter helper |
| `ui/screens.py` | `f"${total_cost / 100:.2f}"`, `f"${fee_profit / 100:.2f}"` | same |
| `ui/event_review.py` | `f"${pnl / 100:.2f}"` (many); `gross_edge = 100 - combined` | formatter + `complement_bps` |
| `ui/app.py` | `f"${disc['our_revenue'] / 100:.2f}"` | formatter |
| `ui/tree_screen.py` | `elapsed_ms` multiplications — NOT money, keep as-is | allowlist: this is time, not money |

`cpm.py:37` (`text = f"{value:.2f}"`) receives a pre-computed ratio, not a price/cost — allowlisted.

### Section 7 — Persistence (versioned ledger loader)

The persisted ledger envelope gains `schema_version` and `legacy_migration_pending` metadata. The envelope semantics distinguish *"v2 data confirmed by an authoritative source"* from *"v2 data derived from a legacy-cents snapshot that has not yet been reconciled."* The distinction is load-bearing: without it, a restart can launder a guessed legacy conversion into a v2 envelope that looks indistinguishable from ground truth on the next restart.

#### v1 → v2 key map (explicit)

Current v1 payload (from `src/talos/position_ledger.py:to_save_dict`, lines 406–429):

| v1 key | v2 key | Scale factor | Type |
|---|---|---|---|
| `filled_a` | `filled_count_fp100_a` | ×100 | int |
| `cost_a` | `filled_total_cost_bps_a` | ×100 | int |
| `fees_a` | `filled_fees_bps_a` | ×100 | int |
| `filled_b` | `filled_count_fp100_b` | ×100 | int |
| `cost_b` | `filled_total_cost_bps_b` | ×100 | int |
| `fees_b` | `filled_fees_bps_b` | ×100 | int |
| `closed_count_a` | `closed_count_fp100_a` | ×100 | int |
| `closed_total_cost_a` | `closed_total_cost_bps_a` | ×100 | int |
| `closed_fees_a` | `closed_fees_bps_a` | ×100 | int |
| `closed_count_b` | `closed_count_fp100_b` | ×100 | int |
| `closed_total_cost_b` | `closed_total_cost_bps_b` | ×100 | int |
| `closed_fees_b` | `closed_fees_bps_b` | ×100 | int |
| `resting_id_a` | `resting_id_a` | — | str \| None |
| `resting_count_a` | `resting_count_fp100_a` | ×100 | int |
| `resting_price_a` | `resting_price_bps_a` | ×100 | int |
| `resting_id_b` | `resting_id_b` | — | str \| None |
| `resting_count_b` | `resting_count_fp100_b` | ×100 | int |
| `resting_price_b` | `resting_price_bps_b` | ×100 | int |

The conversion is **exactly** `×100` for every numeric field. Contract counts go from integer contracts to fp100 (one contract = 100 fp100). Money values go from integer cents to bps (one cent = 100 bps). Same scale factor, different unit.

#### v2 envelope format (per-pair, nested inside the shared `games_full.json` entry)

**Reconciled (normal case):**
```json
{
  "schema_version": 2,
  "legacy_migration_pending": false,
  "ledger": {
    "filled_count_fp100_a": 689,
    "filled_total_cost_bps_a": 35738,
    "filled_fees_bps_a": 0,
    "closed_count_fp100_a": 500,
    "closed_total_cost_bps_a": 26500,
    "closed_fees_bps_a": 0,
    "filled_count_fp100_b": 500,
    "filled_total_cost_bps_b": 22000,
    "filled_fees_bps_b": 0,
    "closed_count_fp100_b": 500,
    "closed_total_cost_bps_b": 22000,
    "closed_fees_bps_b": 0,
    "resting_id_a": null,
    "resting_count_fp100_a": 0,
    "resting_price_bps_a": 0,
    "resting_id_b": "abc-123",
    "resting_count_fp100_b": 100,
    "resting_price_bps_b": 600
  }
}
```

**Unreconciled (post-migration, pre-reconcile):**
```json
{
  "schema_version": 2,
  "legacy_migration_pending": true,
  "ledger": {
    "filled_count_fp100_a": 100,
    "filled_total_cost_bps_a": 6000,
    ...(converted v2 values — the ×100 scale-up of v1)
  },
  "legacy_v1_snapshot": {
    "filled_a": 1,
    "cost_a": 60,
    "fees_a": 0,
    ...(original v1 payload, unmodified, complete)
  }
}
```

The `legacy_v1_snapshot` field carries the original v1 payload verbatim until reconcile clears the flag. When reconcile succeeds, the field is dropped and `legacy_migration_pending` flips to `false` on the next save.

#### Why the embedded legacy blob (design reason)

Talos's existing persistence writer (`src/talos/__main__.py:_persist_games`, lines 403–440) rewrites the entire `games_full.json` snapshot on every save by walking live pair objects and nesting `entry["ledger"] = ledger.to_save_dict()` per game. There is no per-pair file or sidecar.

Preserving v1 data outside the main envelope would require one of:
- **Stop rewriting `games_full.json`** while any pair is unreconciled — loses durability for unrelated `engine_state` / resting-order / discovery state. Rejected.
- **Write a sidecar file per unreconciled pair** — introduces a second persistence contract, doubles the atomic-write surface area, and creates a new class of load-ordering bugs. Rejected.
- **Embed the legacy blob inside the per-pair ledger payload** — the shared writer continues operating on one file, and the legacy blob rides along until dropped. **Chosen.** Minimal architectural impact, no new files, no new race conditions.

#### Load path — `position_ledger.py:seed_from_saved` (v2 semantics)

All load paths set the appropriate staleness flags based on which kinds of state loaded:

**`stale_fills_unconfirmed = True`** iff any *historical* field is nonzero:
- `filled_count_fp100` on either side — affects matched pairs, locked P&L, exposure math.
- `filled_total_cost_bps` or `filled_fees_bps` — affects HOLD_UNPROFITABLE gates and economics decisions.
- `closed_count_fp100` / `closed_total_cost_bps` / `closed_fees_bps` — closed-bucket state drives the open-unit avg scoping.

**`stale_resting_unconfirmed = True`** iff any *resting* field is nonzero/non-null:
- `resting_count_fp100` > 0 — affects `is_placement_safe` / `total_committed` math.
- `resting_order_id` is not None — indicates a specific order Talos believes is live on Kalshi.

The split exists because the two kinds of state have different authoritative sources: fills endpoint confirms historical executions (but can't tell you if an order is currently resting); orders endpoint confirms live resting state (by reading the orders list directly). Conflating them — which earlier drafts of this spec did — leads to gates that clear on incomplete evidence.

A ledger with all-zero persisted state is effectively fresh and both staleness flags stay unset.

1. If payload is `None`, no-op. All flags stay False.
2. If `schema_version` is absent or `< 2` → **bare legacy cents payload** (pre-migration file on disk from before Phase 1+2 lands). Apply the v1→v2 key-map table (×100). Apply the staleness rule above. Set `legacy_migration_pending = True` **iff the v1 payload contained any nonzero safety-relevant field** (i.e., iff either staleness flag was set by the staleness rule). If the v1 payload was all-zero, the conversion is trivial — no legacy reconciliation is needed, `legacy_migration_pending` stays False, and the next save writes a clean v2 envelope with no `legacy_v1_snapshot` field. When `legacy_migration_pending` is set, retain the original v1 payload as an in-memory `legacy_v1_snapshot` so the next save embeds it in the v2 envelope.
3. If `schema_version == 2` and `legacy_migration_pending == True` → load converted values into the ledger, retain `legacy_v1_snapshot` in memory for next save, and carry `legacy_migration_pending = True`. Apply the staleness rule above. Gate stays blocked per Section 8 until reconcile clears the flags.
4. If `schema_version == 2` and `legacy_migration_pending == False` → load directly. Apply the staleness rule. If all safety-relevant state is zero, both staleness flags stay False and `ready()` only waits on `_first_orders_sync.is_set()` per Section 8.
5. If `schema_version > 2` → raise. No downgrade path.

**F22 rationale:** previously `legacy_migration_pending` was set for every v1 load, including all-zero payloads. But auto-reconcile only runs when `stale_fills_unconfirmed` is set. All-zero v1 → no staleness → auto-reconcile never fires → `legacy_migration_pending` never clears → gate permanently blocked. The fix gates legacy_migration_pending on the same condition that would make reconcile do useful work — nonzero state. Zero-state v1 ledgers convert trivially and don't need reconciliation.

Note the on-disk envelope does NOT carry `stale_fills_unconfirmed` or `stale_resting_unconfirmed` — they are always False when the envelope was written (the ledger had to be ready to be saving) and are always recomputed fresh on load from the loaded state. `reconcile_mismatch_pending` also does not persist — see Section 8a for rationale (re-fetchable in-session state, not a durable contract). Only `legacy_migration_pending` persists on disk, because it can legitimately remain True across restarts (operator may close Talos without completing reconcile).

#### Save path

- **`legacy_migration_pending == False`** → write v2 envelope with `legacy_migration_pending: false`, no `legacy_v1_snapshot` field.
- **`legacy_migration_pending == True`** → write v2 envelope with `legacy_migration_pending: true` AND `legacy_v1_snapshot` populated from the in-memory retained blob. The shared `games_full.json` writer continues operating normally; only this per-pair nested blob changes shape.

This is the key rewrite: the "DO NOT TOUCH v1 envelope" rule from v3 of this spec is removed entirely. The shared snapshot writer never has to split its behavior per-pair. The legacy data is preserved as a subfield of the normal v2 envelope until reconciled.

#### Confirmation sources (which flags each source clears)

**Core principle: confirmation must cover every safety-relevant field, not just counts, AND the source must be appropriate for the kind of state.** Fills endpoint confirms history; orders endpoint confirms live resting. A single source cannot replace the other.

| Authoritative source | Clears `stale_fills_unconfirmed`? | Clears `stale_resting_unconfirmed`? | Clears `legacy_migration_pending`? | Trigger |
|---|---|---|---|---|
| `sync_from_orders` — any completed response (including empty) | No | ✅ **Yes** — empty response authoritatively zeroes resting state per sync_from_orders' existing contract | No | Any successful poll response |
| `sync_from_orders` — response with ≥1 matching order | No | ✅ Yes | No | Does NOT clear fills/legacy — response aggregates only non-archived orders, so "counts agree" proof is architecturally incomplete for historical economics (see F20 rationale below) |
| `sync_from_positions` | No | No | No | Purely a count-helper for the existing monotonic-increase rule; does NOT open the gate |
| `reconcile_from_fills` → OK outcome | ✅ Yes | No (fills can't confirm live resting) | ✅ Yes | Successful paginated fills rebuild with no mismatch |
| `reconcile_from_fills` → MISMATCH outcome | No | No | No | Fills rebuild differs from loaded state; awaits operator |
| `accept_pending_mismatch` | ✅ Yes | No (same reason as reconcile_from_fills) | ✅ Yes | Explicit UI action after operator reviews diff |

**Why orders-with-match does NOT clear fills/legacy staleness (F20 rationale):** `sync_from_orders` aggregates `fill_count`, `maker_fill_cost`, and `taker_fill_cost` only from the orders returned in the current poll response. Kalshi's orders endpoint archives older filled/cancelled orders — they don't appear in typical polls. If counts happen to agree between the current poll and the loaded ledger, that agreement could be spurious (archived orders not contributing). The costs in the response are from visible orders only; the ledger's historical costs came from previous polls that may have seen different orders. "Counts agree" is not the same as "historical economics are correct." The only archival-immune source is `/portfolio/fills`, which never archives, so `reconcile_from_fills` is the only automatic source that can prove the loaded historical state is authoritative. This makes the gate slightly more conservative — every restart with `stale_fills_unconfirmed` waits for auto-reconcile to run at 5s — but eliminates a silent-corruption path.

**How the gate opens for any seeded pair:** `sync_from_orders` clears `stale_resting_unconfirmed` within <1s (any response, including empty). `reconcile_from_fills` clears `stale_fills_unconfirmed` + `legacy_migration_pending` at ~5s via auto-reconcile. Gate opens once all the flags a pair had set at load have been cleared. Operator intervention only on mismatch.

**`resting_*` fields are advisory regardless of flag state.** On restart, first authoritative sync overrides them. The loader's job is matched-pair state preservation; resting state is always re-fetched. The staleness flag (set on nonzero resting state at load) is what blocks the gate during the advisory-overwrite window.

#### Self-heal summary

| Pair type | Any order state |
|---|---|
| Cross-ticker | ✅ auto-reconcile at 5s (fills endpoint, archival-immune) |
| Same-ticker | ✅ auto-reconcile at 5s (fills endpoint, archival-immune) |

All cases self-heal without operator action under normal conditions via the same path: fills reconcile at 5s. The pair-shape and order-liveness distinctions that earlier drafts of this spec baked into the gate logic turned out to be footguns — orders-with-match was never strong enough proof of historical economics (F20), and positions-API is count-only. Fills is the only archival-immune, economics-complete source, so it's the single path for automatic healing. Operator action required only on mismatch between persisted state and Kalshi fills.

The stranded 1.89 YES on MARJ (same-ticker, possibly archived when the operator runs the migration): auto-reconcile at 5s hits the fills endpoint, fetches the authoritative 1.89-contract fill record (189 fp100 / ~9240 bps), and heals. Operator involvement only if Kalshi's fills endpoint reports something different — e.g., if additional fills landed between the operator's last Talos-recorded state and the migration.

#### No bulk backfill of historical data

Closed-event snapshots outside the currently-tracked pair set are not migrated. Any still-tracked pair goes through the versioned loader above.

### Section 8 — Startup safety gate (unified state model)

Block all **risk-increasing** money-touching actions (`create_order`, `amend_order`) until the ledger is **confirmed** — meaning any nonzero state carried forward from persistence has been verified against the appropriate authoritative Kalshi source this session. Per-pair, not global.

**`cancel_order` is explicitly NOT gated (F31 fix).** Cancel is the operator's risk-reducing fail-safe — it pulls outstanding resting orders, reducing exposure and time-in-market. Blocking cancel during an unconfirmed/reconcile-mismatch state makes things worse: it traps the operator with potentially-wrong resting orders exactly when state is least trustworthy. Multiple existing cleanup paths rely on cancel being always-available — `engine.py`'s amend failure rollback (`src/talos/engine.py:~2912`, `~3945`), rebalance cleanup (`src/talos/rebalance.py:~728`, `~755`, `~832`), and operator-initiated UI cancels.

**Cancel path authoritative source:** cancels must use Kalshi's live-orders endpoint as the source of truth, not the potentially-stale ledger `resting_order_id`. Specifically, before issuing a cancel:

1. Fetch the live order by ID from Kalshi (`GET /portfolio/orders/{order_id}`).
2. If the order exists and is still resting, issue the cancel.
3. If the order does not exist or is already cancelled, update the ledger's resting state to match Kalshi and skip the cancel (no-op).
4. If the fetch fails (network), fall back to attempting the cancel with the ledger's stored `resting_order_id`; a 404 response means the order is already gone; log and move on.

The existing ledger `resting_order_id` is used as a hint but not trusted. This preserves the cancel fail-safe property even when the ledger is stale-from-persistence or has a pending mismatch.

**Design principle:** the gate protects against acting on stale loaded state, regardless of whether the staleness came from a legacy migration (v1 load) or an ordinary restart (v2 load with persisted counts that haven't been re-confirmed since Talos was offline). The rule is factored by which *kind* of persisted state needs confirmation — fills/costs/fees vs live resting orders — because the authoritative source differs (fills endpoint confirms historical executions; orders endpoint confirms live resting state).

**Flags on each ledger:**

```python
class PositionLedger:
    # True when: seed_from_saved loaded any nonzero historical state
    # (filled_count_fp100, filled_total_cost_bps, filled_fees_bps, any
    # closed_* field) AND that state hasn't been confirmed this session.
    # Cleared by: reconcile_from_fills OK, OR accept_pending_mismatch.
    # NOT cleared by sync_from_orders, even with a matching response —
    # orders-endpoint data is archival-incomplete for historical economics
    # (see Section 7 F20 rationale + confirmation-sources table).
    stale_fills_unconfirmed: bool = False

    # True when: seed_from_saved loaded nonzero resting state
    # (resting_count_fp100 > 0 OR resting_order_id is not None) AND that
    # state hasn't been confirmed this session. Cleared by: any
    # sync_from_orders completion (including empty response — empty means
    # no live orders, so the ledger's resting state gets authoritatively
    # zeroed by sync_from_orders per its existing contract).
    stale_resting_unconfirmed: bool = False

    # True when: seed_from_saved applied a v1→v2 conversion (legacy cents
    # payload) OR loaded a v2 envelope with legacy_migration_pending: true.
    # Drives UX escalation (mandatory reconcile banner) but shares the
    # clearing mechanism with stale_fills_unconfirmed.
    legacy_migration_pending: bool = False

    # In-session only: set when reconcile_from_fills detected a mismatch
    # and retained the rebuilt state in _pending_mismatch for operator
    # review. NOT persisted across restart — after a crash, the operator
    # re-invokes reconcile, which re-fetches fills and re-detects mismatch
    # if it's still real. See Section 8a for rationale.
    reconcile_mismatch_pending: bool = False

    def ready(self) -> bool:
        return not (
            self.stale_fills_unconfirmed
            or self.stale_resting_unconfirmed
            or self.legacy_migration_pending
            or self.reconcile_mismatch_pending
        )
```

**Why the fills/resting split:** fills tell you history (what has executed). Orders tell you present (what is currently resting). A fills-based rebuild can reconstruct fill counts, costs, fees, and closed-state accurately, but cannot tell you whether the `resting_order_id="abc-123"` Talos persisted is still live on Kalshi — that order might have been cancelled, expired, or amended while Talos was offline. Only `sync_from_orders` — which reads the live orders endpoint — can confirm or refute persisted resting state. Conflating the two sources leads to ledgers that look "confirmed" but carry stale reservations, letting `is_placement_safe` permit duplicate or oversized orders.

**Startup sequence per pair:**

1. `seed_from_saved` loads persisted state into the ledger and sets the appropriate flags:
   - `stale_fills_unconfirmed = True` if any historical field is nonzero (see Section 7).
   - `stale_resting_unconfirmed = True` if resting_count is nonzero or resting_order_id is non-null.
   - `legacy_migration_pending = True` if (a) v2 envelope carried the flag, OR (b) v1 conversion was applied AND the converted payload contained nonzero safety-relevant state (F22). Zero-state v1 payloads convert trivially and do NOT set this flag — otherwise the gate permanently blocks ledgers that had nothing to reconcile. See Section 7 load-path step 2 for the authoritative rule.
2. First `sync_from_orders` runs. **Always** clears `stale_resting_unconfirmed` on completion (any response; empty response authoritatively zeroes the ledger's resting state via sync_from_orders' existing contract). Does NOT clear `stale_fills_unconfirmed` or `legacy_migration_pending` even on matching-order responses — those flags require archival-immune confirmation (see Section 7 F20 rationale).
3. `sync_from_positions` may run (cross-ticker only; same-ticker early-returns) and may update counts via the monotonic-increase rule, but **does NOT clear any staleness flag on its own** — positions data is count-only and doesn't confirm cost/fee authoritative state, and doesn't confirm resting orders.
4. If `stale_fills_unconfirmed` is still set after `AUTO_RECONCILE_DELAY_S` (5s) → **auto-invoke `reconcile_from_fills`** unconditionally. Orders-with-match no longer bypasses this step. Covers same-ticker and cross-ticker uniformly. See Section 8a.
5. If fills reconcile succeeds → `stale_fills_unconfirmed` and `legacy_migration_pending` clear. **`stale_resting_unconfirmed` does NOT clear** from fills reconcile — it requires `sync_from_orders` completion. In practice these overlap: if the operator's Talos has been up long enough to complete one orders poll, both are already resolved by the time fills reconcile finishes.
6. If fills reconcile detects a mismatch → `reconcile_mismatch_pending = True`, UI surfaces diff; operator must explicitly accept the rebuild or manually resolve. Gate stays closed until operator acts.
7. If fills reconcile encounters a pagination or network error → error logged, UI surfaces retry action. Gate stays closed.

**Fresh pairs** (no persisted state, or all-zero persisted state): `stale_fills_unconfirmed` and `stale_resting_unconfirmed` both start False. `ready()` still gates on a minimum of `_first_orders_sync` completion (any response) — this covers the narrow case where Talos crashed mid-place-order, didn't persist the new resting order, and restarts with pristine persistence but Kalshi knows about the order. One orders sync resolves it. This is the v5 "fresh pairs need one sync" rule preserved. No auto-reconcile, no bootstrap deadlock, no "waiting for a prior matching order."

```python
def ready(self) -> bool:
    if self.stale_fills_unconfirmed:          return False
    if self.stale_resting_unconfirmed:        return False
    if self.legacy_migration_pending:         return False
    if self.reconcile_mismatch_pending:       return False
    return self._first_orders_sync.is_set()   # fresh pair minimum gate
```

**Engine-side gate at every risk-increasing call site (`create_order`, `amend_order` only — NOT `cancel_order`):**

```python
# Wrap at create_order / amend_order entry points:
deadline = time.monotonic() + STARTUP_SYNC_TIMEOUT_S
while not ledger.ready():
    if ledger.legacy_migration_pending or ledger.reconcile_mismatch_pending:
        # Neither clears on its own; operator must act via the UI.
        self._notify(
            f"Confirm or reconcile {pair.name} before create/amend can proceed",
            "error",
        )
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        self._notify(f"Confirmation pending for {pair.name} — create/amend blocked", "error")
        return
    await asyncio.sleep(min(0.2, remaining))
```

**Cancel path wrapper (always allowed, authoritative-resync-before-clear):**

```python
# Wrap at cancel_order entry points:
async def cancel_order_with_verify(self, order_id: str, pair: ArbPair) -> None:
    """Fail-safe cancel. Always allowed regardless of ledger.ready() state.

    IMPORTANT: a 404 on a single order ID does NOT prove the side has zero
    resting exposure. PositionLedger stores only the first resting_order_id
    per side but supports multiple live orders on Kalshi. A stale first ID
    disappearing might mean:
      (a) That one order was cancelled/filled; other orders may still exist.
      (b) All orders on that side are gone.
    The only way to tell is an authoritative sync_from_orders for the pair.
    See F33 fix.
    """
    try:
        live = await self._rest.get_order(order_id)
    except KalshiNotFoundError:
        # The specific stored order is gone. Do NOT clear ledger locally yet.
        # Force a pair-level orders resync; only after the resync does the
        # ledger reflect authoritative resting state.
        await self._resync_pair_orders(pair)
        return
    except (KalshiAPIError, httpx.HTTPError):
        # Network error — fall through to attempted cancel. A subsequent 404
        # from the cancel is handled the same way: resync, don't blindly clear.
        live = None

    if live is not None and live.status not in ("resting", "executed"):
        # Order exists but is in a terminal state. Resync to refresh the
        # ledger's whole-side view, not just this one order.
        await self._resync_pair_orders(pair)
        return

    # Issue the cancel. Cancel is risk-reducing, never blocked by the
    # confirmation gate (F31).
    try:
        await self._rest.cancel_order(order_id)
    except KalshiNotFoundError:
        # Race: order went terminal between get_order and cancel_order.
        # Handled the same way — resync, don't blindly clear.
        await self._resync_pair_orders(pair)
        return

    # Successful cancel. Still resync rather than optimistically updating
    # the ledger's single-ID state — the side may have other live orders.
    await self._resync_pair_orders(pair)
```

`_resync_pair_orders(pair)` fetches the pair's active orders (both tickers for cross-ticker; single ticker for same-ticker), calls `ledger.sync_from_orders(orders, ticker_a=..., ticker_b=...)` which is the existing authoritative resting-state path. Any live orders still on the side are preserved; the gone order is dropped; counts reflect Kalshi truth.

**Why this matters:** a stale first-ID 404 that blindly cleared `resting_count_fp100` and `resting_order_id` would underreport committed inventory on that side. Safety math (`is_placement_safe`, `total_committed`) would then permit new orders that exceed unit size, causing oversized positions. The F33 resync rule prevents that.

**Exhaustive caller migration (F36 fix).** The "route through the wrapper" rule is only safe if EVERY existing direct `rest.cancel_order(...)` call site is migrated. Incomplete migration leaves stale-ID paths that bypass F33 resync. Enumeration of current raw call sites (verified on HEAD 2026-04-21):

| File | Line | Current call | Migration target |
|---|---|---|---|
| `src/talos/engine.py` | ~531 | `await self._rest.cancel_order(order_id)` (startup-cleanup of orphaned orders) | `await self.cancel_order_with_verify(order_id, pair)` |
| `src/talos/engine.py` | ~2912 | `await self._rest.cancel_order(order_a.order_id)` (amend-fail rollback) | `await self.cancel_order_with_verify(order_a.order_id, pair)` |
| `src/talos/engine.py` | ~3945 | `await self._rest.cancel_order(order_id)` (queue-improve amend rollback) | `await self.cancel_order_with_verify(order_id, pair)` |
| `src/talos/rebalance.py` | ~728 | `await rest_client.cancel_order(primary_order_id)` (rebalance primary cancel) | Route via engine wrapper — inject/pass the engine reference or use a shared cancel helper |
| `src/talos/rebalance.py` | ~755 | `await rest_client.cancel_order(order.order_id)` (rebalance cleanup) | Same |
| `src/talos/rebalance.py` | ~832 | `await rest_client.cancel_order(order.order_id)` (rebalance cleanup) | Same |

The `src/talos/rest_client.py:~342` definition of `cancel_order` itself stays — the grep rule below excludes the definition line.

**Grep-based regression guard** (added to `tests/test_unit_discipline.py` or a sibling):

```python
# tests/test_cancel_discipline.py

import ast
import pathlib

ALLOWED_CALLERS = {
    # The wrapper itself is allowed to call rest.cancel_order directly.
    ("src/talos/engine.py", "cancel_order_with_verify"),
    # The REST client's own definition site.
    ("src/talos/rest_client.py", "cancel_order"),
}

def test_no_raw_rest_cancel_order_calls():
    """Every rest.cancel_order() call must go through
    engine.cancel_order_with_verify() (F33 resync + F31 gate carve-out).

    Direct callers outside the wrapper re-introduce the stale-first-ID
    hole: a 404 on the stored order_id can clear resting state even
    when other live orders exist on the same side.
    """
    for py in pathlib.Path("src/talos").rglob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "cancel_order":
                continue
            # Check the enclosing function name — allow the wrapper
            # and the rest_client definition.
            enclosing = _enclosing_func(tree, node)
            key = (str(py.relative_to(".")), enclosing)
            if key in ALLOWED_CALLERS:
                continue
            raise AssertionError(
                f"{py}:{node.lineno}: direct rest.cancel_order() call in "
                f"{enclosing} — use engine.cancel_order_with_verify() instead"
            )
```

This test fails at the migration-incomplete state and passes only when all six callers have been migrated. Any future raw cancel added outside the wrapper also fails.

- `STARTUP_SYNC_TIMEOUT_S`: 30s. Ordinary confirmation path (orders sync + optional auto-reconcile) should complete within this window.
- `AUTO_RECONCILE_DELAY_S`: 5s. Time between first sync attempts and auto-invoking fills reconcile. Tuned so the cheap orders/positions path has time to clear the flag without auto-reconcile round-tripping the fills endpoint unnecessarily.
- Operator-action states (`legacy_migration_pending`, `reconcile_mismatch_pending`) escape the loop early with a notification — no point spinning, they don't self-clear.

**Startup scenarios this gate handles:**

| Scenario | `stale_fills` | `stale_resting` | `legacy_pending` | Resolution path | Typical latency |
|---|---|---|---|---|---|
| Fresh pair, no history | F | F | F | First orders sync completes | <1s |
| Routine restart, v2 ledger, only historical state (no resting) | T | F | F | Auto fills reconcile at 5s | ~5–10s |
| Routine restart, v2 ledger, with resting orders | T | T | F | Orders sync clears resting (~<1s); fills reconcile clears fills (~5s) | ~5–10s |
| Routine restart, v2 ledger, only resting no fills | F | T | F | Orders sync completes | <1s |
| Migration restart, v1 ledger, any archival state | T | T if had resting | T | Orders sync clears resting; fills reconcile clears fills+legacy | ~5–10s |
| Reconcile detects mismatch | T retained | Varies | T retained | Operator reviews diff → accepts → flags clear + durable persist | Operator-driven |

**Uniform confirmation path for fills/legacy:** every pair with `stale_fills_unconfirmed` goes through fills reconcile at 5s. No fast-path via orders-with-match because orders cannot prove historical economics are authoritative when archival is possible (see Section 7 F20 rationale). The 5s startup-gate cost applies to every seeded pair with historical state. Given Talos already waits for market-data and WS connections on startup, this adds no user-visible latency beyond what already exists.

**Why resting staleness clears fast:** resting is confirmed by any `sync_from_orders` response (including empty) because sync_from_orders reads the authoritative live-orders list. Fills staleness requires fills reconcile (archival-immune) — slower but always correct.

#### Section 8a — Fills-based reconcile (authoritative recovery path)

**Paginator contract (added in Phase 1+2):**

```python
# src/talos/rest_client.py

@dataclass(frozen=True, slots=True)
class FillsPage:
    fills: list[Fill]
    cursor: str | None   # None iff this is the last page

async def get_fills_page(
    self,
    *,
    ticker: str | None = None,
    order_id: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> FillsPage:
    """Single page of fills with next-page cursor.

    Unlike the hot-path `get_fills` (which drops the cursor), this returns
    the structured page so reconcile can exhaust the cursor chain.
    """
    params: dict[str, Any] = {"limit": limit}
    if ticker:    params["ticker"] = ticker
    if order_id:  params["order_id"] = order_id
    if cursor:    params["cursor"] = cursor
    data = await self._request("GET", "/portfolio/fills", params=params)
    next_cursor = data.get("cursor") or None  # Kalshi returns "" on last page
    return FillsPage(
        fills=[Fill.model_validate(f) for f in data["fills"]],
        cursor=next_cursor,
    )

async def get_all_fills(
    self,
    *,
    ticker: str | None = None,
    order_id: str | None = None,
) -> list[Fill]:
    """Exhaust all pages of fills. Raises on pagination error.

    Used only by the reconcile path. The hot path uses `get_fills` which
    returns one page and is sufficient for streaming new-fill detection.
    """
    all_fills: list[Fill] = []
    cursor: str | None = None
    pages = 0
    while True:
        page = await self.get_fills_page(ticker=ticker, order_id=order_id, cursor=cursor)
        all_fills.extend(page.fills)
        pages += 1
        cursor = page.cursor
        if cursor is None:
            break
        if pages > MAX_FILLS_PAGES:     # sanity guard; defaults to 100 pages / 10k fills
            raise KalshiAPIError("get_all_fills exceeded MAX_FILLS_PAGES — abort reconcile")
    logger.info("get_all_fills_complete", ticker=ticker, pages=pages, fills=len(all_fills))
    return all_fills
```

**Existing `get_fills` remains unchanged** (returns `list[Fill]`, first page only) for backward-compat with hot-path callers that don't need pagination. The reconcile path always uses `get_all_fills`.

**Test preconditions for clearing flags (mandatory in `test_position_ledger_migration.py`):**

1. `get_all_fills` was called (not `get_fills`). Verified by mock spy on the REST client.
2. The cursor chain was exhausted — the mock returns a multi-page response and the test asserts all pages were consumed before the flag clears.
3. On a mid-chain pagination error, the mock raises; the test asserts the flag stays set and `reconcile_from_fills` raises.

**Durability contract (F13 + F16 + v11 simplification):**

Reconcile and accept paths must resolve two concerns:

1. **F13 — durable before success.** On OK outcome, both `reconcile_from_fills` and `accept_pending_mismatch` call `persist_cb` synchronously before returning. `persist_cb` writes `games_full.json` via atomic temp+rename. A crash after the function returns has the rebuilt state on disk. A crash during or before the write has no mutation applied in memory either (apply happens AFTER persist; if persist raises, apply is skipped and ERROR returns).
2. **F16 — mismatch state is NOT crash-durable.** Discarded on restart; operator re-invokes reconcile. Fills are authoritative and re-fetchable.

**No concurrency protocol beyond what the event loop already provides.** All ledger mutators are sync — they run atomically relative to other coroutines. The reconcile/accept mutation phase is a single sync block (no `await` inside). Other coroutines cannot interleave. This is the sync-mutator-under-single-event-loop-asyncio pattern Talos already uses everywhere; the migration does not change it.

Earlier spec drafts (v6–v10) added an async-lock protocol to guard against cross-coroutine interleaving during `await persist_cb`. Moving `persist_cb` to sync eliminates the `await`, eliminates the interleaving window, and eliminates the need for locks. This is the v11 simplification — the same safety property with less machinery.

The only runtime state beyond per-ledger fields is `_mutation_generation: int`, a plain counter incremented by sync mutators. Used by `accept_pending_mismatch` to reject stale pending mismatches (F11 staleness detection) without requiring any lock.

**Detached-snapshot pattern (F18):**

```python
@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    """Immutable full-state snapshot for the persistence envelope.

    Built by _snapshot_with_rebuild_applied() without touching the live
    ledger. persist_cb serializes and writes this snapshot to disk. Only
    after persist_cb returns does the snapshot get applied to the live
    ledger via a synchronous apply block.
    """
    # Both sides' historical state and resting state in v2 units.
    filled_count_fp100_a: int
    filled_total_cost_bps_a: int
    filled_fees_bps_a: int
    closed_count_fp100_a: int
    closed_total_cost_bps_a: int
    closed_fees_bps_a: int
    resting_id_a: str | None
    resting_count_fp100_a: int
    resting_price_bps_a: int
    # ... parallel for side b ...
    legacy_migration_pending: bool
    # (no stale_* fields, no reconcile_mismatch_pending — those are in-memory only)
```

**Concurrency model (simplified from v10; rationale below):**

Talos runs on a single asyncio event loop. Every ledger mutator (`record_fill`, `record_resting`, `record_placement`, `record_cancel`, `sync_from_orders`, `sync_from_positions`, `seed_from_saved`) is a **synchronous** Python function. Sync functions run atomically with respect to other coroutines because they contain no `await` — the event loop cannot interrupt them.

This property — **sync-mutator atomicity under single-event-loop asyncio** — is already how Talos works today and what the existing ledger API assumes. The migration preserves it.

**The only concurrency primitive required:**

```python
class PositionLedger:
    # Monotonically increasing counter. Incremented by every sync mutator
    # before returning. Used ONLY by accept_pending_mismatch to detect if
    # the ledger mutated between mismatch detection and operator click
    # (operator may click minutes later; intervening WS fills or sync
    # cycles bump the generation). No lock needed — this is a plain int,
    # incremented and read atomically within sync functions.
    _mutation_generation: int = 0
```

No `asyncio.Lock`. No global persistence lock. No lock acquisition protocol. No timeout guards on lock acquisition.

**Why this works:**

- Ordinary mutators are sync — they run atomically relative to every other coroutine. No coroutine can observe a mid-mutation state.
- `reconcile_from_fills` and `accept_pending_mismatch` are async because they `await` REST fetches. Their mutation phase (build snapshot → check mismatch → write proposed → call persist → apply) is a **single sync block with no await inside**. The entire mutation phase is therefore atomic relative to every other coroutine on the same event loop.
- `persist_cb(proposed, event_ticker)` is SYNC. It reads every active pair's `to_save_dict()` sequentially (atomic because sync), substitutes the proposed snapshot for the calling pair, writes `games_full.json` via atomic temp + rename. Typical duration <10ms for a small file on a local SSD. Blocks the event loop briefly; no other coroutine runs during the write.
- Generation counter handles the one remaining race: between `reconcile_from_fills` detecting a mismatch and the operator clicking "accept" minutes later. Intervening WS fills bump the generation; accept-time check refuses stale mismatches and forces a fresh reconcile.

**What about concurrent reconciles on different pairs?** Reconcile A and reconcile B both fetch fills (async, can interleave at `await rest.get_all_fills(...)`). Each enters its own sync mutation block when fetch completes. The two sync blocks serialize naturally on the event loop — they cannot run simultaneously. If A commits at t=1 and B's rebuild was based on pre-t=1 fills, B's sync block reads the CURRENT ledger state (post-A), compares to B's rebuilt, detects mismatch if any, handles appropriately. No corruption, no deadlock.

**What about local disk write blocking the event loop?** `save_games_full` atomic temp+rename on a local SSD typically takes <10ms. For rare operator-triggered reconciles and startup auto-reconciles, a 10ms event-loop pause is invisible. If disk I/O hangs, that's a system-level problem affecting all Talos persistence (not specific to the migration). Sync persist is the existing Talos pattern — `_persist_games` today is sync.

**What about crash windows?** A mutation that lands in a ledger but hasn't been persisted by the next `_persist_games` cycle IS vulnerable to loss on a crash. This is existing Talos behavior, not something the migration introduces. Recovery path: on next startup, the authoritative Kalshi re-sync (orders + fills reconcile) catches up anything the persisted state missed. Per CLAUDE.md Principle 15, Kalshi is the source of truth; Talos's persisted state is a restart cache, not the primary record.

**What the migration DOES guarantee about durability:**

1. **Successful reconcile is durable.** `reconcile_from_fills → OK` has called `persist_cb` sync before returning. The rebuilt state is on disk.
2. **Operator-accept is durable.** `accept_pending_mismatch` has called `persist_cb` sync before returning.
3. **Legacy conversion is durable on first save.** Legacy v1 → converted v2 state gets persisted on the next scheduled `_persist_games` cycle, with the `legacy_v1_snapshot` blob embedded for rollback.
4. **Mismatch pending state is NOT durable (F16).** Operator re-invokes reconcile after restart; fills are re-fetched; same diff re-emerges if still real.

**What the migration does NOT guarantee:**

- No durability for individual WS fills or sync mutations beyond the existing `_persist_games` scheduler cadence. Kalshi re-sync fills that gap on restart.
- No serialization across concurrent mutators on different ledgers during the write window of a reconcile's persist. Since mutators and persist are all sync and run on one event loop, they serialize naturally — but a mutation that happens BEFORE persist starts may not be in that persist's file snapshot. Again, Kalshi re-sync on next startup resolves.

This is the pragmatic boundary: the migration hardens the new reconcile paths to full durability, and inherits Talos's existing "mostly-durable with Kalshi-sync safety net" behavior for everything else.

**Reconcile procedure (F13 + F16 + F18 + F19):**

```python
async def reconcile_from_fills(
    self,
    rest: KalshiRestClient,
    persist_cb: Callable[[LedgerSnapshot], Awaitable[None]],
) -> ReconcileResult:
    """Authoritative rebuild from per-fill ground truth.

    Invoked by:
      1. Auto-reconcile at startup (Section 8 step 4) for any pair where
         stale_fills_unconfirmed remains set after AUTO_RECONCILE_DELAY_S.
      2. Operator manual action (UI button) for pairs in
         reconcile_mismatch_pending or legacy_migration_pending state.

    F19 concurrency guard: records _mutation_generation at snapshot time;
    before applying, re-acquires the lock and verifies the generation is
    unchanged. If any other mutator touched the ledger between snapshot
    build and apply, returns ERROR so the caller can retry with fresh fills.
    """
    # Step 1: fetch fills. No mutation, no lock.
    try:
        fills_a = await rest.get_all_fills(ticker=self._ticker_a)
        fills_b = [] if self._is_same_ticker else await rest.get_all_fills(ticker=self._ticker_b)
    except KalshiAPIError as exc:
        return ReconcileResult(outcome=ReconcileOutcome.ERROR, error=str(exc))

    rebuilt = self._rebuild_from_fills(fills_a, fills_b)

    # Step 2: mutation phase. Single sync block — no await inside. The entire
    # block runs atomically relative to every other coroutine on the event
    # loop. No locks needed.
    if self._significantly_differs(rebuilt):
        # In-session-only flags — not persisted (F16). Operator re-invokes
        # reconcile after restart if the mismatch was real.
        self._pending_mismatch = rebuilt
        self.reconcile_mismatch_pending = True
        self._pending_mismatch_gen = self._mutation_generation
        logger.warning(
            "reconcile_mismatch",
            event_ticker=self.event_ticker,
            gen=self._mutation_generation,
            loaded_count_a=self.filled_count_fp100(Side.A),
            rebuilt_count_a=rebuilt.filled_count_fp100_a,
            loaded_count_b=self.filled_count_fp100(Side.B),
            rebuilt_count_b=rebuilt.filled_count_fp100_b,
        )
        return ReconcileResult(outcome=ReconcileOutcome.MISMATCH, rebuilt=rebuilt)

    # Detached snapshot build (no live mutation yet).
    proposed = self._snapshot_with_rebuild_applied(
        rebuilt,
        clear_fills_stale=True,
        clear_resting_stale=False,   # fills cannot confirm resting (F17)
        clear_legacy_pending=True,
    )

    # Sync persist. No await. Other coroutines cannot run during this block.
    # If persist_cb raises, no state was ever mutated — return ERROR cleanly.
    try:
        persist_cb(proposed, self.event_ticker)
    except Exception as exc:
        logger.exception("reconcile_persist_failed")
        return ReconcileResult(outcome=ReconcileOutcome.ERROR, error=str(exc))

    # Sync apply. Still no await. Atomic relative to event loop.
    self._apply_snapshot(proposed)
    self._mutation_generation += 1

    logger.info(
        "ledger_reconciled_from_fills",
        event_ticker=self.event_ticker,
        fills_count=len(fills_a) + len(fills_b),
        gen=self._mutation_generation,
    )
    return ReconcileResult(outcome=ReconcileOutcome.OK, rebuilt=rebuilt)
```

**Operator-accept flow (F11 + F13 + F18 + F19):**

```python
async def accept_pending_mismatch(
    self,
    persist_cb: Callable[[LedgerSnapshot], Awaitable[None]],
) -> None:
    """Explicitly apply a previously-detected fills-rebuild.

    Rejects acceptance if the ledger mutated since the mismatch was captured
    (F19). The operator must re-invoke reconcile to see a fresh diff before
    they can accept. This prevents stale rebuilds from clobbering newer
    authoritative state on click.

    In-session only: on restart, pending_mismatch is discarded (F16); the
    operator re-invokes reconcile, gets the current diff.
    """
    # Single sync block. No await inside. Atomic relative to every other
    # coroutine on the event loop.
    if not self.reconcile_mismatch_pending or self._pending_mismatch is None:
        raise RuntimeError("no pending mismatch to accept")

    # Generation check: if the ledger mutated since the mismatch was captured
    # (WS fills or sync polls during the operator's thinking time), reject
    # the accept and force a fresh reconcile.
    if self._mutation_generation != self._pending_mismatch_gen:
        self._pending_mismatch = None
        self.reconcile_mismatch_pending = False
        self._pending_mismatch_gen = -1
        raise StaleMismatchError(
            "pending mismatch is stale — re-run reconcile to see current diff"
        )

    rebuilt = self._pending_mismatch
    proposed = self._snapshot_with_rebuild_applied(
        rebuilt,
        clear_fills_stale=True,
        clear_resting_stale=False,
        clear_legacy_pending=True,
    )

    # Sync persist. If it raises, no state mutated; re-raise for caller.
    persist_cb(proposed, self.event_ticker)

    # Sync apply.
    self._apply_snapshot(proposed)
    self.reconcile_mismatch_pending = False
    self._pending_mismatch = None
    self._pending_mismatch_gen = -1
    self._mutation_generation += 1

    logger.info("mismatch_accepted_by_operator", event_ticker=self.event_ticker)
```

**UI handling of `StaleMismatchError`:** the "Accept Kalshi-fills state" button, on StaleMismatchError, transitions the banner back to "Confirm state with Kalshi" and auto-invokes `reconcile_from_fills` to fetch a fresh diff. The operator sees a new dialog (possibly showing "no mismatch — applied automatically" if the race resolved favorably, or a new diff if it didn't).

**Why the detached-snapshot pattern is safer than rollback (F18 rationale):**

The v6 pattern was: mutate live ledger → await persist → on failure, roll back. That leaked the mutation across the `await` boundary. Other coroutines (`evaluate_opportunities`, `place_bids`, `refresh_account`) could read the mutated state and act on it. If persist failed and we rolled back, those actions were now based on discarded state — possibly placing duplicate orders, suppressing legitimate orders, or recording phantom fills.

The v7 pattern is: build detached snapshot (read-only of live ledger) → await persist → on success, synchronous-apply to live ledger. No mutation is ever exposed across an `await`. The apply block has no yield points — it's a single contiguous sequence of assignments. Other coroutines see either fully-pre-apply state or fully-post-apply state, never a transient mix.

**Helpers:**

- `_snapshot_with_rebuild_applied(rebuilt, clear_fills_stale, clear_resting_stale, clear_legacy_pending) → LedgerSnapshot`: pure function. Reads current ledger state, applies rebuild overlay, sets flag clears according to parameters, returns immutable snapshot. Does NOT mutate the ledger.
- `_apply_snapshot(snapshot: LedgerSnapshot) → None`: synchronous. Overwrites live ledger fields from the snapshot in a single block. No `await`. Callers must hold the pair-level mutation lock iff other synchronous code paths could interleave (single-threaded event loop makes this a no-op unless we fork threads).

**Engine wiring:** `Engine._persist_games_now(proposed: LedgerSnapshot, event_ticker: str)` is a **synchronous** function. It:

1. Iterates active pairs in the game manager.
2. For the pair matching `event_ticker`, uses `proposed` as its `ledger` dict (instead of calling `to_save_dict()` on the live ledger).
3. For all other pairs, calls `ledger.to_save_dict()` directly. Each call is sync and atomic relative to the event loop — no interleaving possible.
4. Serializes the combined dict to JSON.
5. Writes via atomic temp + rename (`_atomic_write_text`).
6. Returns.

No locks. No timeouts. If any `to_save_dict()` raises, the whole function propagates the exception and the caller returns ERROR. If the atomic write fails, same thing. There are no partial writes because atomic rename is all-or-nothing.

Typical duration <10ms for a game set of dozens of pairs on local SSD. Blocks the event loop briefly during that window, which is the existing Talos pattern for `_persist_games` today.

**Why this structure beats the v4 dead-end:** v4 returned early on mismatch with no further API — the flag was permanently set if the operator had no way to clear it. v5 separates detection (`reconcile_from_fills` → MISMATCH + retained `_pending_mismatch`) from resolution (`accept_pending_mismatch` → explicit apply). The operator always has a forward path; the spec never leaves a ledger permanently stranded.

**UI banner states (per pair, visible whenever `ready() == False`):**

| State | Banner severity | Primary action | Secondary action |
|---|---|---|---|
| `stale_from_persistence` only, auto-reconcile in progress | info ("Confirming state with Kalshi…") | — | — |
| `stale_from_persistence` + 30s timeout exceeded | warning | "Retry sync" | "Manual reconcile" |
| `legacy_migration_pending` | warning (always shown on startup after migration) | "Reconcile now" | "View what will change" |
| `reconcile_mismatch_pending` | error (mandatory resolution) | "Accept Kalshi-fills state" | "Resolve on Kalshi, then reset pair" |

**"Resolve on Kalshi, then reset pair"** is the manual escape: operator closes the position on Kalshi directly (or accepts that the persisted state was wrong), then invokes `reset_pair` which zeroes counts, clears all flags, and treats the pair as fresh on next cycle.

**Invariants the reconcile path enforces (unchanged from v4 but now testable):**

1. **Pagination exhaustion** — see paginator contract + test preconditions above.
2. **Single-source ground truth** — fills only, no blending.
3. **Mismatch halts clearing** — now with an explicit operator-accept path (F11 fix).
4. **Atomic application** — `_apply_rebuilt` replaces both sides' counts/costs/fees in one step or not at all.

### Section 9 — Regression guardrail (`tests/test_unit_discipline.py`)

AST-based test that fails if any file outside `src/talos/units.py` uses raw unit arithmetic. This is the key mechanism for preventing silent re-regression after the migration lands.

**Checks:**

1. **Literal `100` or `10_000`** as an arithmetic operand on an identifier matching `*price*`, `*cost*`, `*bps*`, `*fp100*`, `*cents*`, `*fees*`, `*edge*`. `ast.walk` + binop node type matching.
2. **Format spec `:.2f` or `:.4f`** applied to any identifier matching the patterns above.
3. **Call to `float()`** on any identifier matching `*_dollars` or `*_fp` (the wire payloads). These must go through `Decimal`.
4. **Call to deprecated helpers** `dollars_to_cents`, `fp_to_int` outside the deprecated-re-export block.

**Allowlist:** a single `ALLOWED_LITERALS` set at the top of the test lists file:lineno allowlist entries, each with a one-line comment explaining why (e.g., time conversions: `time.time() * 1000` for ms timestamps). Fewer than 10 entries expected post-migration.

**Runtime cost:** walks `src/talos/` Python files at collection time. Expected <500ms total.

---

## Touch sites summary

Counts from `grep -rn '/ 100\|\* 100\|100 -\|:.2f\|:.4f\|int(float' src/talos/` on HEAD of `feat/bps-fp100-migration` (2026-04-21):

- **99 hits across 24 files.**

Not every hit is a money-unit bug — the same regex captures time-ms conversions (`time.time() * 1000`), elapsed-ms display formatters, and cosmetic percentage rendering. The AST test in Section 9 distinguishes money-unit arithmetic from unrelated uses by requiring the identifier name to match money patterns. The allowlist is expected to absorb ~10–20 of the 99.

The `writing-plans` step will enumerate every hit individually and classify it as (a) migrate to `units.py`, (b) allowlist, or (c) intentionally out of scope.

---

## Test plan

**New test files:**
- `tests/test_units.py` — conversion helper round-trip tests, Decimal precision tests, edge-case values (0, ONE_DOLLAR_BPS, fractional contracts including the Marjorie `1.89` case). Also: fail-closed tests for `dollars_str_to_bps` and `fp_str_to_fp100` on non-integral inputs (`"0.53001"`, `"1.891"`) — must raise ValueError, not round silently.
- `tests/test_unit_discipline.py` — AST regression guardrail.
- `tests/test_position_ledger_migration.py` — v1 (cents) → v2 (bps) persistence migration round-trip. Must include the stranded-position scenario (1.89 YES on MARJ, same-ticker).
- `tests/test_fees_bps.py` — parameterized equivalence: fee formula in bps must match cents formula to ≤1 bps error at every integer cent price 0..100.
- `tests/test_reconcile_durability.py` — **F13/F16/v11 atomicity tests.** Fixtures:
  - **Successful reconcile + durable persist:** mock `persist_cb` that succeeds → assert state applied AND serialized snapshot matches post-mutation state. Read the file from disk and assert all fields match.
  - **Reconcile persist failure:** mock `persist_cb` that raises → assert live ledger completely unchanged (apply happens only AFTER persist succeeds). Assert return is ERROR.
  - **F16 in-session-only mismatch contract:** `reconcile_from_fills` → MISMATCH outcome → serialize ledger → assert the serialized envelope does NOT contain `reconcile_mismatch_pending` or the rebuilt payload. Reload ledger → assert `reconcile_mismatch_pending` is False, `_pending_mismatch` is None, `stale_fills_unconfirmed` still True.
  - **Pagination failure:** `reconcile_from_fills` with `get_all_fills` raising mid-chain → assert flags untouched, no persist call, no live mutation.
  - **Successful operator accept + durable persist:** `accept_pending_mismatch` → state applied, flags clear, generation incremented, disk reflects new state.
  - **Stale-mismatch-accept prevention (generation counter):** capture `_pending_mismatch` at gen G. Call `record_fill` (gen becomes G+1). Call `accept_pending_mismatch` → assert raises `StaleMismatchError`, `_pending_mismatch` cleared, live ledger still has the record_fill mutation intact.
  - **Accept persist failure:** `accept_pending_mismatch` with persist_cb raising → assert live ledger unchanged, `_pending_mismatch` retained (operator can retry after fixing disk issue).
  - **v11 single-event-loop atomicity:** while `reconcile_from_fills` is in its sync mutation block (monkey-patched `_apply_snapshot` sleeps sync for 50ms to simulate a slow disk), spawn a concurrent coroutine that calls `record_fill`. Because the mutation phase is sync with no await, assert the `record_fill` coroutine cannot run until reconcile's full block completes. After reconcile finishes, `record_fill` proceeds and lands AFTER the rebuild. Final ledger state = rebuild + fill, not a mix.
  - **Mutator generation counter discipline:** for every `PositionLedger` sync mutator (`record_fill`, `record_resting`, `record_placement`, `record_cancel`, `sync_from_orders`, `sync_from_positions`, `seed_from_saved`), call it and assert `_mutation_generation` incremented by exactly 1. Property test via an explicit method list.
  - **No async lock regression guard:** assert `PositionLedger` has no `_mutation_lock` attribute and `Engine` has no `_persistence_lock` attribute. Regression guard against re-introducing the async-lock protocol Codex flagged as unimplementable against the sync API (rounds 7–10).
  - **Sync persist_cb contract:** assert `persist_cb` is a plain `def` (not `async def`). Regression guard — if someone refactors it to async, the whole v11 atomicity argument collapses.
  - **F31 cancel bypasses gate:** set `stale_fills_unconfirmed = True` on a ledger. Assert `create_order` and `amend_order` both block; assert `cancel_order_with_verify` SUCCEEDS. Mock `rest.get_order` returning a live order; assert the cancel is issued.
  - **F31 cancel during reconcile_mismatch_pending:** set mismatch flag. Assert cancel still works.
  - **F33 stale-first-ID does NOT clear resting blindly:** set up a ledger where Side A has `resting_count_fp100 = 200` (2 contracts) and `resting_order_id = "stale-1"` but Kalshi actually has two live orders on Side A (order "stale-1" cancelled, order "fresh-2" still resting with count=100). Call `cancel_order_with_verify("stale-1", pair)`. Mock `get_order("stale-1")` returning 404. Assert (a) `_resync_pair_orders` is invoked, (b) after resync, ledger's `resting_count_fp100 == 100` and `resting_order_id == "fresh-2"` — NOT zero. Regression guard against the single-ID-clears-everything hole Codex flagged in F33.
  - **F31 cancel during network failure on get_order:** mock `rest.get_order` raising network error. Assert `cancel_order` falls through to attempted `rest.cancel_order`. On 404 from the cancel, assert resync is triggered (not blind-clear). On other errors, exception propagates.
  - **F31 cancel race: order goes terminal between get and cancel:** mock `get_order` returning live order at T1, then `cancel_order` returning 404 at T2 (order cancelled by Kalshi between calls). Assert `_resync_pair_orders` is invoked, ledger reflects Kalshi-current state.
- `tests/test_market_admission.py` — **F30 scope.** Parameterized over each ingress path (scanner, manual add UI, market-picker UI, tree-commit, startup restore):
  - Admit a non-fractional cent-only market → success on every path.
  - Admit a `fractional_trading_enabled=True` market via a NEW-admission path (scanner, manual add, market-picker, tree commit) → `MarketAdmissionError` raised; pair NOT added to game manager; appropriate UX surfaced (modal for UI paths, WARNING log for scanner).
  - Admit a sub-cent-tick market via a NEW-admission path → same treatment.
  - **F34 + F35 tree-commit structured rejection (end-to-end):** stage a commit batch in a real `TreeScreen` instance with N valid pairs + M rejected pairs (fractional/sub-cent). Invoke `TreeScreen.commit()` → `_commit_worker` full path (not `add_pairs_from_selection` in isolation — that would miss the staging-clear placement bug F35 exists to prevent). Assertions:
    - (a) `add_pairs_from_selection` returned a `CommitResult` with `len(admitted) == N` and `len(rejected) == M` including admission-reason errors.
    - (b) The N admitted pairs are registered in `game_manager.active_games`.
    - (c) The M rejected pairs are NOT registered.
    - (d) After `TreeScreen.commit()` returns, the M rejected rows ARE STILL staged (not cleared by `to_clear_unticked` sweep); the N admitted rows ARE cleared.
    - (e) Post-add metadata (labels, subtitles) was applied only to admitted rows, not rejected.
    - (f) `_commit_worker` shows the partial-failure dialog enumerating each rejected row + reason.
    - (g) The "Commit complete" success toast is NOT shown when M > 0.
    - (h) All-rejected case (N=0, M>0): only partial-failure dialog shown, staging untouched, no success toast.
    - Regression guards against both the v12 silent-partial-success bug (F34) AND the v13 "contract only in _commit_worker" placement bug (F35).
  - **Startup restore quarantine (F32):** persist a pair with a currently-OK market; while Talos is offline, Kalshi flips the market to `fractional_trading_enabled=True` (simulated via fixture); restart Talos. Assertions: (a) the pair IS registered in `game_manager.active_games`, (b) ledger is seeded from persistence, (c) feeds are wired up, (d) `pair.engine_state in ("exit_only", "winding_down")` after restore, (e) event is in `_exit_only_events`, (f) operator notification fires explaining the admission reason, (g) a `create_order` attempt is blocked by engine_state gating, (h) a `cancel_order_with_verify` on any existing resting order succeeds. The pair is NOT unregistered, NOT silently dropped, NOT silently admitted for new entry.
  - **F37 quarantine durability across restart:** after the F32 scenario above completes and `_persist_games_now` has been called, kill the engine process. Restart. Assertions: (a) the pair loads from `games_full.json` with `engine_state == "exit_only"` (NOT reverted to `"active"` from an older persisted value), (b) restore runs admission again — if the market is still fractional, the quarantine is re-applied and another durable persist fires (idempotent), (c) if Kalshi has since reverted the market to non-fractional, restore-admission passes but the pair stays in `exit_only` because that was its persisted state (operator must explicitly reactivate). Regression guard against the quarantine-only-in-memory hole Codex flagged in F37.
  - **Phase 1+2 admission relaxation:** after the full migration lands, fractional and sub-cent markets pass admission on every path (regression guard that the admission guard gets updated, not just the scanner).
- `tests/test_staleness_triggers.py` — **F14 + F17 + F22 scope.** Parameterized over the eight staleness-relevant persisted fields: each nonzero historical field must set `stale_fills_unconfirmed = True`; each nonzero resting field must set `stale_resting_unconfirmed = True`; all-zero leaves both False. Also: loaded state with only resting nonzero (no fills) sets only the resting flag and is cleared by any `sync_from_orders` completion. **F22 zero-state v1 migration:** loading a v1 envelope where every numeric field is zero must NOT set `legacy_migration_pending`; both staleness flags stay False; `ready()` passes after first orders sync; next save writes a clean v2 envelope with no `legacy_v1_snapshot` field.
- `tests/test_confirmation_sources.py` — **F15 + F17 + F20 scope.** Parameterized source vs flag matrix:
  - `sync_from_positions` alone → NO flag clears.
  - `sync_from_orders` any response (including empty) → ONLY `stale_resting_unconfirmed` clears.
  - **`sync_from_orders` with match → ONLY `stale_resting_unconfirmed` clears.** Matched orders do NOT clear `stale_fills_unconfirmed` or `legacy_migration_pending` — orders-endpoint data is archival-incomplete for historical economics (F20).
  - **F20 negative regression:** after orders-with-match, assert `stale_fills_unconfirmed` is still True AND `legacy_migration_pending` is still True. Regression test that prevents implementation drift back to the archived-orders false-positive path.
  - `reconcile_from_fills` OK → `stale_fills_unconfirmed` + `legacy_migration_pending` clear; `stale_resting_unconfirmed` does NOT clear.
  - Combined: `sync_from_orders` empty + `reconcile_from_fills` OK → all three clear (cumulative).
  - `accept_pending_mismatch` → `stale_fills_unconfirmed` + `legacy_migration_pending` clear; `stale_resting_unconfirmed` does NOT clear.

**Updated test files:** every existing test that constructs a model instance with cent-valued integer fields needs to either use the new `_bps` / `_fp100` field names or call a `from_cents` constructor helper (to be added to each model). The fixtures in `tests/fixtures/` that mock Kalshi API responses in legacy format must be regenerated in the new `_dollars` / `_fp` wire format — partially already done per the 02-kalshi-fp-migration phases that shipped in March.

**Mandatory skills per CLAUDE.md after implementation:**
- `safety-audit` — touches order placement, position tracking, fees. Run after every commit in Phase 1+2.
- `position-scenarios` — changes `position_ledger.py` and `bid_adjuster.py`.
- `test-runner` + `lint-check` in parallel before every commit.

---

## Verification (runtime)

After Phase 1+2 ships:

1. Launch Talos on demo, verify connect to Kalshi, opportunities table populates.
2. **Rehydration of the stranded MARJ position:** ledger legacy-load converts to fp100/bps; first `sync_from_orders` reports `fill_count_fp="6.89"` parsed as `689` fp100 on Side A at `maker_fill_cost_dollars="3.57"` parsed as `35700` bps; `filled_total_cost_bps` replaced monotonically. Review tab shows:
   - Side A fills: 6.89 contracts (displayed as `6.89`), avg 51.87¢.
   - Matched pairs: 5 contracts (500 fp100 × 2 sides).
   - Unmatched A: 1.89 contracts, exposure $0.92 (at mark $1.13 if YES is at 60¢).
   - Locked P&L: +$0.15 (actual), not −$0.22 (legacy-buggy).
3. Place a whole-contract order via the UI; verify request payload uses 4-decimal `_dollars` wire format and `count_fp` matches.
4. Cancel + amend round-trip; verify ledger stays consistent.
5. Run a scanner cycle against a sub-cent market (e.g. DJT); verify it now produces opportunities (it previously silently dropped them).
6. Exit-only events, milestone escalations, rebalance-on-imbalance: exercise all code paths that mutate the ledger.

Full pytest suite must pass green. `ruff check --fix` clean. `pyright` clean.

---

## Out of scope (restated explicitly)

- Active tick-snapping on outbound non-book-derived prices (separate follow-up).
- Fractional-contract order submission (only fractional parsing in scope).
- Backfill of historical closed-event ledger data beyond the unit-scale conversion.
- Production opt-in (demo-only until separate hardening PR after soak).

## Open questions

None. The three questions surfaced in the 2026-04-17 design session (persistence strategy, regression-test opt-out, out-of-scope expansions) were answered in the codex review and locked into the decisions above. If operator review of this spec surfaces new questions, note them here and re-run the review.

---

## Implementation sequencing (for the writing-plans step)

The spec does NOT prescribe a commit-by-commit plan. `superpowers:writing-plans` will produce that from this spec. As context for the planner:

- Phase 0 is a single PR of <300 lines. Phase 1+2 is multi-commit within one PR.
- Commit order within Phase 1+2 should preserve green tests and a working binary at every commit: (a) add `units.py` with AST-disabled, (b) add `test_units.py`, (c) migrate `_converters.py` + boundary models with shim, (d) migrate leaf consumers (scanner, orderbook, display) with shims in place, (e) migrate money-critical core (`fees.py`, `position_ledger.py`, `bid_adjuster.py`), (f) migrate `rest_client.py` wire format, (g) add `get_all_fills` paginator in `rest_client.py`, (h) add persistence versioning (v2 envelope with optional `legacy_v1_snapshot`) + startup gate + `_seeded_nonzero_from_persistence` tracking, (i) add `reconcile_from_fills` method + UI reconcile action + banner, (j) enable AST test, (k) remove `_converters.py` shim.
- `position-scenarios` skill runs at (e) and (h)–(i). `safety-audit` runs at (e), (f), (h), (i), and before PR merge.
