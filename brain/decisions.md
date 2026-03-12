# Decisions

Record significant technical decisions here.

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
**Decision:** 3s polling via `refresh_queue_positions`, conservative merge cache (`_merge_queue`), only positive values cached/displayed. Zero from API means "no data", not "front of queue".
**Rationale:** Queue position only improves (monotonically decreasing). Conservative merge (keep smallest positive value) handles data artifacts from API version inconsistencies. See [[patterns#Enrichment caching with split polling cadence]].

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

**Context:** Planning automated bid adjustment when resting orders get "jumped" (outbid). A previous attempt at similar automation failed because the system lost track of both resting orders and fills, leading to cascading over-placement on one side — the exact failure mode that delta-neutral arbitrage cannot tolerate.

**Decision:** Established a set of structural safety rules that must be enforced before any bid adjustment automation is implemented. These are captured as Principles 15–19.

Key design choices and why:
- **Unit-based atomic bidding** (Principle 16): Orders are placed in fixed units (10 contracts). A "pair" is one unit per side. No new pair until both sides fully fill. This prevents the "just place a few more" drift that caused the previous failure.
- **Amend over cancel-and-replace** (Principle 17): Use the Kalshi amend API (`POST /portfolio/orders/{id}/amend`) to change price in a single atomic call. The previous system used cancel-then-place as two separate operations, creating timing windows where the position check saw inconsistent state and triggered further placements. Amend eliminates this class of bug entirely — there's never a moment with zero or two orders on the same side. For partial fills, amend moves only the unfilled portion to the new price queue.
- **Fee-adjusted profitability gate** (Principle 18): Every bid placement or amendment must pass a fee-adjusted arb check. This prevents chasing a jumped price into unprofitable territory.
- **Most-behind-first tiebreaker** (Principle 19): When both sides have partial fills and both get jumped, the side needing more fills adjusts first. This minimizes worst-case delta.
- **Semi-auto first, then full-auto** (Principle 2): The system will propose actions for human approval before executing. Graduation to full-auto only after trust is established through observation.
- **Fractional completion bids**: Partial fills may be topped up with a fractional bid at the new price, provided total resting + filled ≤ 1 unit and the arb remains profitable.

**Rationale:** Every rule traces back to a specific failure mode from the previous system or a worst-case scenario analysis. The goal is to make unsafe states structurally impossible rather than relying on runtime checks that can be bypassed by timing issues. See [[principles#15. Position Accuracy Is Non-Negotiable]] through [[principles#19. Most-Behind-First on Dual Jumps]].

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

**Context:** User had 129 contracts committed on side A vs 107 on side B — a 22-contract imbalance exceeding the 10-contract unit size. Delta neutrality is critical for arb safety. Initially shipped detection-only (approving dismissed without acting). Extended with a two-step executable rebalance after fill imbalances from runaway bidding showed "manual action needed" on every approval.
**Decision:** Two-step rebalance maintaining delta neutrality at every intermediate state:
1. **Reduce over-side resting** (cancel or amend) — shrinks the larger side first
2. **Catch-up bid on under-side** — grows the smaller side (capped at one unit, requires book price > 0 and no existing resting on under-side)

Equalization target: `max(over_filled, under_committed)` — the minimum both sides can converge to (can't unfill, can't cancel under-side orders). If step 1 fails, step 2 is skipped (fail-safe). Step 2 passes through `is_placement_safe()` before placing. Multi-cycle convergence for gaps > unit_size.
**Rationale:** Executing reduce-first/catch-up-second means the imbalance either stays the same (step 2 skipped) or decreases — never temporarily increases. The equalization formula naturally handles all five scenarios: cancel+catchup, catchup-only, cancel-only, partial-reduce, and multi-cycle convergence. See [[principles#16. Delta Neutral by Construction]] and [[principles#22. End-to-End Before Done]].

## 2026-03-10 — Status column for engine decision transparency (P20)

**Context:** Operator couldn't tell why Talos wasn't proposing entry on certain tickers. The system was making correct decisions (low edge, cooldown, waiting for side to catch up) but the reasoning was invisible.
**Decision:** Added `_compute_event_status()` to `TradingEngine` and a "Status" column to `OpportunitiesTable`. Each gate in the proposer pipeline maps to a visible status string: "Low edge", "Stable Xs", "Cooldown Xs", "Filling (B -5)", "Waiting A (-3)", "Need bid A/B", "Proposed", "Sug. off", "Discrepancy", "Imbalanced", "Ready".
**Rationale:** Direct application of Principle 20 (Inaction Is a Decision — Make It Visible). The operator should never wonder "is Talos broken or deliberately waiting?" — every non-action has a visible reason. Status is computed fresh each refresh cycle from current engine state. See [[principles#20. Inaction Is a Decision — Make It Visible]].

## 2026-03-10 — Runaway bidding: safety gate wiring and Kalshi-as-truth

**Context:** Live runaway bidding — positions showed 10/20 and 10/30 (2-3x the intended unit of resting orders per side). Three compounding gaps: (1) `is_placement_safe()` existed and was well-tested but was never called from any bid placement path; (2) after placing orders, the ledger wasn't updated until the next API sync (~10s), so the proposer saw stale state (resting=0) and created duplicate proposals; (3) after approving a proposal, the stability timer wasn't reset, allowing immediate re-proposal.

**Decision:** Applied two fixes (not three — the third was tried and reverted):
- **Fix 1: Hard safety gate in `place_bids()`** — calls `is_placement_safe()` on both sides before sending any orders. Also updated `is_placement_safe()` to use modular arithmetic (`filled_count % unit_size`) so re-entry is allowed after a complete unit while still blocking duplicates.
- **Fix 2: Stability reset on approval** — `record_approval()` on `OpportunityProposer` resets the stability timer after a bid is approved, forcing the proposer to re-observe stable edge for `stability_seconds` before re-proposing. This covers the sync gap between placement and the next `sync_from_orders`.
- **Reverted: Optimistic ledger update** — initially added `record_resting()` calls after each `create_order`, but this caused false discrepancies when Kalshi's API hadn't reflected the new order yet ("ledger has resting order X, kalshi shows none"). Removed in favor of trusting Kalshi as source of truth (P7/P21). The hard gate + stability reset are sufficient without it.

**Rationale:** The runaway happened because `is_placement_safe()` was built during Phase 3 (PositionLedger) but the bid placement path (`place_bids`) predated it and was never wired. The optimistic update was a tempting defense-in-depth layer but violated P7 (Trust Kalshi) — it created a temporary mismatch between ledger and API that triggered `sync_from_orders` discrepancy detection, generating confusing HOLD proposals. Two defenses (hard gate + stability reset) are sufficient: the gate catches structural violations, and the stability reset provides the time buffer for `sync_from_orders` to catch up. See [[principles#1. Safety Above All]], [[principles#16. Delta Neutral by Construction]], [[principles#7. Kalshi Is the Source of Truth — Always]].

## 2026-03-10 — Positions API as second authoritative source for fills

**Context:** DEDGAL event had 30+10 fills and 2 resting orders on Kalshi, but Talos showed all dashes. Root cause: `GET /portfolio/orders` archives old executed/cancelled orders. When filled orders are archived (no longer returned), `sync_from_orders` computes 0 fills → ledger empty → UI shows dashes. This is the worst failure mode for P7/P15 — the operator is completely blind to real positions.
**Decision:** Added `GET /portfolio/positions` as a second data source in `refresh_account`. The positions endpoint returns `position` (signed contract count, negative = NO contracts) and `total_traded` — and crucially, **never archives**. `sync_from_positions()` patches fill counts when they exceed what orders reported. Runs after `sync_from_orders` in every refresh cycle.
**Rationale:** Two complementary data sources cover each other's gaps: orders API gives per-order detail (prices, IDs, resting status) but archives; positions API gives aggregate counts (total fills) and never archives. Running orders-first then positions-second gives the best of both. The `sync_from_positions` method only ratchets fills upward (never decreases), so it can't introduce false data. See [[principles#7. Kalshi Is the Source of Truth — Always]] and [[principles#15. Position Accuracy Is Non-Negotiable]].

**Addendum (same day):** The initial implementation still had two bugs: (1) `sync_from_orders` flagged a "fill decrease" discrepancy when orders-API fills (0, due to archival) were less than positions-augmented fills (10) — a false positive every cycle. (2) Two resting orders on the same side (valid on Kalshi) were flagged as a discrepancy. Fix: rewrote `sync_from_orders` to use **monotonic fills** (never decrease, take max of orders vs current ledger) and **sum multiple resting orders** instead of flagging. Removed all discrepancy-setting from `sync_from_orders` — the two-source pattern is self-healing. See [[patterns#Monotonic state updates across data sources]].

## 2026-03-10 — Verify after every order action

**Context:** Rebalance step 1 (amend) returned `AMEND_ORDER_NO_OP` — the order was already at the target count due to fills between proposal and execution. The old code treated this as a hard error, showed a failure toast, and returned early. The next `check_imbalances` cycle saw delta < unit_size and skipped — never confirming the action's outcome. The user's insight: the review must verify reality, not trust the model.
**Decision:** Added `_verify_after_action()` — a full two-source sync (orders + positions) that runs immediately after every order action (rebalance, adjustment, bid). Also handle `AMEND_ORDER_NO_OP` as success via `_is_no_op()` helper. Wired into `approve_proposal` for all three action types.
**Rationale:** The 10s polling cycle is too slow to confirm action outcomes. The system was assuming success/failure based on the API response alone, without verifying the resulting state. Immediate verification means the ledger reflects reality within milliseconds of acting, not 10 seconds later. See [[patterns#Verify after every order action]] and [[principles#7. Kalshi Is the Source of Truth — Always]].

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

## 2026-03-07 — Bid modal falls back to all_snapshots

**Context:** After placing orders, users couldn't reopen the bid modal on the same game. `on_data_table_row_selected` called `scanner.get_opportunity()` which only returns pairs with positive raw edge. After fills move the market, edge drops to 0 or negative — the row stays visible (from `all_snapshots`) but clicking it silently did nothing.
**Decision:** Fall back to `scanner.all_snapshots` when `get_opportunity()` returns None. See [[codebase/index#Gotchas]] "Don't gate UI actions on volatile data."
**Rationale:** The table is built from `all_snapshots` (all monitored pairs), so the click handler must use the same data source. Users should be able to place bids on any monitored pair regardless of current edge. See [[principles#2. Human in the Loop]].
