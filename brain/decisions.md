# Decisions

Record significant technical decisions here.

## 2026-03-03 — Pure state + async orchestrator split

**Context:** OrderBookManager needs to apply snapshots/deltas (pure logic), while MarketFeed needs to subscribe via WS and route messages (async I/O).
**Decision:** Split into two classes — pure state machine (no async) and async orchestrator (no state logic). Applied in Layer 2 (OrderBookManager/MarketFeed) and reused in Layer 3 (ArbitrageScanner/GameManager).
**Rationale:** Pure state machine is trivially testable without mocks. Async surface area stays minimal. See [[patterns#Pure state + async orchestrator split]] and [[principles#14. Test Purity Drives Architecture]].

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
