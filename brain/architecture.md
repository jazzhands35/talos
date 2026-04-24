# Architecture

Talos is a Kalshi arbitrage trading system designed for progressive automation.

## Design Philosophy

**Manual-first:** The system starts with full manual control over trade decisions. Automation is added incrementally as confidence grows. The human is always able to override or intervene.

## Domain Concepts

### Arb Modes
Two arb modes, mathematically identical (`100 - price_a - price_b`):

- **Cross-NO (sports):** Two markets in one event. Buy NO on both. `side_a="no", side_b="no"`, different tickers. Event ticker = Kalshi event ticker.
- **YES/NO (non-sports):** One market. Buy YES + NO. `side_a="yes", side_b="no"`, same ticker. Event ticker = market ticker (unique key), `kalshi_event_ticker` = real Kalshi event ticker for API calls. `sync_from_positions` skipped (Kalshi nets YES+NO to zero).

Sports markets can be blocked via `sports_enabled=False` in `AutomationConfig`.

### Unit
The atomic bidding quantity. Currently **20 contracts** in production (default 10). Configurable, but always a fixed integer. All order placement, position tracking, and safety checks are denominated in units. A "pair" is one unit on side A and one unit on side B of the same event.

### Money units (bps / fp100)

Shipped 2026-04-23 via PR #1 ([bps/fp100 unit migration](../docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md)). Every money value in the core trading path is an integer in **basis points** (`$1 = 10,000 bps`, `1¢ = 100 bps`); every contract count is an integer in **fp100** (`1 contract = 100 fp100`). See [`src/talos/units.py`](../src/talos/units.py) for the canonical constants + Decimal-boundary parsers.

Abstract formulas in this document (e.g. `100 - price_a - price_b`, `avg_price_A + avg_price_B < 100`) describe the underlying arithmetic as a concept. In code those formulas operate on bps values — e.g. the scanner's admission check is actually `raw_edge_bps > 0` where `raw_edge_bps = ONE_DOLLAR_BPS - pa_bps - pb_bps`. The `100` in prose is shorthand for the dollar boundary (`$1`), not a literal cents arithmetic constant.

Display layers render bps back to dollars/cents via formatters (`format_bps_as_dollars_display`, `format_bps_as_cents`, `format_fp100_as_contracts`); operator config fields like `edge_threshold_cents` stay in cents at the config boundary and convert via `cents_to_bps` at first consumption. A permanent AST discipline test ([`tests/test_unit_discipline.py`](../tests/test_unit_discipline.py)) prevents regression to literal-`100` arithmetic on money identifiers.

### Event Lifecycle (per-event, independent)
Each event maintains an independent position ledger. Events are completely isolated — no cross-event logic.

```
Empty → Bidding → Partial → Filled → Ready (for next pair)
```

- **Empty:** No orders, no position on this event
- **Bidding:** One unit resting on each side, nothing filled yet
- **Partial:** Some fills on one or both sides, resting orders still out
- **Filled:** Both sides have a complete unit filled. Arb locked in. May deploy next pair
- **Ready:** State after reset — equivalent to Empty but with P&L history

Transition rule: **Bidding/Partial → Filled requires exactly 1 full unit filled on EACH side.** 9/10 is not complete.

### Position
Measured as `avg_price_in_cents × contract_count` per side. A position is "safe" (arb locked in) when both sides have equal contract counts and `avg_price_A + avg_price_B < 100` (fee-adjusted). The danger state is unequal counts — one side filled without the other.

### PositionLedger (single source of truth)
Single source of truth for both UI display and bid adjustment safety gates. `compute_display_positions()` reads from ledger state to produce `EventPositionSummary` objects for the UI. The old `compute_event_positions()` (which derived from raw orders) has been deleted.

## Layers

1. **API Client** (Layer 1) — **COMPLETE**
   Auth, REST, WebSocket, Pydantic models, error hierarchy.
   - REST: order groups CRUD, decrease_order, fee schedule, batch orders
   - WS: bulk subscribe (`market_tickers` list), `update_subscription`, `list_subscriptions`
   - Models: Market enrichment (`settlement_ts`, `close_time`, `result`, `market_type`), Series enrichment (`fee_type`, `fee_multiplier`, `frequency`, `settlement_sources`)
   - `LifecycleFeed`: WS handler for `market_lifecycle_v2` channel — determination, settlement, pause/unpause events with typed callbacks
2. **Market Data** (Layer 2) — **COMPLETE**
   Pure `OrderBookManager` + async `MarketFeed` orchestrator. Stale book auto-recovery: `_recover_stale_books()` runs at top of each `refresh_account` cycle, unsubscribes/resubscribes stale tickers to get a fresh snapshot.
3. **Strategy Engine** (Layer 3) — **COMPLETE**
   Pure `ArbitrageScanner` + async `GameManager` orchestrator. Scanner computes both raw and fee-adjusted edges via `fees.py`.
