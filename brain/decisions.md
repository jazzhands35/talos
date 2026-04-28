# Decisions

Record significant technical decisions here.

## 2026-04-27 — DRIP catch-up stays one-at-a-time until ledger trust is proven

**Context:** When DRIP is enabled on a ticker that already has an imbalanced position (e.g., side A = 0 contracts, side B = 20 contracts — either from a mistake, from transitioning a non-DRIP ticker into DRIP, or from any other prior trading), `_drive_drip`'s seed logic places **one** drip-sized order at a time on the behind side, gated on `resting_count(side) == 0`. The `max_drips` cap (which sets `per_side_contract_cap = drip_size * max_drips`) is irrelevant during catch-up — the serialization condition keeps resting at 0 or 1 throughout. Closing a 20-contract gap with `drip_size=1` therefore takes ~20 sequential fills instead of being parallelized up to `max_drips`.

A natural future enhancement is to let catch-up exceed `max_drips` (or otherwise place multiple drips concurrently up to a separate "catch-up cap") so imbalances close faster.

**Decision:** **Defer that enhancement.** Keep `_drive_drip`'s seed logic strictly one-at-a-time. Document the deferral here so it isn't re-litigated as an oversight, and so a future PR can land it intentionally rather than as a side effect of someone "improving" the seed logic.

**Rationale:** The accelerator only behaves correctly if "how far behind is each side?" is a *trustworthy* number. The CLE-TOR runaway (5c45274, 2026-04-23) and the KXGOLDCARDS double-write (PR fix, 2026-04-27) are both in-session ledger-correctness incidents where Talos's view of `filled_count(side)` diverged from Kalshi's truth — by enough to drive bad decisions. KXGOLDCARDS specifically over-counted by 1 contract per fill, permanent for the session, with no API correction path for same-ticker pairs. Until the ledger-correctness story is more thoroughly stress-tested (no `ws_fill_position_drift` warnings observed across a multi-week window, audit script clean across all 46+ active pairs), accelerated catch-up could *amplify* a misread instead of recovering from one — e.g., placing 15 unwanted contracts because the ledger thinks A is 20 behind when reality is 5. One-at-a-time keeps the worst-case overshoot bounded to `drip_size`.

**Re-examine when:** the drift detector at [engine.py `_on_fill`](../src/talos/engine.py) has gone quiet for a sustained window AND the [drift_report.py diagnostic](../scripts/drift_report.py) shows zero per-pair drift across a representative sample of restarts. At that point the design question becomes "what's the right catch-up cap and acceleration shape?" — separate from the deferral itself. Tracked by the pointer comment in `_drive_drip`.



## 2026-03-03 — Pure state + async orchestrator split

