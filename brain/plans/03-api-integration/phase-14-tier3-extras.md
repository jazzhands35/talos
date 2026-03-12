# Phase 14 — Tier 3 Extras

Back to [[plans/03-api-integration/overview]]

## Goal

Implement remaining nice-to-have items: market_positions WS (fallback cross-check), top-of-book sizes on Market, account limits, user_data_timestamp, and list_subscriptions. All are low-risk additions that round out API coverage.

## Changes

### market_positions WS channel
- Wire into PortfolioFeed (or create separate handler)
- Subscribe globally, cache latest `MarketPositionMessage` per ticker
- `realized_pnl` cross-check: compare against ledger's computed P&L each refresh cycle
- Log discrepancies but don't act on them — this is observability only

### Top-of-book sizes on Market model
- Add `yes_bid_size: int | None = None` and `yes_ask_size: int | None = None` to `Market` model
- Add FP migration for `yes_bid_size_fp` and `yes_ask_size_fp`
- Display in UI (informational — "150 contracts at best bid")

### GET /account/limits
- Add `get_account_limits() -> dict` to REST client
- Returns tier, read/write limits per second
- Log at startup for awareness
- Optionally display in AccountPanel

### GET /exchange/user_data_timestamp
- Add `get_user_data_timestamp() -> str` to REST client
- Log after `_verify_after_action()` to confirm data freshness
- Diagnostic only — not used for control flow

### list_subscriptions WS command
- Already added to ws_client in Phase 1
- Add a debug method to engine: `debug_subscriptions()` that sends the command and logs the response
- Could be wired to a debug keybinding in the TUI

## Data Structures

- `Market` gains: `yes_bid_size: int | None`, `yes_ask_size: int | None`
- `MarketPositionMessage` from Phase 1 used for WS channel
- No new complex types

## Verification

### Static
- `pyright` passes
- `ruff` passes

### Runtime
- Unit test: `Market` model parses `yes_bid_size_fp` correctly
- Unit test: `get_account_limits()` returns parsed response
- Unit test: `get_user_data_timestamp()` returns timestamp string
- Unit test: market_positions WS handler caches latest data and cross-checks P&L
- Manual test: start Talos, verify account limits logged at startup
