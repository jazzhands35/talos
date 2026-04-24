# Phase 14 — Tier 3 Extras

Back to [[plans/03-api-integration/overview]]

## Goal

Triage and selectively implement remaining nice-to-have items from the API audit.

## Decisions (2026-03-12)

### market_positions WS channel — DONE
- New `PositionFeed` handler (separate from PortfolioFeed — different channel, different semantics)
- Subscribes globally, caches latest `MarketPositionMessage` per ticker
- Cross-checks position counts and fees against ledger each refresh cycle
- Logs mismatches via structlog but never acts on them — pure observability

### Top-of-book sizes on Market model — SKIPPED
- Redundant with OrderBookManager's full depth data
- Queue position + CPM/ETA already provide more actionable fill-time info

### GET /account/limits — SKIPPED
- Polling cadences are hand-tuned and well under limits
- Info available in Kalshi dashboard; no runtime use case

### GET /exchange/user_data_timestamp — SKIPPED
- Verify-after-action already re-fetches orders and checks fills
- No observed "Kalshi silently didn't process" bugs to justify the extra API call

### list_subscriptions WS command — DEFERRED
- WS command already exists in ws_client (Phase 1)
- Engine debug wrapper + TUI keybinding deferred until needed for debugging
