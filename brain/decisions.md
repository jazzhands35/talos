# Decisions

Record significant technical decisions here.

## 2026-03-03 — Pure state + async orchestrator split

Split into pure state machine + async orchestrator. See [[patterns#Pure state + async orchestrator split]] and [[principles#13. Test Purity Drives Architecture]].

## 2026-03-06 — Fee model and scanner integration

**Context:** Kalshi's 1.75% maker fee on profit significantly affects the real edge. Fee math was needed in edge calculations, position P&L, display columns, and effective odds.
**Decision:** Created `src/talos/fees.py` as a pure utility module with zero dependencies. Scanner computes both `raw_edge` and `fee_edge` via `fee_adjusted_edge()`. Display uses `fee_edge`; raw edge is kept for reference.
**Rationale:** Single source of truth for fee math. Pure functions are trivially testable and composable. Used by scanner (edge), position.py (locked profit), and widgets (display). See [[principles#14. Parse at the Boundary]]. Reference spec: `docs/KALSHI_POSITION_AND_PNL.md`.

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
**Rationale:** Two systems deriving from the same data can disagree due to timing or implementation drift. If the UI shows "10 filled on side A" but the safety gate thinks it's 8, the operator can't trust either. A single source of truth means if the UI looks right, the safety logic is right — and if it's wrong, the operator sees it immediately. The risk of a larger blast radius (changing the UI data source) is worth the guarantee of consistency. See [[principles#15. Position Awareness Before Action]].

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

**Rationale:** Every rule traces back to a specific failure mode from the previous system or a worst-case scenario analysis. The goal is to make unsafe states structurally impossible rather than relying on runtime checks that can be bypassed by timing issues. See [[principles#15. Position Awareness Before Action]] through [[principles#19. Most-Behind-First on Dual Jumps]].

## 2026-03-08 — TradingEngine extraction and position unification

**Context:** `TalosApp` was a 481-line god class owning subsystem references, mutable caches (queue, orders, CPM), and all polling/action methods. Position display was computed by `compute_event_positions()` from raw orders, separate from `PositionLedger` which drove safety gates — two systems deriving from the same data.

**Decision:** Extract `TradingEngine` as a headless orchestrator (395 lines) owning all business logic. Slim `TalosApp` to a thin UI shell (196 lines) that delegates via callbacks. Unify position computation: `compute_display_positions()` reads from `PositionLedger` (the safety source of truth), replacing the deleted `compute_event_positions()` and `position.py`.

Key changes:
- **Engine owns state**: Queue cache, orders cache, CPM tracker, balance, position summaries all live in `TradingEngine`
- **Callback-based UI**: Engine communicates via `on_notification(message, severity)`. App wires this to Textual toasts
- **Single position truth**: `compute_display_positions()` lives in `position_ledger.py`, reads ledger state, enriches with queue/CPM data
- **BidAdjuster encapsulation**: Replaced 4 `ledger._sides[side]` accesses with public accessors

**Rationale:** Engine extraction enables headless testing of all business logic without Textual. Position unification eliminates the risk of UI and safety gates disagreeing. The callback pattern keeps the engine framework-agnostic — a future web UI or API could use the same engine. See [[principles#13. Test Purity Drives Architecture]].

## 2026-03-07 — Bid modal falls back to all_snapshots

**Context:** After placing orders, users couldn't reopen the bid modal on the same game. `on_data_table_row_selected` called `scanner.get_opportunity()` which only returns pairs with positive raw edge. After fills move the market, edge drops to 0 or negative — the row stays visible (from `all_snapshots`) but clicking it silently did nothing.
**Decision:** Fall back to `scanner.all_snapshots` when `get_opportunity()` returns None. See [[codebase/index#Gotchas]] "Don't gate UI actions on volatile data."
**Rationale:** The table is built from `all_snapshots` (all monitored pairs), so the click handler must use the same data source. Users should be able to place bids on any monitored pair regardless of current edge. See [[principles#2. Human in the Loop]].
