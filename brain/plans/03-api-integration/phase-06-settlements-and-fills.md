# Phase 6 — Settlements and Fills REST Endpoints

Back to [[plans/03-api-integration/overview]]

## Goal

Fix the Settlement model to match the actual API, implement `GET /portfolio/settlements` and enhance `GET /portfolio/fills` usage. Settlements provide Kalshi's authoritative P&L (P7/P21) and fills with `fee_cost` enable startup catch-up.

## Changes

### src/talos/models/portfolio.py
- Rewrite `Settlement` model to match actual API fields:
  - `ticker`, `event_ticker`, `market_result` (yes/no/scalar/void)
  - `revenue: int` (cents — integer, NOT dollars string!)
  - `fee_cost: int` (cents — converted from dollars string, note mixed units in same response)
  - `yes_count: int`, `no_count: int` (from `_fp` strings)
  - `yes_total_cost: int`, `no_total_cost: int` (from `_dollars` strings)
  - `settled_time: str`
  - `value: int | None` (per-contract payout, cents)
  - Validator must handle the unit mismatch: `revenue` is already cents int, `fee_cost` is dollars string

### src/talos/rest_client.py
- Add `get_settlements()` method:
  - Params: `ticker`, `event_ticker`, `min_ts`, `max_ts`, `limit`, `cursor`, `subaccount`
  - Paginated, returns `list[Settlement]`
  - Response key: `data["settlements"]`
- Update `get_fills()` — already implemented but enhance the `Fill` model (done in Phase 2)

### src/talos/engine.py
- Add `get_settlement_history(event_ticker: str | None = None) -> list[Settlement]` convenience method
- On settlement detection (manual query or future lifecycle WS in Phase 10):
  - Compare Kalshi's settlement P&L against computed `scenario_pnl()`
  - Log any discrepancy with full detail for calibrating estimators
- Startup: optionally fetch recent settlements to populate P&L history

### Startup catch-up via fills
- In `start_feed()` or `refresh_account()`, after discovering active events:
  - Call `get_fills(ticker=...)` for each monitored market
  - Use fill data to seed the position ledger for events where orders were archived
  - This supplements `sync_from_positions` — fills give per-trade detail while positions give aggregate counts

## Data Structures

- `Settlement`: `ticker`, `event_ticker`, `market_result`, `revenue` (cents int), `fee_cost` (cents from dollars), `yes_count` (int), `no_count` (int), `yes_total_cost` (cents), `no_total_cost` (cents), `settled_time`, `value` (cents | None)

## Verification

### Static
- `pyright` passes
- `ruff` passes

### Runtime
- Unit test: `Settlement` model parses mixed-unit response correctly — `revenue: 1000` stays as 1000 (already cents), `fee_cost: "0.0770"` → 8 (rounded cents)
- Unit test: `get_settlements()` returns parsed list, handles pagination cursor
- Unit test: settlement P&L vs `scenario_pnl()` comparison — construct matching data, verify agreement
- Unit test: startup fill catch-up seeds ledger correctly for archived orders
- Manual test: query settlements for a known settled event, verify numbers match Kalshi dashboard