4. **Execution** (Layer 4) — **COMPLETE**
   `TopOfMarketTracker`: detects penny jumps on resting NO bids in real-time via WS deltas. TUI shows toast alerts and `!!` prefix in Q columns.
   `PositionLedger`: per-event single source of truth for filled counts, resting orders, avg prices, and safety gates. Pure state machine (no I/O). Also hosts `compute_display_positions()` for UI display.
   `BidAdjuster`: mixed pure/async — pure decision logic (`evaluate_jump`) that queries ledger, checks profitability gate (P18), enforces most-behind-first tiebreaker (P19), and proposes amend adjustments; async execution (`execute`) that calls `rest_client.amend_order()` for atomic price changes (P17).
   `rebalance.py`: pure detection (`compute_rebalance_proposal`) + async execution (`execute_rebalance`) for position imbalance correction. Follows the pure/async split — detection is mock-free testable, execution handles the two-step reduce-then-catchup with fresh Kalshi sync.
   `TradingEngine`: central orchestrator owning all subsystem references, mutable caches (queue, orders, CPM), and polling/action methods. Communicates with the UI via `on_notification` callback. Proposals flow through `ProposalQueue` for operator approval. Extracted from `TalosApp` to enable headless testing and future API-driven control.
   Bid modal uses `all_snapshots` fallback so any monitored pair is always selectable.
5. **UI (Textual TUI)** (Layer 5) — **COMPLETE**
   Thin UI shell. `OpportunitiesTable` (prices + positions + queue), `AccountPanel` (balance display), `OrderLog` (filled/total + queue position), `ProposalPanel` (collapsible right sidebar for pending proposals with keyboard approve/reject). `AddGamesScreen` + `BidScreen` modals. `TalosApp` delegates all polling and actions to `TradingEngine`; owns only widget wiring and Textual lifecycle.
6. **Automation** (Layer 6) — **AUTOMATIC**
   `ProposalQueue`: pure state machine holding pending proposals (adjustments + bids). Single choke point for proposal approval.
   `OpportunityProposer`: pure decision logic that evaluates scanner output against edge threshold + stability filter + position gate. Emits bid proposals into ProposalQueue.
   `AutomationConfig`: settings dataclass (edge threshold, stability seconds, cooldown, enabled flag). `DEFAULT_UNIT_SIZE` constant is the single authority for unit_size defaults.
   `ExecutionMode`: two modes — Automatic (intended default, proposals auto-approve) and Manual (override/debug, operator presses Y/N). Optional auto-stop timer on automatic. Safety flows (rebalance, catch-up, overcommit) execute in both modes. Status bar shows `MODE: AUTO|MANUAL` + `DATA: LIVE|STALE` as orthogonal always-visible dimensions.
   Startup reads `execution_mode` and `auto_stop_hours` from `settings.json` as boot policy (never rewritten at runtime).

7. **Event Scanner** (Layer 7) — **ACTIVE**
   `GameManager.scan_events()` discovers open arb-eligible events from `SPORTS_SERIES` and `NON_SPORTS_SERIES` (toggled by `sports_enabled`). Fetches concurrently with semaphore(10). `ScanScreen` modal shows results with Sport/League/Date/Volume columns. `MarketPickerScreen` for non-sports events with 2+ active markets. Press `c` to scan, Space to toggle, Enter to add selected, `a` to add all.
8. **Game Status** (Layer 8) — **ACTIVE**
   `GameStatusResolver`: multi-source live game status via ESPN (major leagues), The Odds API (AHL, minor leagues), PandaScore (esports). Maps Kalshi series tickers (e.g., `KXNHLGAME`) to external APIs, matches games by team codes extracted from `Event.sub_title`. Cached per event_ticker, refreshed hourly. Replaces "Closes" column with Date + Game Status columns (Pacific Time). Also provides per-leg 24h volume (`Market.volume_24h`). Tennis coverage incomplete (no free API for individual challenger matches).
   **Expiration fallback (Plan 05):** Unmapped leagues derive estimated start from `Market.expected_expiration_time` minus sport offset (3h default, 5h UFC/Boxing). Shows `~` prefix on estimated times. Midnight UTC placeholder filtered.

See [[codebase/index]] for the full module map and gotchas.

## Drip — Sibling Program

Standalone Textual TUI at `src/drip/` that manages a single arb event by drip-feeding 1-contract bids with fill-reactive balance control. Prevents imbalance instead of reacting to it (Talos's approach).

- **Pure state machine** (`DripController`) + async orchestrator (`DripApp`) — same pattern as Talos
- **REST polling v1** — no WS; polls every 10s for fills/orderbook, 30s full reconciliation
- **Imports from Talos** — `talos.auth`, `talos.rest_client`, `talos.fees`, `talos.config`
- **Run:** `python -m drip EVENT TICKER_A TICKER_B PRICE_A PRICE_B [--demo]`
- **Design spec:** `docs/specs/2026-03-19-drip-staggered-arb-design.md`

## API Reference

- REST: `https://api.elections.kalshi.com/trade-api/v2` (prod) / `https://demo-api.kalshi.co/trade-api/v2` (demo)
- WS: `wss://api.elections.kalshi.com/trade-api/ws/v2` (prod) / `wss://demo-api.kalshi.co/trade-api/ws/v2` (demo)
- Auth: RSA-PSS SHA-256 signing of `timestamp_ms + method + path`
- Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`
