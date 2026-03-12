# Plan 03 — Kalshi API Integration Expansion

Back to [[plans/index]]

## Context

Talos currently uses 10 of 40+ REST endpoints and 1 of 10 WebSocket channels. A comprehensive API audit (March 2026) revealed significant gaps in data utilization — from missing real-time fill notifications (causing 10s sync gaps that contributed to runaway bidding) to unused authoritative fields on responses already being fetched. The single biggest failure mode in production — getting trapped with one-sided exposure on fast-moving markets — is partly caused by lacking the real-time data Kalshi provides.

## Scope

**In scope:**
- All Tier 1–3 items from the API audit
- New WS channel subscriptions (user_orders, fill, ticker, market_lifecycle_v2, market_positions)
- WS infrastructure upgrades (update_subscription, sequence gap recovery, bulk subscribe)
- Model enrichment (Order, Fill, Market, Series, EventPosition, Settlement fields)
- REST client additions (settlements, fills with fee_cost, fee_changes, event filtering)
- Order creation improvements (post_only, cancel_order_on_pause)
- Engine wiring for all new data sources

**Out of scope:**
- ESPN API integration (future plan)
- RFQ/Quotes (institutional scale, not needed at current volume)
- Historical data endpoints (archived orders/fills)
- Exchange schedule polling
- FIX protocol
- Subaccount management
- Auto-discovery scanner (using min_close_ts for discovery is in scope; building a full auto-scanner is not)
- Autopilot v2 integration (this plan targets the Talos TUI system)

## Constraints

- **P7/P15:** Kalshi is source of truth. New WS data sources supplement, never replace, REST polling reconciliation
- **P13:** Pure state + async orchestrator split. New handlers follow MarketFeed pattern — pure state machine + async wiring
- **P14:** Parse at the boundary. All new data flows through Pydantic models with validators
- **P4:** Subtract before you add. Don't build abstractions until patterns emerge across channels
- **P20:** Inaction is visible. New data sources must surface their state (connected, receiving, stale)
- **Single callback per channel:** ws_client supports one callback per channel name — new channels each get their own handler class

### Alternatives Considered

**WS-first vs REST-first:** Could prioritize model/REST fixes (simpler) or WS channels (higher impact). Chose WS infrastructure first because: (a) items 1-2 are the highest-impact safety improvements, (b) model changes are prerequisites for WS message parsing, (c) REST improvements layer naturally on top.

**Monolithic WS handler vs per-channel handlers:** Could route all new channels through a single dispatcher or create separate handler classes like MarketFeed. Chose per-channel handlers (PortfolioFeed for user_orders+fill+market_positions, TickerFeed for ticker, LifecycleFeed for market_lifecycle_v2) because they follow the existing pattern, keep each handler focused, and allow independent testing.

**Replace polling with WS vs supplement:** Could try to replace REST polling entirely with WS updates. Chose supplement (belt-and-suspenders) because: (a) WS connections drop, (b) P15 demands multiple data sources cross-checking, (c) the polling reconciliation loop is proven and catches WS gaps.

## Applicable Skills

- `safety-audit` — after phases touching order placement or position tracking
- `test-runner` — after every phase
- `position-scenarios` — after phases affecting position state or fill tracking
- `strategy-verify` — after phases affecting edge/fee calculations

## Phases

### Tier 1 — Safety, Accuracy, and Core Trading
- [[plans/03-api-integration/phase-01-ws-infrastructure]] — WS client upgrades (message registry, update_subscription, seq gap recovery)
- [[plans/03-api-integration/phase-02-order-model-enrichment]] — Add maker_fill_cost, post_only, cancel_order_on_pause to Order model and REST client
- [[plans/03-api-integration/phase-03-portfolio-feed]] — New PortfolioFeed handler for user_orders + fill WS channels
- [[plans/03-api-integration/phase-04-portfolio-feed-wiring]] — Wire PortfolioFeed into engine, update ledger from WS events
- [[plans/03-api-integration/phase-05-ticker-feed]] — New TickerFeed handler for ticker WS channel
- [[plans/03-api-integration/phase-06-settlements-and-fills]] — GET /portfolio/settlements, GET /portfolio/fills with fee_cost, fix Settlement model
- [[plans/03-api-integration/phase-07-event-position-enrichment]] — Capture rich EventPosition fields, min_close_ts filter
- [[plans/03-api-integration/phase-08-leaner-polling]] — event_ticker filter on GET /portfolio/orders

### Tier 2 — Strategy and Operational Awareness
- [[plans/03-api-integration/phase-09-fee-schedule]] — GET /series/fee_changes, Series model fee_type/fee_multiplier, dynamic fee rates
- [[plans/03-api-integration/phase-10-lifecycle-feed]] — market_lifecycle_v2 WS channel (settlements, pauses, new markets)
- [[plans/03-api-integration/phase-11-market-model-enrichment]] — settlement_ts, cancel_order_on_pause usage, bulk WS subscribe
- [[plans/03-api-integration/phase-12-order-groups]] — Server-side unit size enforcement via Order Groups
- [[plans/03-api-integration/phase-13-decrease-order]] — POST /portfolio/orders/{id}/decrease for cleaner quantity reduction

### Tier 3 — Nice to Have
- [[plans/03-api-integration/phase-14-tier3-extras]] — market_positions WS, top-of-book sizes, account limits, user_data_timestamp, list_subscriptions

## Verification

```bash
# After every phase:
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check src/ tests/
.venv/Scripts/python -m pyright

# After WS phases: manual smoke test
# 1. Start Talos in demo mode
# 2. Add a game
# 3. Verify WS messages appear in structlog output
# 4. Place a bid, verify fill notification arrives via WS
# 5. Check positions panel updates from WS (not just polling)
```
