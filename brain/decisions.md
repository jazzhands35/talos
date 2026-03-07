# Decisions

Record significant technical decisions here.

## 2026-03-03 — Pure state + async orchestrator split

**Context:** OrderBookManager needs to apply snapshots/deltas (pure logic), while MarketFeed needs to subscribe via WS and route messages (async I/O).
**Decision:** Split into two classes — pure state machine (no async) and async orchestrator (no state logic). Applied in Layer 2 (OrderBookManager/MarketFeed) and reused in Layer 3 (ArbitrageScanner/GameManager).
**Rationale:** Pure state machine is trivially testable without mocks. Async surface area stays minimal. See [[patterns#Pure state + async orchestrator split]] and [[principles#13. Test Purity Drives Architecture]].

## 2026-03-06 — Fee model and scanner integration

**Context:** Kalshi's 1.75% maker fee on profit significantly affects the real edge. Fee math was needed in edge calculations, position P&L, display columns, and effective odds.
**Decision:** Created `src/talos/fees.py` as a pure utility module with zero dependencies. Scanner computes both `raw_edge` and `fee_edge` via `fee_adjusted_edge()`. Display uses `fee_edge`; raw edge is kept for reference.
**Rationale:** Single source of truth for fee math. Pure functions are trivially testable and composable. Used by scanner (edge), position.py (locked profit), and widgets (display). Reference spec: `docs/KALSHI_POSITION_AND_PNL.md`.

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

## 2026-03-07 — Bid modal falls back to all_snapshots

**Context:** After placing orders, users couldn't reopen the bid modal on the same game. `on_data_table_row_selected` called `scanner.get_opportunity()` which only returns pairs with positive raw edge. After fills move the market, edge drops to 0 or negative — the row stays visible (from `all_snapshots`) but clicking it silently did nothing.
**Decision:** Fall back to `scanner.all_snapshots` when `get_opportunity()` returns None. See [[codebase/index#Gotchas]] "Don't gate UI actions on volatile data."
**Rationale:** The table is built from `all_snapshots` (all monitored pairs), so the click handler must use the same data source. Users should be able to place bids on any monitored pair regardless of current edge.