Split into pure state machine + async orchestrator. See [[patterns#Pure state + async orchestrator split]] and [[principles#13. Test Purity Drives Architecture]].

## 2026-03-06 — Fee model and scanner integration

**Context:** Kalshi's maker fee significantly affects the real edge. Fee math was needed in edge calculations, position P&L, display columns, and effective odds.
**Decision:** Created `src/talos/fees.py` as a pure utility module with zero dependencies. Scanner computes both `raw_edge` and `fee_edge` via `fee_adjusted_edge()`. Display uses `fee_edge`; raw edge is kept for reference.
**Rationale:** Single source of truth for fee math. Pure functions are trivially testable and composable. Used by scanner (edge), position_ledger.py (locked profit), and widgets (display). See [[principles#14. Parse at the Boundary]].

## 2026-03-09 — Quadratic fee model and fill-time charging

**Context:** P&L display showed wildly wrong values (e.g., -$0.52 for a position with actual profit $0.02; $0.56 for actual $1.46). Two compounding bugs: (1) fee formula was linear `(100-p) × 1.75%` instead of Kalshi's quadratic `p × (100-p) × 1.75% / 100`, massively overstating fees on asymmetric prices; (2) fees were modeled as settlement-time deductions from the winning side's profit, but Kalshi actually charges fees at fill time.
**Decision:** Fixed fee formula to quadratic. Changed `fee_adjusted_profit_matched` and `scenario_pnl` to accept actual `maker_fees` from the API instead of computing fees. Added `filled_fees` tracking to `PositionLedger._SideState`, populated from `order.maker_fees` in `sync_from_orders`. Threaded fees through `LegSummary.total_fees` to widgets.
**Rationale:** Verified against actual API data — `fee_cost` on fills and `maker_fees` on orders match the quadratic formula exactly. Using actual fees from the API is more accurate than any formula (handles rounding, fee accumulator rebates). The old linear model overstated fees 3-4x for prices far from 50¢.

## 2026-03-09 — Count fills from all orders including cancelled

**Context:** Position showing 124/124 when Kalshi had 150/150. User had manually cancelled a partially filled bid and re-placed at a different price. The cancelled order's 26 fills were invisible because `sync_from_orders` filtered on `ACTIVE_STATUSES` before counting fills.
**Decision:** Removed `ACTIVE_STATUSES` filter for fill counting — fills from cancelled/amended orders are real. Kept the filter for resting order tracking only. Bumped order fetch limit from 50 to 200 to catch older cancelled orders.
**Rationale:** A fill is a fill regardless of the order's final status. The previous filter was safe for a world where orders were never cancelled with partial fills, but manual cancel-and-replace is a normal workflow.

## 2026-03-06 — Queue position: separate fast polling with conservative merge

**Context:** Queue positions change faster than order state. The 10s `refresh_account` cycle was too slow. Kalshi's dedicated endpoint has inconsistent response schemas across API versions.
**Decision:** 3s polling via `refresh_queue_positions`, conservative merge cache (`_merge_queue`), all non-negative values cached/displayed. Zero from API means "front of queue" (0 preceding shares per API spec). Cache is pruned every 30s on `refresh_account` to prevent stale entries.
**Rationale:** Queue position only improves (monotonically decreasing). Conservative merge (keep smallest non-negative value) handles transient API inconsistencies. The 30s prune cycle resets stale cache entries. See [[patterns#Enrichment caching with split polling cadence]].

## 2026-03-06 — Game persistence: tickers only, re-fetch on startup

**Context:** Games added via the TUI are lost on restart. Need persistence without coupling GameManager to filesystem.
**Decision:** Persist only event tickers to `games.json`. On startup, re-add via the normal `add_game` flow (REST fetch + WS subscribe). `GameManager.on_change` callback fires on add/remove/clear; `__main__.py` wires it to `save_games`.
**Rationale:** Persisting tickers (not full pair data) ensures state is always fresh from the API. The callback pattern (see [[patterns#Callback-based layer decoupling]]) keeps GameManager testable without filesystem mocks.

## 2026-03-07 — scenario_pnl uses total costs, not per-contract averages

**Context:** GTD profit display showed $16 when actual Kalshi payout was $10.21 (~$6 discrepancy). Root cause: `scenario_pnl` received per-contract averages computed via integer division (`total_fill_cost // filled`), which truncated remainders. At 1400 contracts with avg 49.58¢ truncated to 49¢, cost underestimated by 0.58¢ × 1400 = $8.12.
**Decision:** Changed `scenario_pnl` signature to accept `total_cost_a`/`total_cost_b` (exact sums) instead of per-contract averages. Added `total_fill_cost` field to `LegSummary` so exact costs flow through the entire pipeline. GTD display changed from `:.0f` to `:.2f` for cent-accurate amounts.
**Rationale:** Financial calculations must carry exact values as deep as possible. Integer division truncation compounds linearly with contract count. See [[patterns#Financial calculation precision]].

## 2026-03-07 — PositionLedger as single source of truth for UI and safety

**Context:** The system needs a position model for bid adjustment safety gates. Two options: (A) single source of truth that feeds both UI display and safety logic, or (B) separate systems that both derive from polled order data independently.
**Decision:** Option A — PositionLedger as single source of truth. Feeds both bid adjustment safety gates and UI display via `compute_display_positions()`. The old `compute_event_positions()` and `position.py` have been deleted. See [[decisions#2026-03-08 — TradingEngine extraction and position unification]].
**Rationale:** Two systems deriving from the same data can disagree due to timing or implementation drift. If the UI shows "10 filled on side A" but the safety gate thinks it's 8, the operator can't trust either. A single source of truth means if the UI looks right, the safety logic is right — and if it's wrong, the operator sees it immediately. The risk of a larger blast radius (changing the UI data source) is worth the guarantee of consistency. See [[principles#15. Position Accuracy Is Non-Negotiable]].

## 2026-03-07 — Semi-auto graduating to full-auto for bid adjustment

**Context:** Bid adjustment could be fully automatic or require human approval. A previous automated system failed due to position tracking bugs, causing cascading over-placement.
**Decision:** Start semi-auto (system proposes, human approves). Graduate to full-auto only after trust is established through observation.
**Rationale:** Semi-auto serves as a live validation layer — the operator sees every proposed action with full position context and can verify the position model matches reality. This builds confidence in the safety invariants before removing the human gate. See [[principles#2. Human in the Loop]].

## 2026-03-07 — Position-aware bid adjustment: safety model

**Context:** Planning automated bid adjustment when resting orders get "jumped" (outbid). A previous system failed due to cascading over-placement from lost position tracking.

**Decision:** Established structural safety rules captured as [[principles#15. Position Accuracy Is Non-Negotiable]] through [[principles#19. Most-Behind-First on Dual Jumps]]: unit-based atomic bidding (P16), amend over cancel-and-replace (P17), fee-adjusted profitability gate (P18), most-behind-first tiebreaker (P19), semi-auto first (P2). Plus fractional completion bids (resting + filled ≤ 1 unit). Every rule traces to a specific prior failure mode.

## 2026-03-08 — TradingEngine extraction and position unification

**Context:** `TalosApp` was a 481-line god class owning subsystem references, mutable caches (queue, orders, CPM), and all polling/action methods. Position display was computed by `compute_event_positions()` from raw orders, separate from `PositionLedger` which drove safety gates — two systems deriving from the same data.

**Decision:** Extract `TradingEngine` as a headless orchestrator owning all business logic. Slim `TalosApp` to a thin UI shell that delegates via callbacks. Unify position computation: `compute_display_positions()` reads from `PositionLedger` (the safety source of truth), replacing the deleted `compute_event_positions()` and `position.py`.

Key changes:
- **Engine owns state**: Queue cache, orders cache, CPM tracker, balance, position summaries all live in `TradingEngine`
- **Callback-based UI**: Engine communicates via `on_notification(message, severity)`. App wires this to Textual toasts
- **Single position truth**: `compute_display_positions()` lives in `position_ledger.py`, reads ledger state, enriches with queue/CPM data
- **BidAdjuster encapsulation**: Replaced 4 `ledger._sides[side]` accesses with public accessors

**Rationale:** Engine extraction enables headless testing of all business logic without Textual. Position unification eliminates the risk of UI and safety gates disagreeing. The callback pattern keeps the engine framework-agnostic — a future web UI or API could use the same engine. See [[principles#13. Test Purity Drives Architecture]].

## 2026-03-08 — Auto-discover events with positions or resting orders

**Context:** On startup, users had to manually add Kalshi event URLs for every game they had positions or resting orders in. This was tedious and error-prone.
**Decision:** At startup, query Kalshi's `/portfolio/positions` endpoint and parse the `event_positions` array (which contains `event_ticker` directly). Merge discovered event tickers with saved games (union, deduplicated). Wire `adjuster.add_event(pair)` in engine's `start_feed()` and `add_games()` to ensure position ledgers are created. Add one-time seeding path in `sync_from_orders()` for fresh (empty) ledgers encountering existing Kalshi fills.
**Rationale:** The `event_positions` array avoids needing to resolve market→event tickers (zero extra API calls). Fresh-ledger seeding is safe because there's no prior state to conflict with — P15 discrepancy checks resume on subsequent syncs. See [[principles#15. Position Accuracy Is Non-Negotiable]].

## 2026-03-09 — Hold proposals and periodic re-evaluation

**Context:** When a resting order was jumped but the system decided not to adjust (unprofitable, deferred, safety gate), `evaluate_jump` returned `None` silently. The operator couldn't tell if the system was broken or deliberately holding. Additionally, jumps present at startup were never evaluated — `TopOfMarketTracker.on_change` only fired on state transitions, and `was_at_top=None` on first check meant no callback.
**Decision:** (1) `evaluate_jump` now returns `ProposedAdjustment(action="hold")` with a reason when deciding not to adjust, surfaced in the ProposalPanel. (2) `TopOfMarketTracker.check` fires `on_change` on first observation when already jumped. (3) `TradingEngine.reevaluate_jumps()` runs every `refresh_account` cycle, generating proposals for any jumped ticker missing one.
**Rationale:** Inaction is a decision — it must be visible to the operator (Principle 20). The periodic re-evaluation catches anything missed by startup timing, lost WebSocket events, or transitions that occurred before the callback was wired.

## 2026-03-10 — Multi-pair re-entry via modular arithmetic

**Context:** After both sides of a unit fill completely (e.g., 10/10 on each side), the system should suggest re-entering the same event if the edge is still profitable — tracking cumulative fills across multiple pairs.
**Decision:** Used modular arithmetic (`filled_count % unit_size`) instead of calling `reset_pair()`. Gate 2 in `OpportunityProposer` allows re-entry when `both_sides_complete()` and fills are balanced. `BidAdjuster` safety gate uses `filled_count(side) % unit_size` to compute remaining capacity in the current unit.
**Rationale:** Modular arithmetic naturally wraps without resetting any state — fill history is preserved for P&L, and the system seamlessly supports N pairs without explicit pair tracking. `reset_pair()` existed but would have destroyed fill cost and fee data needed for accurate P&L display. See [[principles#16. Delta Neutral by Construction]].

## 2026-03-10 — Position imbalance detection and two-step rebalance

**Context:** 129 vs 107 contract imbalance (22 contracts, exceeding unit size). Initially detection-only; extended to executable rebalance.

**Decision:** Two-step rebalance: (1) reduce over-side resting, (2) catch-up bid on under-side. Equalization target: `max(over_filled, under_committed)`. Trigger threshold: any non-zero committed delta (`abs(committed_a - committed_b) > 0`). Unit size controls entry/re-entry qty; catch-up must close ANY gap because every unhedged contract is real exposure. See [[patterns#Multi-step execution with fail-safe ordering]] for ordering rationale and [[principles#16. Delta Neutral by Construction]].

## 2026-03-10 — Status column for engine decision transparency (P20)

**Context:** Operator couldn't tell why Talos wasn't proposing entry on certain tickers. The system was making correct decisions (low edge, cooldown, waiting for side to catch up) but the reasoning was invisible.
**Decision:** Added `_compute_event_status()` to `TradingEngine` and a "Status" column to `OpportunitiesTable`. Each gate in the proposer pipeline maps to a visible status string: "Low edge", "Stable Xs", "Cooldown Xs", "Filling (B -5)", "Waiting A (-3)", "Need bid A/B", "Proposed", "Sug. off", "Discrepancy", "Imbalanced", "Ready".
**Rationale:** Direct application of Principle 20 (Inaction Is a Decision — Make It Visible). The operator should never wonder "is Talos broken or deliberately waiting?" — every non-action has a visible reason. Status is computed fresh each refresh cycle from current engine state. See [[principles#20. Inaction Is a Decision — Make It Visible]].

## 2026-03-10 — Runaway bidding: safety gate wiring and Kalshi-as-truth

**Context:** Live runaway — positions at 10/20 and 10/30. Three gaps: (1) `is_placement_safe()` never called from `place_bids()`; (2) stale ledger after placement → duplicate proposals; (3) no stability reset after approval.

**Decision:** Two fixes: (1) hard safety gate in `place_bids()` calling `is_placement_safe()` with modular arithmetic for re-entry; (2) stability reset on approval via `record_approval()`. Reverted optimistic ledger update — violated P7, caused false discrepancies. See [[patterns#Stability reset as sync-gap buffer]] and [[principles#7. Kalshi Is the Source of Truth — Always]].

## 2026-03-10 — Positions API as second authoritative source for fills

**Context:** DEDGAL event: Kalshi had 30+10 fills but Talos showed dashes — `GET /portfolio/orders` archived old orders, so `sync_from_orders` computed 0 fills.

**Decision:** Added `GET /portfolio/positions` as second data source (never archives). `sync_from_positions()` patches fill counts when they exceed orders-reported values. Runs after `sync_from_orders` each cycle. Addendum: rewrote to use monotonic fills and sum multiple resting orders (see [[patterns#Monotonic state updates across data sources]]). See [[principles#7. Kalshi Is the Source of Truth — Always]].

**2026-04-26 update:** the `/portfolio/orders` archival behavior described here became universal on **2026-02-19** — completed orders (canceled or fully executed) older than the historical cutoff (`historical.get_historical_cutoff.orders_updated_ts`) no longer appear in `/portfolio/orders` at all. `/historical/orders` is the new source for archived order state. The DEDGAL fix (positions-as-second-source) still works for cross-ticker pairs, but **same-ticker yes/no pairs have no equivalent rescue** — `sync_from_positions` is a no-op for them because `position_fp` is a signed-net scalar (yes − no), useless when both sides are bought. See [[project_cletor_runaway_diagnosis]] for the operational consequence.

## 2026-03-10 — Verify after every order action

**Context:** Rebalance returned `AMEND_ORDER_NO_OP` (fills resolved the imbalance between proposal and execution). Old code treated this as error and never confirmed the outcome.

**Decision:** Added `_verify_after_action()` — immediate two-source sync after every order action. `AMEND_ORDER_NO_OP` treated as success via `_is_no_op()`. See [[patterns#Verify after every order action]].

## 2026-03-12 — Rebalance step 1: decrease_order replaces amend_order

**Context:** Rebalance step 1 used `amend_order` to reduce resting quantity, requiring computation of `fill_count + target_resting` for the specific order. This was fragile — aggregate vs instance fill counts caused `AMEND_ORDER_NO_OP` bugs (see above). The Kalshi `decrease_order` API (`POST /portfolio/orders/{id}/decrease`) is purpose-built for quantity-only reductions: just pass `reduce_to=target` or `reduce_by=N`.

**Decision:** Replaced `amend_order` with `decrease_order(reduce_to=target_resting)` in `_execute_rebalance` step 1. No need to fetch the order first — `reduce_to` is absolute, not relative to fill count. Eliminated the aggregate-vs-instance fill count bug entirely.

**Rationale:** `decrease_order` preserves queue position (unlike cancel+replace), takes a direct target (unlike amend which needs `fill_count + desired`), and eliminates the class of bug where aggregate vs instance fill data produces wrong results. Simpler API = fewer failure modes. The `amend_order` path remains available for price changes.

## 2026-03-12 — Dynamic fee rates from Series API

**Context:** Fee calculations hardcoded `MAKER_FEE_RATE = 0.0175`, but Kalshi supports multiple fee types (`quadratic_with_maker_fees`, `flat`, `fee_free`) with different rates per series. The `Series` model has `fee_type` and `fee_multiplier` fields.

**Decision:** All fee functions (`quadratic_fee`, `fee_adjusted_cost`, `fee_adjusted_edge`, `american_odds`) gained an optional `rate` kwarg (default `MAKER_FEE_RATE`). Added `flat_fee()` and `compute_fee()` dispatcher. `ArbPair` model gained `fee_type` and `fee_rate` fields. `GameManager.add_game()` fetches `Series` to populate fee metadata. `ArbitrageScanner.add_pair()` and `BidAdjuster` pass pair-specific rates to fee functions.

**Flow:** Series API → `ArbPair.fee_rate` → scanner's `fee_adjusted_edge(rate=pair.fee_rate)` → adjuster's profitability checks.

**Rationale:** Backward compatible (all existing calls use default rate), correct for non-standard series. Isolating the rate in `ArbPair` means each pair carries its own fee context without global state. See [[principles#21. Authoritative Data Over Computed Data]].

## 2026-03-12 — Fill cost from API fields, not computed

**Context:** `sync_from_orders` computed fill cost as `order.no_price * order.fill_count`. This is inaccurate for orders that were amended at different prices — the original price doesn't reflect the actual cost of fills at the new price.

**Decision:** Changed to `order.maker_fill_cost + order.taker_fill_cost`, which are exact values from the Kalshi API that account for all fills at their actual prices. Added `maker_fill_cost` and `taker_fill_cost` fields to `Order` model with FP migration from `maker_fill_cost_dollars`/`taker_fill_cost_dollars`.

**Rationale:** Direct application of [[principles#21. Authoritative Data Over Computed Data]]. The API tracks exact costs across all fills at potentially different prices. Our computed approximation was wrong whenever an order was amended. See also [[patterns#Financial calculation precision]].

## 2026-03-12 — Leaner polling reverted (event_ticker filter removed)

**Context:** Phase 8 of the API integration plan added an `event_ticker` filter to `get_orders()` in `refresh_account()` — comma-joining scanner pair event tickers to fetch only monitored events. Kalshi's `GET /portfolio/orders` supports `event_ticker` as a comma-separated list (max 10). After deploying, the UI showed "No orders yet" and position columns showed dashes, while scanner Edge/Net values still worked (WS orderbook path was unaffected).

**Decision:** Reverted the event_ticker filter. `get_orders()` now fetches all orders without filtering (`limit=200`). Also added defensive try/except wrappers in `ws_client._dispatch()` around `model_validate()` and callback execution to prevent WS loop crashes.

**Rationale:** The filter was a premature optimization. The exact root cause was ambiguous — potentially the filter, a WS parse error crashing the listen loop, or a model validation issue in the newly-subscribed portfolio/ticker channels. Rather than debugging blind (no access to runtime logs), the safest fix was reverting the most suspicious change and adding WS safety wrappers to prevent the most dangerous failure mode (listen loop crash). The filter can be re-added later with proper runtime validation. See [[patterns#Defensive WS dispatch (never crash the listen loop)]].

## 2026-03-12 — BidAdjuster.execute: fetch fresh order before amend

**Context:** `BidAdjuster.execute` computed `total_count = ledger.filled_count(side) + ledger.resting_count(side)` for the amend API's `count` parameter. The ledger's `filled_count` is the AGGREGATE across all orders (including archived ones augmented by the positions API). If old orders were archived and fills were augmented, the aggregate could be much higher than the specific order's fill_count, silently expanding the order.

**Decision:** Changed to `rest_client.get_order(cancel_order_id)` before amending, then `total_count = fresh_order.fill_count + fresh_order.remaining_count`. This uses the ORDER's own state, not the ledger aggregate.

**Rationale:** Direct application of [[patterns#Order-specific APIs need order-specific data]]. The rebalance path was already fixed (uses `decrease_order`), but the bid adjustment path still had the aggregate-vs-instance bug. The extra API call is cheap insurance against silent order expansion.

## 2026-03-12 — Removed dead _discrepancy field from PositionLedger

**Context:** `PositionLedger._discrepancy` had field, properties (`has_discrepancy`, `discrepancy`), and guards in `is_placement_safe()`, `_check_post_cancel_safety()`, and `_compute_event_status()`. But no code path ever set it to a non-None value — all tests manually assigned `ledger._discrepancy = "..."`. The field was infrastructure for a halt-on-mismatch design that was superseded by monotonic sync.

**Decision:** Removed the field, properties, all guards, and 4 tests across `position_ledger.py`, `bid_adjuster.py`, `engine.py`, and their test files.

**Rationale:** The monotonic sync pattern (`sync_from_orders` only increases fills, authoritatively overwrites resting) makes discrepancy detection unnecessary — the system self-corrects. Dead safety code creates false confidence that a protection exists when it doesn't. If halt-on-mismatch is needed in the future, it should be designed fresh against the current architecture. See [[patterns#Monotonic state updates across data sources]].

## 2026-03-12 — is_placement_safe: pair-specific fee rate

**Context:** `is_placement_safe()` called `fee_adjusted_cost(price)` without a `rate=` parameter, defaulting to `MAKER_FEE_RATE = 0.0175`. But pairs can have different fee rates via `ArbPair.fee_rate` (from the Series API). `BidAdjuster.evaluate_jump` and `_check_post_cancel_safety` already passed pair-specific rates — only this gate was hardcoded.

**Decision:** Added optional `rate` keyword to `is_placement_safe(side, count, price, *, rate=MAKER_FEE_RATE)`. Both call sites in `engine.py` (`place_bids` and rebalance catch-up) now pass `pair.fee_rate`.

**Rationale:** For pairs with higher fee rates, the hardcoded default underestimated fees and could approve unprofitable placements. Backward compatible — all existing callers without `rate=` use the default. See [[principles#18. Profitable Arb Gate]].

## 2026-03-07 — Bid modal falls back to all_snapshots

**Context:** After placing orders, users couldn't reopen the bid modal on the same game. `on_data_table_row_selected` called `scanner.get_opportunity()` which only returns pairs with positive raw edge. After fills move the market, edge drops to 0 or negative — the row stays visible (from `all_snapshots`) but clicking it silently did nothing.
**Decision:** Fall back to `scanner.all_snapshots` when `get_opportunity()` returns None. See [[codebase/index#Gotchas]] "Don't gate UI actions on volatile data."
**Rationale:** The table is built from `all_snapshots` (all monitored pairs), so the click handler must use the same data source. Users should be able to place bids on any monitored pair regardless of current edge. See [[principles#2. Human in the Loop]].

## 2026-03-12 — Capacity-based safety gate and proposer coverage

**Context:** After changing unit size at runtime (e.g., 10→20), Talos blocked valid placements with "already resting on Side B" and proposed wrong quantities (full unit_size instead of the gap). Three overlapping issues: (1) `is_placement_safe` had a boolean "one resting per side" hard block, preventing additions alongside existing orders; (2) `OpportunityProposer` Gate 2 used boolean `resting > 0` to mean "side is covered," which was wrong when resting orders only partially filled the new unit; (3) qty computation didn't subtract existing resting orders.

**Decision:** Three-layer fix: (1) Removed the "one resting per side" check from `is_placement_safe` — it was strictly redundant with the unit capacity check (`filled_in_unit + resting + count > unit_size`). (2) Changed Gate 2 from `resting > 0` to `resting >= unit_remaining(side)` — a side is "covered" only when resting orders fill the remaining capacity. (3) Qty computation subtracts existing resting: `need = unit_remaining(side) - resting_count(side)`, with an exception for re-entry after both sides complete (uses full `unit_size`).

**Rationale:** The fix is general-purpose — works for any partial state (unit size change, Kalshi-side errors, manual interventions) without special-casing. Boolean coverage was a false simplification: 5 resting in a 20-unit is not "covered." Capacity-based checks naturally handle any combination of fills, resting, and unit size. See [[principles#16. Delta Neutral by Construction]].

## 2026-03-12 — Withdraw both sides when unprofitable with no fills

**Context:** With 0 fills on both sides and resting orders, a jump can make the arb unprofitable. The existing `hold` action keeps capital locked in a losing position with no delta-neutral anchor to protect.

**Decision:** Added `withdraw` action to `evaluate_jump`. When the profitability check fails AND `filled_count(A) == 0 AND filled_count(B) == 0`, return a `withdraw` instead of `hold`. The engine cancels both sides' resting orders at execution time (looking up order IDs fresh from the ledger per P7). When fills exist on either side, `hold` remains correct — the filled side provides a delta anchor and waiting for market return avoids crystallizing a loss.

**Rationale:** With 0 fills, there's no sunk cost and no delta to protect. Holding resting orders in an unprofitable arb just locks capital. Withdrawing frees capital to redeploy when conditions improve. With fills, the calculus reverses — the filled side creates an obligation, and the market often returns to fill the other side. See [[principles#16. Delta Neutral by Construction]].

## 2026-03-13 — Rebalance extraction from TradingEngine

**Context:** `engine.py` was 1,528 lines with 40+ methods. The rebalance logic — `check_imbalances()` (152 lines) + `_execute_rebalance()` (182 lines) — was the largest and most complex code path, with deeply nested conditionals and multiple error branches. This was the highest-risk area for subtle bugs.

**Decision:** Extracted into `rebalance.py` as two standalone functions: `compute_rebalance_proposal()` (pure, no I/O) and `execute_rebalance()` (async). Engine's `check_imbalances()` became a 15-line loop. Also consolidated `_is_no_op()` helper (only used by rebalance). Used functions, not a class — avoids state sync with engine caches.

**Additional improvements in the same session:**
- Consolidated FP converters (`_dollars_to_cents`, `_fp_to_int`) from 4 model files into `models/_converters.py` — single source of truth for API format conversion
- Added `log_unknown_fields()` to REST models — DEBUG-level, once-per-session dedup, surfaces schema drift without noise
- Added toast notification on `_verify_after_action` failure — operator now sees "Verify FAILED" instead of silent swallow (P20)
- Added `resting_tickers` property to `TopOfMarketTracker` — replaced private `_resting` access from `app.py`
- Added dedicated tests for `models/strategy.py` (ArbPair, Opportunity, BidConfirmation)

**Rationale:** Engine dropped from 1,528 to 1,222 lines (-20%). Detection tests became purely mock-free (just ledger + pair + function call). Behavioral improvement: `_verify_after_action` now always runs after rebalance from the engine (even on step 2 early returns), fixing a gap where step 1 could succeed but verification was skipped. See [[patterns#Extract as functions, not classes]].

## 2026-03-13 — Optimistic ledger with generation-based stale-sync guard

**Context:** Live double bidding — auto-accept placed 20+20 on both sides, then 19+19 more. The Talos table showed only one row (summed resting) and didn't catch the duplication. Root cause: `refresh_account` has multiple `await` yield points between `get_orders()` and `evaluate_opportunities()`. When auto-accept approves a proposal during one of these yields, the stale orders list (fetched before placement) is used by `sync_from_orders` to overwrite the ledger's resting state to 0. The proposer then sees empty resting and re-proposes.

**Prior approach (reverted 2026-03-10):** Stability-reset-only. After approval, clear the proposer's stability timer so it must re-observe `stability_seconds` before re-proposing. This was insufficient because: (a) with `stability_seconds=0` there's no protection, and (b) the stale sync happens WITHIN a single `refresh_account` call, so stability timing between poll cycles is irrelevant.

**Decision:** Two-part fix:
1. **Optimistic ledger update** — `place_bids` calls `ledger.record_placement()` (not `record_resting()`) after successful `create_order`. Also appends the returned `Order` objects to `_orders_cache` for WS handler matching.
2. **Generation-based stale-sync guard** — `PositionLedger` gains a `_sync_gen` counter bumped at the start of each `refresh_account`. `record_placement` tags the side with `_placed_at_gen = sync_gen`. `sync_from_orders` refuses to clear resting when `_placed_at_gen >= sync_gen` (stale data from the same generation). When `resting_list` is found (confirming the order), the guard is cleared. Next generation's fresh data can clear resting normally.

**Why this doesn't violate P7:** The optimistic state is set AFTER `create_order` returns successfully — the order exists on Kalshi. The generation guard doesn't prevent Kalshi-sourced updates from taking effect; it only prevents stale Kalshi data (fetched before the order existed) from erasing confirmed state.

**Supersedes:** The 2026-03-10 "Reverted optimistic ledger update" decision. The old approach (bare `record_resting()`) had no stale-sync protection. The new approach (`record_placement()` + generation guard) prevents both the false-discrepancy problem and the double-bidding race. See [[patterns#Generation-based stale-sync protection]].

## 2026-03-14 — Paginate order fetching (safety-critical)

**Context:** With 50+ games, `get_orders(limit=200)` silently truncated the response. Resting orders beyond the 200th were invisible to the ledger, causing false "Balanced" status. The Kalshi API returns most-recent-first, so older resting orders fell off the end. 83 cancelled orders wasted slots.
**Decision:** Added `get_all_orders()` which paginates via Kalshi's cursor-based API. All callers (`refresh_account`, `_verify_after_action`) switched from `get_orders(limit=200)` to `get_all_orders()`.
**Rationale:** Principle 15 violation — position accuracy is non-negotiable. A truncated order list means the ledger doesn't reflect Kalshi's actual state. With auto-accept on, this could cause missed second-leg placements (the system thinks the event is "Balanced" when it has an active resting order).

## 2026-03-14 — Multi-source game status provider

**Context:** Kalshi provides no explicit "game start time" field. The "Closes" column (market close time) was useless for trading decisions.
**Decision:** Built `GameStatusResolver` with three external sources: ESPN (NHL, NBA, MLB, NFL, college sports — free, no auth), The Odds API (AHL, minor leagues — free tier), PandaScore (esports — free tier). Maps Kalshi series tickers to sources via `SOURCE_MAP`. Matches games by team codes extracted from `Event.sub_title`. Batched by source to avoid N+1 API calls.
**Key gotchas:** Kalshi series tickers use `KXNHLGAME` not `KXNHL`. Event tickers concatenate teams (`KXNHLGAME-26MAR14BOSWSH`) — team extraction must use `sub_title`. PandaScore rejects `filter[scheduled_at]`, requires `range[scheduled_at]`. ESPN status lives inside `competitions[0].status`, not `event.status`. Tennis has no good free API for individual matches.

## 2026-03-14 — HTTP timeout prevents permanent freeze

**Context:** Talos froze permanently under load (146+ games). `httpx.AsyncClient()` had no timeout. When Kalshi's API hung on a response (common — balance calls took 7+ seconds), the `await` blocked forever. With auto-accept placing orders (4 REST calls per approval), one hung call backed up the entire event loop.
**Decision:** Added `httpx.Timeout(15.0)` to the REST client and `Timeout(10.0)` to game status providers. Timeout exceptions propagate as regular errors, caught by existing `except Exception` handlers.
**Rationale:** A 15-second timeout is generous — Kalshi normally responds in 200ms-3s. Anything over 15s is effectively dead. The timeout converts a permanent hang into a recoverable error.

## 2026-03-14 — WS-primary data architecture

**Context:** `refresh_account` polled Kalshi REST API every 10 seconds, taking 5-10 seconds per cycle (balance 1-7s, orders 1-5s, positions 1-3s). With 146+ games, this consumed >50% of event loop time.
**Decision:** Made WS channels (`user_orders`, `market_positions`, `fill`) the primary data source. Slowed REST polling to 30s backup. Split balance into its own 10s poll (no WS channel for balance). Unknown orders from WS now added to cache instead of ignored.
**Rationale:** WS already delivers the same data in milliseconds. REST polling was redundant but consumed the event loop. 30s backup catches any missed WS messages without blocking.

## 2026-03-14 — Cached startup for instant game restore

**Context:** Startup re-fetched 196 events via REST (392 API calls, 40 seconds). Persistence only saved event tickers.
**Decision:** Save full pair data (`games_full.json`: event_ticker, ticker_a, ticker_b, fee_type, fee_rate, close_time, label, sub_title). On startup, restore from cache with zero API calls. Fallback to REST if cache missing.
**Rationale:** All data needed to create ArbPairs is available at save time. No reason to re-fetch from Kalshi on every restart.

## 2026-03-16 — UI freeze: task accumulation from unbounded asyncio.gather

**Context:** UI froze on WS disconnect. Event loop watchdog (`talos_freeze.log`) caught 561 active tasks. Root cause: `refresh_trades` spawned `asyncio.gather(*[_fetch(t) for t in tickers])` — one REST call per market ticker (40+ with 20 games). `_poll_trades` (30s interval) had no `exclusive` guard, so when the API was slow, new batches launched while old ones were still in-flight. After a few cycles: 500+ tasks overwhelmed the asyncio scheduler.
**Decision:** (1) `_poll_trades` and `_poll_queue` now use `@work(thread=False, exclusive=True, group=...)` — Textual cancels the previous worker before starting a new one. (2) `refresh_trades` now uses `Semaphore(5)` to cap concurrent REST calls per batch. (3) Non-recursive `start_feed` reconnection loop (while True instead of recursive await). (4) Notification dedup (30s window) for recurring toasts. (5) Silent bulk `tracker.check` during refresh_account. (6) Batched SQLite commits in `log_market_snapshots`.
**Rationale:** The freeze was NOT from blocking I/O (all REST calls are async). It was from task scheduling overhead — asyncio with 500+ tasks spends more time context-switching than doing useful work. The `exclusive=True` pattern is critical for any Textual `@work` method called from `set_interval` — without it, slow I/O causes unbounded task growth. See [[patterns#Guard interval-triggered workers with exclusive=True]].

## 2026-03-16 — Toast notification accumulation freeze

**Context:** Auto-accept showed "0 accepted" and no proposals appeared despite the system being fully enabled. Event loop watchdog (`talos_freeze.log`) caught 2,466 active tasks — almost all `ToastHolder` message pumps from Textual's `self.notify()`. Each toast creates a widget with its own asyncio task. `on_top_of_market_change` fired for every orderbook update that changed top-of-market state — with ~80 tickers, thousands of toasts accumulated, blocking the event loop 5-8s every 30s. `_poll_account` (which runs `evaluate_opportunities`, `check_imbalances`, `reevaluate_jumps`) and `_auto_accept_tick` were starved — proposals were never generated.
**Decision:** (1) Replaced `self._notify()` in `on_top_of_market_change` with `logger.info()` — the Status column already shows jump state. (2) Added rate limit to `_notify`: max 10 unique toasts per 10s window, on top of the existing 30s dedup. This prevents toast task accumulation regardless of notification source.
**Rationale:** This is a distinct variant of the worker accumulation freeze (see below). Workers accumulate from unbounded `set_interval` calls; toasts accumulate from unbounded `self.notify()` calls. Both create asyncio tasks that overwhelm the scheduler. The fix targets both the specific source (jump toasts → structlog) and the general mechanism (rate limit all notifications). Jump state visibility is preserved via the Status column and structlog — toasts were informational noise in this context.

**Diagnostic shortcut:** Compare `auto_accept_sessions/` file sizes — healthy sessions produce MBs/GBs; broken sessions are 185 bytes (just the session_start event). Also check `talos_freeze.log` task counts before deep code tracing.

## 2026-03-16 — Multi-order toast suppressed; FINAL games enter exit-only pipeline

**Context:** Two bugs: (1) `_reconcile_with_kalshi` fired a "MULTI-ORDER" toast every poll cycle (~10s) when multiple resting orders existed on the same side. Too noisy — decided against toasts for this check. (2) `_check_exit_only` only handled `state == "live"` and `state == "pre"`, missing `state == "post"` (FINAL). Games that reached FINAL without being caught in the "live" window never entered `_exit_only_events`, so `_enforce_all_exit_only` never checked them for auto-removal.
**Decision:** (1) Removed `self._notify()` from multi-order check; kept `logger.warning` for log analysis. (2) Added `gs.state == "post"` branch in `_check_exit_only` — FINAL games now enter exit-only, which triggers the existing auto-remove path (balanced fills + no resting → remove game). Priority order: "live" first, then "post", then "pre" with time window.
**Rationale:** (1) Reconciliation health checks should log, not toast — they're informational, not actionable in real-time. (2) The exit-only pipeline is a classify → enforce two-phase system. Classification gates must be exhaustive — any missing state creates invisible stuck games. The "post" case is especially important because it means the game is over and we're just waiting for Kalshi settlement.

## 2026-03-17 — Tiered notifications: ActivityLog replaces toast accumulation

**Context:** Despite the 10/10s rate limiter added on Mar 16, Talos crashed after ~1.5h with 2,652 active tasks and a 785 MB freeze log. Root cause: even rate-limited toasts accumulate over hours. Each `self.notify()` creates a ToastHolder widget+asyncio task. When the event loop stalls from too many tasks, toast expiry timers can't fire, creating a death spiral.
**Decision:** Tiered notification system: (1) `ActivityLog` widget (Textual `RichLog`) for all automated events — suggestions, accepts, exit-only, rebalance, jumps. Zero asyncio overhead (text append only). (2) Textual toasts reserved for critical errors and user-initiated action results. Engine `_notify(toast=True)` for the rare cases needing interruptive UI. Removed the rate limiter entirely — no longer needed.
**Rationale:** The rate limiter was a bandaid. The real fix is separating the information channel (cheap, high-frequency) from the attention channel (expensive, rare). Same principle as syslog severity: DEBUG goes to files, CRITICAL goes to pagers.

## 2026-03-14 — Disable websockets client-side keepalive pings

**Context:** WS disconnected with code 1011 "keepalive ping timeout". The `websockets` library sends client-side pings every 20s with 20s timeout. When `refresh_account` blocked the event loop for 5-8s, pong responses couldn't process in time, so the library killed its own connection.
**Decision:** Set `ping_interval=None, ping_timeout=None` on `websockets.connect()`. Kalshi already sends server-side pings every 10s with body "heartbeat".
**Rationale:** Client pings are redundant when the server provides keepalive. Disabling them eliminates self-inflicted disconnects. The Kalshi API research skill identified this during investigation.

## 2026-03-17 — Game-start cancel: force-cancel all resting at game start

**Context:** Exit-only mode (30 min before game start) allows behind-side resting orders to catch up to balanced. But when the game actually starts, any remaining resting orders face an informational disadvantage — the market knows the score, our stale orders don't. Analysis of 112 events showed 17 one-sided fills (15.2%) caused -$84.72 in losses, wiping all balanced profit (+$82.49). Of those, 7 events with `game_state_at_fill` of "live" or "post" accounted for -$28.84.

**Decision:** Added `_game_started_events: set[str]` as a second escalation tier above `_exit_only_events`. When `_check_exit_only` detects state "live" or "post", the event is added to both sets. `_enforce_exit_only` cancels all resting when balanced, and uses the imbalanced path (cancel ahead side, keep/reduce behind side) when fills are unequal — regardless of `game_started` status.

**Behavior (all exit-only modes, including game-started):**
- **Balanced fills:** Cancel ALL resting on both sides. No new pairs.
- **Imbalanced fills:** Cancel ahead-side resting. Keep behind-side resting (capped at the gap). `check_imbalances` places catch-up on behind side.
- **Top-ups:** Blocked (new speculative exposure).
- **New pair proposals:** Blocked.

**Revised 2026-03-21:** Originally `game_started` forced cancel-all even when imbalanced, which left behind-side gaps permanently unhedged. Catch-up is risk-reducing (closing unhedged exposure), not speculative — blocking it is worse than allowing it. The imbalanced path now always runs when fills differ, ensuring delta convergence. See [[principles#16. Delta Neutral by Construction]].

## 2026-03-18 — Scope per-action API calls to single event

**Context:** "Catch-up BLOCKED: fresh sync failed" errors flooding the UI during auto-accept. Also frequent "Verify FAILED" warnings. Root cause: `execute_rebalance` called `get_all_orders()` with no filter — fetching ALL orders across 50+ events, paginating through hundreds of records. `_verify_after_action` called `get_positions(limit=200)` also unfiltered. With auto-accept cycling ~1 action/second, each action triggered 3+ unfiltered API calls, hitting Kalshi's rate limit and causing cascading failures.

**Decision:** Three rounds of fixes:

1. **Scope filtering:** `rebalance.py` catch-up: `get_all_orders()` → `get_all_orders(event_ticker=...)`. Verify: `get_positions(limit=200)` → `get_positions(event_ticker=..., limit=200)`. Verify orders: 2× `get_orders(ticker=...)` → 1× `get_all_orders(event_ticker=...)` (3 API calls → 2 per verify).
2. **Error surfacing:** All error handlers now include `type(e).__name__` in notifications. `KalshiRateLimitError` in verify silently logged at debug level (non-critical — action already succeeded, 30s poll catches up).
3. **Rate-limit backoff:** `KalshiRateLimitError` re-raised from engine's adjustment and bid handlers (not swallowed). Auto-accept tick catches it and sets a cooldown timer (`retry_after` from header, minimum 2s). Subsequent ticks skip until cooldown expires. Proposal stays in queue and retries automatically.

**Not changed:** `refresh_account()` at lines 644 and 703 — these are the 30-second global safety net, intentionally fetching all data. Filtering these would require N calls (one per event) instead of 1, making things worse.

**Rationale:** Per-action calls only need data for one event. Filtering reduces response from hundreds of records (multi-page) to 2-4 records (single page). Rate-limit errors are categorically different from action failures — they signal "slow down," not "this broke." Swallowing them at the action layer prevents the scheduler from backing off, causing cascading failures. See [[patterns#API call scope must match call frequency]] and [[patterns#Rate limit errors propagate to the scheduler]].

## 2026-03-20 — Session persistence: sync gate + positions API fees

**Context:** Portland/Minnesota NBA game: Talos placed a 48¢ bid with the other side at 61¢ (109¢ combined = guaranteed loss). Root cause: Kalshi archives old filled orders from the REST API. After Talos restarts, `sync_from_orders` gets zero fills for the event. `sync_from_positions` restored fill counts and costs but NOT fees. The P18 safety gate trivially passed because the ledger was empty ("no position on other side → allow placement"). Auto-accept fired before the first `refresh_account` completed.

**Decision:** Three-layer fix:
1. **Sync gate** — `place_bids()` blocks until `_initial_sync_done = True`, set after the first `refresh_account` completes. Prevents all order placement on empty ledger state.
2. **Positions API fees** — `GET /portfolio/positions` returns `fees_paid_dollars` per market (discovered in OpenAPI spec — was ignored due to `extra="ignore"`). Added to `Position` model and wired through `sync_from_positions`. Eliminates reliance on archived orders for fee data.
3. **Fee estimation fallback** — When fees are still zero after sync (edge case), estimate via quadratic formula on avg fill price. Better than showing gross-only profit.

**Also fixed:** `fee_cost` omitted from P&L calculation in settlement tracker, settlement history screen (3 locations). All computed `revenue - cost` without subtracting fees — overstating P&L by the full fee amount.

**Also fixed:** `execute()` in `BidAdjuster` had no execution-time P18 re-check. Between proposal and approval, the other side could fill at a different price, making the amendment unprofitable.

**Rationale:** Session boundaries are dangerous — any data that only lives in memory is lost. The positions API is the correct authoritative source because it never archives. See [[principles#7. Kalshi Is the Source of Truth — Always]] and [[principles#21. Authoritative Data Over Computed Data]].

## 2026-03-20 — Stale position reconciliation (two-strike cleanup)

**Context:** "Invested" display showed $5,040 when Kalshi showed $2,398 (and account never had $5,040). Positions from settled events remained in the scanner/ledger because lifecycle WS events (determined/settled) were missed during WS disconnects or when Talos wasn't running.

**Decision:** Two-strike reconciliation in `_reconcile_stale_positions()`: each poll cycle, check `pos_map` for pairs where both tickers have zero positions but the ledger has fills. Flag on first detection, auto-remove via `remove_game()` on second consecutive detection. Zero additional API calls — piggybacks on existing `get_positions()` data.

**Also fixed:** `remove_game()` wasn't calling `_adjuster.remove_event()` (ledger memory leak). `clear_games()` bypassed all cleanup (stale_candidates, exit_only_events, game_started_events, adjuster).

**Rationale:** Two-strike avoids false positives from transient API failures. A single failed `get_positions()` response (rate limit, network blip) would make every pair look "settled" if we acted immediately. See [[patterns#Two-strike cleanup for eventual consistency]].

## 2026-03-21 — Overcommit reduction and catch-up price fallback

**Context:** Two related bugs caused widespread stuck "Waiting" positions after exit-only cleanup:

1. **Overcommit with balanced committed counts:** `compute_rebalance_proposal()` returned None when `delta=0`, even when one side violated unit capacity (`filled_in_unit + resting > unit_size`). Example: Side A 20f+3r=23 committed, Side B 3f+20r=23 committed — delta=0, but Side B has `3+20=23 > unit 20`. The overcommit was detected and logged every cycle but never resolved.

2. **Catch-up permanently blocked by P18 historical fills:** The P18 profitability pre-check in `compute_rebalance_proposal()` used the other side's historical average fill price (sunk cost), not the current market price. When fills were at worse prices than current market, the catch-up was blocked even though a profitable resting bid existed at a lower price. This left positions stuck in "Waiting" indefinitely.

**Decision:** Two fixes:

1. **`compute_overcommit_reduction()`** — New pure function in `rebalance.py`. When `compute_rebalance_proposal()` returns None (balanced), checks each side for unit capacity violation. If found, returns a reduce-only `ProposedRebalance` (decrease resting to `unit_size - filled_in_unit`). The resulting cross-side imbalance is handled by existing rebalance logic in the next cycle.

2. **`max_profitable_price()` fallback** — New function in `fees.py`. When P18 blocks the snapshot catch-up price, computes the highest integer price where `fee_adjusted_cost(P) + fee_adjusted_cost(other_avg) < 100`. Uses this as a resting bid price instead of zeroing out the catch-up. If no profitable price exists (other side at extreme prices), catch-up is still skipped.

**Rationale:** The rebalance system had two independent safety concerns (cross-side balance and unit capacity) funneled through one resolution gate (`delta != 0`). Orthogonal invariants need independent resolution paths. For catch-up pricing, historical fills are sunk costs — the right question is "at what price can I profitably hedge?" not "is the snapshot price profitable?" A resting bid at max profitable price provides eventual hedging without guaranteeing a loss. See [[patterns#Max-profitable-price fallback for catch-up bids]].

## 2026-03-24 — Rename UI status "Settled" → "Balanced"

**Context:** The Status column showed "Settled" when both sides had equal fills and no resting orders. But Kalshi uses "settled" for a completely different lifecycle state (market closed, result determined, payout distributed). Having both meanings in the same UI caused confusion.
**Decision:** Renamed the UI status label to "Balanced". Updated `engine.py` (status computation), `event_review.py` (tooltip), `rebalance.py` (comments), and test fixtures. All references to Kalshi's actual settlement lifecycle (lifecycle_feed, settlement_tracker, models) left unchanged.
**Rationale:** Domain terminology must be unambiguous. "Balanced" is self-describing (equal fills, nothing resting) and doesn't collide with any Kalshi API concept.

## 2026-03-29 — Multi-pair ledger clobbering fix

**Context:** Temperature/crypto events have many threshold markets (B70, B72.5, B75) sharing one `event_ticker`. Each pair was sync'd against the same ledger — the last pair's empty resting list zeroed the owning pair's resting state. This caused: (1) table showing wrong resting counts, (2) overcommit detection vs resolution data mismatch, (3) rebalance unable to cancel excess orders.

**Decision:** Added `ticker_a`/`ticker_b` ownership to `PositionLedger` + `owns_tickers()` guard. `add_event()` skips if ledger already exists. Sync, reconciliation, and positions loops skip non-owning pairs.

**Rationale:** The ledger model assumes one pair per event_ticker. Multi-market events violate this. Rather than restructuring to one-ledger-per-pair (massive refactor), guarding the sync loop preserves the existing architecture while preventing cross-pair corruption.

## 2026-03-30 — Same-ticker settlement P&L: implicit revenue

**Context:** Performance panel showed -$17,523 when actual P&L was +$670. Root cause: Kalshi nets YES+NO positions on same-ticker markets at settlement, reporting `revenue=0`. The formula `revenue - cost - fees` treated 3,062 profitable settlements as massive losses.

**Decision:** Added `implicit_revenue = min(yes_count, no_count) * 100` to all P&L calculation sites (aggregate_settlements, settlement history day totals, event-level P&L). Each matched YES+NO pair settles at 100¢ regardless of outcome.

**Rationale:** Same-ticker arb buys both YES and NO. At settlement, one wins (100¢) and one loses (0¢), but Kalshi nets them to zero. The cost is still recorded. Without implicit revenue, every same-ticker settlement looks like a total loss.

## 2026-03-30 — Catch-up price: min(proposal, fresh_ask)

**Context:** Massive "arb not profitable" and "post only cross" floods. The catch-up price refresh replaced the proposal's `max_profitable_price` fallback (e.g., 1¢) with the raw orderbook ask (e.g., 55¢). This caused: (1) profitability check to fail (55¢ + 99¢ = 154¢ ≥ 100), (2) perpetual retry every 30s.

**Decision:** Changed to `min(proposal_price, fresh_ask)`. Never inflate above what the proposal computed as profitable. The 1¢ bid rests on the book waiting for a counterparty, rather than being blocked every cycle.

**Rationale:** The proposal already ran profitability math and computed the best viable price. The fresh price refresh should only LOWER the bid to avoid crossing — never raise it above the profitable threshold.

## 2026-03-31 — Volume gate for new pair entry

**Context:** One-sided exposure risk on illiquid markets where the second leg may never fill.

**Decision:** Added `MIN_VOLUME_24H = 50` (contracts) gate in `OpportunityProposer.evaluate()`. Uses `min(vol_a, vol_b)` — both sides need liquidity. Only blocks new entries; existing positions and catch-ups unaffected.

**Rationale:** User research direction — full volume-based strategy (including exit-only timing) deferred to Minerva simulation. This is the minimal safe default: markets with <50 contracts/day are extremely unlikely to complete a pair.

## 2026-04-01 — 409 market_closed → exit-only + auto-removal

**Context:** Determined/settled markets stuck in Talos because: (1) WS lifecycle events missed during disconnect/restart, (2) cancel attempts on closed markets failed with 409 but were silently swallowed, (3) stale resting in ledger prevented the "no resting" cleanup path.

**Decision:** Three-layer fix: (1) 409 `market_closed` on bid/adjustment → set exit-only. (2) Cancel failure with 409 → clear ledger resting state (orders don't exist anymore). (3) Exit-only cleanup: when resting=0 and unbalanced, check `close_time` (no API call) — if past, auto-remove. Close_time is reliable for non-sports.

**Rationale:** The original REST `get_market` fallback was rate-limited for 18+ events per cycle. Close_time check uses data already in memory. Combined with the ledger-clearing 409 handler, determined markets now flow through: detect → exit-only → cancel attempts → ledger cleared → close_time check → removed.

## 2026-04-01 — Queue-aware price improvement

**Context:** Partially-filled arb pairs with the behind side stuck deep in queue (ETA exceeds time remaining before game) had no automated response. A human trader would improve by 1c to leapfrog queue while remaining profitable.

**Decision:** `check_queue_stress()` runs every 30s cycle, compares ETA vs time_remaining for behind-side resting orders. Generates `ProposedQueueImprovement` proposals flowing through standard ProposalQueue. Execution uses `amend_order()` (atomic cancel+replace). Safety: profitability (P18), no spread crossing, one proposal per event. Repeat: each cycle re-evaluates, proposes next 1c increment until edge exhausted or ETA resolved.

**Rationale:** Reuses the supervised-automation pattern (detect → propose → approve → execute) and the amend_order mechanism from BidAdjuster. CPM=0 (dead market) treated as infinite ETA — being first at 42c beats being 186k back at 41c.

## 2026-04-03 — Use explicit signals, not absence of data

**Context:** `_compute_event_status` inferred "markets closed" from empty orderbooks (no best_ask on either side). When WS feed died after 7 hours, all books were empty/stale, causing every unbalanced pair to show "Balanced" — masking real imbalances.

**Decision:** Replaced the empty-book heuristic with explicit `_settled_markets` check from Kalshi's lifecycle WS feed. Only pairs where both markets have actually settled show "Settled" status.

**Rationale:** Empty orderbooks are ambiguous (stale WS, low liquidity, or truly closed). Kalshi provides explicit settlement signals via the lifecycle feed — use the authoritative source, not inferred state.

## 2026-04-03 — Code review fixes: five money-touching edge cases

**Context:** Automated code review flagged five issues in money-touching paths. Four confirmed, one partially false-positive (rebalance fresh-sync with monotonic guard).

**Fixes applied:**
1. **Amend fill delta** (`bid_adjuster.py:546`): Fill delta during approval windows now compared against `fresh_order.fill_count` (same order, pre-amend) instead of `ledger.filled_count(side)` (aggregate). Historical fills from other orders made the delta negative. See [[patterns#Order-specific APIs need order-specific data]].
2. **Rebalance fresh sync** (`rebalance.py:556-578`): Added `sync_from_positions` after `sync_from_orders` in the catch-up pre-placement sync. Mirrors the engine's full polling pattern. `sync_from_orders` monotonic guard (never decrease) means this is belt-and-suspenders, not critical.
3. **Catch-up ledger update** (`rebalance.py:668-673`): Added `record_placement()` after successful catch-up `create_order()`. Without it, another imbalance pass could repropose catch-up before next poll.
4. **Non-JSON error body** (`rest_client.py:73-78`): Wrapped `response.json()` in try/except — CloudFront/nginx HTML error pages no longer raise `JSONDecodeError`, instead route through `KalshiAPIError` for proper retry/notification handling.
5. **Seq-gap recovery scope** (`market_feed.py:70-91`): Unmapped tickers now scoped to their subscription batch, not swept globally. Prevents churn of unrelated in-flight subscriptions.

**Rationale:** All five are state-consistency fixes in money-touching paths. Principle 7 (Kalshi is source of truth) and Principle 15 (ledger accuracy) are the invariants being enforced.

## 2026-04-03 — Execution mode governance

**Context:** Multi-model council review identified that Talos's documented governance ("supervised — human approves everything") didn't match its actual runtime (168h auto-accept starting on mount with `_start_auto_accept(168.0)` in `app.py`). This mismatch became dangerous with `Talos.exe` distribution to other users.

**Decision:** Replaced `AutoAcceptState` with `ExecutionMode` state machine. Two modes: Automatic (intended default — proposals auto-approve) and Manual (override/debug). Optional `auto_stop_at` timer on automatic mode. Startup reads from `settings.json` as boot policy (never rewritten at runtime). Status bar shows three orthogonal dimensions: scan mode, execution mode, and data health — each always visible.

**Key design choices:**
- Manual mode is "manual proposal approval" only — safety flows (rebalance, catch-up, overcommit reduction) still auto-execute in both modes
- Data health (`DATA: LIVE`/`DATA: STALE`) driven by actual book freshness (60s threshold), not just WS connection state. Intentionally lower than the 120s orderbook recovery threshold
- WS disconnect banner coexists with status bar (never overwrites mode display)
- `accepted_count` is session-local, resets on every `enter_automatic()` call

**Rationale:** Separates three previously conflated concerns: startup policy, current execution state, and data health. Makes the actual operating mode honest and visible. See spec at `docs/superpowers/specs/2026-04-03-execution-mode-governance.md`.

## 2026-04-03 — Unit_size single source of truth

**Context:** `unit_size` had four different defaults across six locations: `AutomationConfig` (10, dead field), `__main__.py` (5), `BidAdjuster` (10), `PositionLedger` (10), `first_run.py` (5), `app.py` (10). Config drift is a control problem for distributed exe.

**Decision:** Created `DEFAULT_UNIT_SIZE = 5` constant in `automation_config.py`. All constructor defaults and fallback paths import from this single authority. Removed dead `unit_size` field from `AutomationConfig` (was never read by any production code). Tests pin their own explicit values.

**Rationale:** One constant, one source. Factory default (5) is conservative for new users. Runtime overrides come from `settings.json` via the existing persistence path.

## 2026-04-03 — Narrowed except Exception on money-touching paths

**Context:** `engine.py` had 27+ `except Exception` blocks, several on money-touching and state-sync paths. These silently swallowed errors that should surface to the operator (violating Principle 20: "inaction is a decision — make it visible").

**Decision:** Narrowed 5 Tier 1 sites to `(KalshiAPIError, KalshiRateLimitError, httpx.HTTPError)`. Unknown exceptions now escape instead of being silently logged. Tier 2 sites (enrichment, discovery, recovery) left as broad catches — correct resilience for non-critical paths.

**Sites narrowed:** positions_sync_failed, refresh_account outermost, top-up placement, side-B placement + compensating cancel, place_bids outermost.

**Side effect:** Three tests were using `RuntimeError` as mock side effects and only passed because the broad catch swallowed them. Updated to `KalshiAPIError(status_code=500, ...)` — realistic API failure types.

**Rationale:** The catch set matches the actual REST boundary (`rest_client.py`): typed Kalshi errors + httpx transport errors are the operational failures at this layer. Anything else is a bug that should crash loudly.

## 2026-04-03 — Non-JSON 200 success-path hardening

**Context:** `rest_client.py:84` called `response.json()` unconditionally on 2xx responses. The error path (≥400) already handled non-JSON bodies gracefully, but a CloudFront HTML 200 (maintenance page, proxy redirect) would crash with an unhandled `JSONDecodeError`, halting the entire polling loop.

**Decision:** Wrapped the success-path `response.json()` in `try/except (ValueError, UnicodeDecodeError)`, raising `KalshiAPIError` with the raw text. Mirrors the exact pattern already used for error bodies.

**Rationale:** Preserves the typed failure path so the engine's existing `except KalshiAPIError` handlers process it — not just "don't crash" but "crash through the right channel."

## 2026-04-27 — CPM/ETA per-side granularity + test-suite speedup

**Context:** `CPMTracker` was per-ticker only — every trade on a ticker collapsed into one stream. The doc spec at `docs/KALSHI_CPM_AND_ETA.md` had always called for per-(ticker, outcome, book_side, price) granularity, but the implementation never caught up. This blocked the upcoming DRIP/BLIP POC, which needs per-side fill-rate ETA to decide which side is racing.

**Decision (PR #4):**
- Refactored `CPMTracker._events` from `dict[str, list[(ts, float_qty)]]` → `dict[FlowKey, list[(ts, int_count_fp100)]]`. `FlowKey` is a frozen dataclass `(ticker, outcome, book_side, price_bps)`.
- Each trade decomposes into TWO flow events using the doc's complement rule: `taker_side == outcome → ASK hit at outcome's price; else → BID hit at the complement price`. Source: structlog `taker_side` field, normalized to `Trade.side` during validation.
- `eta_minutes` signature is backward-compatible: `(ticker, queue_position, outcome=None, book_side=None, price_bps=None, window_sec=300.0)`. Old callers (`engine.py:2915`, `position_ledger.py:1741-1747`) keep working via aggregate fallback; new callers pass the bucket for per-side ETA.
- Three-level fallback chain in `eta_minutes`: exact bucket → drop price_bps → drop book_side → return None. Deliberately do NOT fall back to bare-ticker aggregate — after the C1 fix the bare aggregate iterates `outcome=="yes"` only (to avoid double-counting since each trade decomposes into both yes and no buckets), so falling back from a `outcome="no"` query to it would return a yes-side fill rate. Cross-side trap closed.
- Engine call site at `engine.py:2915` updated to pass `outcome="no", book_side="BID", price_bps=resting_price_bps_val` for per-side stale-position detection. Introduced a separate `resting_price_bps_val = ledger.resting_price_bps(behind_side)` local because `ledger.resting_price()` is the legacy cents accessor (still used by surrounding cents-based logic).

**Test-suite speedup (same PR):** Default dev run dropped from **13:20 → 0:31** (96% reduction) with three surgical fixes:
1. `_make_engine` test fixture pre-arms `_ready_for_trading` event (engine.py:1340 startup gate); 10 tests dropped from 30s each to 0.01s each.
2. `scan_series_failed` in `game_manager.py:867-871` logs `error_type + error_msg` instead of `exc_info=True`. With 50+ sports series failing concurrently during a Kalshi outage, serializing 50+ tracebacks to structlog's print sink hit the Windows cp1252 encoding-retry path — `test_scan_handles_api_failure` was 190s. Now 0.01s. Also a modest production win during real outages.
3. New `slow` pytest marker registered in `pyproject.toml`; default `addopts = "-m 'not slow'"`. `test_freeze_diagnosis.py` (23 tests, ~58s) intentionally simulates hung REST/WS scenarios — opted out of default dev runs but still in CI via `pytest -m ""`.

**Other meta-changes (PR #4 + PR #6):**
- 20 module docstrings added to test files that lacked them.
- Deleted `tests/test_tree_screen_skeleton.py` — 19 LOC, 1 test verifying TreeScreen could be instantiated; covered implicitly by `test_tree_screen_render.py` actually rendering.
- Suppressed pre-existing `reportUnreachable` pyright warning on the defensive `isinstance` check in `engine.add_market_pairs` (intentional runtime guard).
- CLAUDE.md: cardinal-rule section now explicitly points at the kalshi-mcp server as the mechanism for upholding the rule; Development Commands section reflects the new pytest invocations.

**Rationale:** Per-side ETA unlocks the DRIP/BLIP POC's BLIP threshold (operator-set `BLIP_DELTA_MIN` minutes between behind-side and ahead-side fill ETAs) — see [redesign spec](../docs/superpowers/specs/2026-04-26-drip-staggered-arb-redesign.md) and [DRIP plan](../docs/superpowers/plans/2026-04-26-drip-blip-poc.md). Test speedup is an independent operational win — daily dev iteration was burning 13 minutes per run on what turned out to be ONE shared startup-gate timeout in the test fixture, with another 3 minutes on a structlog traceback explosion specific to the test scenario. Both fixes were single-character or single-line changes once the root cause was found.

**Source-of-truth invariant:** The C1 fix (bare-ticker aggregate iterates yes-only) preserves backward-compat *numerical* semantics for legacy callers — without it, every legacy call would have silently doubled CPM and halved ETA, breaking stale-detection thresholds. Caught by code review before merge; locked by `test_ticker_aggregate_counts_each_trade_once_via_ingest`.

## 2026-04-28 — DRIP redesigned as insertion-strategy parameter

Replaced the parallel DRIP pipeline (DripController + `_drive_drip`
owning seed/replenish/place + 7 `is_drip` gates blocking the standard
pipeline) with a single `per_side_max_ahead(ledger, side, drip_config)`
helper routed through every per-side cap site (rebalance overcommit,
top-up, post-cancel safety, reconcile, proposer qty). DRIP events now
flow through the standard pipeline; `_drive_drip` is BLIP-only.

Snap-to-cap on toggle handled by adding the event to `_dirty_events`;
the next `check_imbalances` cycle calls `compute_overcommit_reduction`,
which reads the new cap via the helper and cancels surplus resting
down to it.

`DripController` class removed; `evaluate_blip` is a free function in
`drip.py`. `DripConfig.per_side_contract_cap` renamed to
`max_ahead_per_side` for cross-strategy consistency.

Frozen-row symptom from 2026-04-28 (KXTRUMP-…-PELO row 143) resolved:
under the new model, the standard pipeline's catch-up exception
covers the queue-bumped behind-side independent of ETA-gap signals.

**Spec:** `docs/superpowers/specs/2026-04-28-drip-redesign-design.md`
**Plan:** `docs/superpowers/plans/2026-04-28-drip-redesign-plan.md`
