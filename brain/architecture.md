# Architecture

Talos is a Kalshi arbitrage trading system designed for progressive automation.

## Design Philosophy

**Manual-first:** The system starts with full manual control over trade decisions. Automation is added incrementally as confidence grows. The human is always able to override or intervene.

## Domain Concepts

### Unit
The atomic bidding quantity. Currently **10 contracts**. Configurable, but always a fixed integer. All order placement, position tracking, and safety checks are denominated in units. A "pair" is one unit on side A and one unit on side B of the same event.

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
2. **Market Data** (Layer 2) — **COMPLETE**
   Pure `OrderBookManager` + async `MarketFeed` orchestrator.
3. **Strategy Engine** (Layer 3) — **COMPLETE**
   Pure `ArbitrageScanner` + async `GameManager` orchestrator. Scanner computes both raw and fee-adjusted edges via `fees.py`.
4. **Execution** (Layer 4) — **COMPLETE**
   `TopOfMarketTracker`: detects penny jumps on resting NO bids in real-time via WS deltas. TUI shows toast alerts and `!!` prefix in Q columns.
   `PositionLedger`: per-event single source of truth for filled counts, resting orders, avg prices, and safety gates. Pure state machine (no I/O). Also hosts `compute_display_positions()` for UI display.
   `BidAdjuster`: async orchestrator that responds to jumps — queries ledger, checks profitability gate (P18), enforces most-behind-first tiebreaker (P19), proposes amend adjustments. Semi-auto (propose → human approves) graduating to full-auto. Uses `rest_client.amend_order()` for atomic price changes (P17).
   `TradingEngine`: central orchestrator owning all subsystem references, mutable caches (queue, orders, CPM), and polling/action methods. Communicates with the UI via callbacks (`on_notification`, `on_proposal`). Extracted from `TalosApp` to enable headless testing and future API-driven control.
   Bid modal uses `all_snapshots` fallback so any monitored pair is always selectable.
5. **UI (Textual TUI)** (Layer 5) — **COMPLETE**
   Thin UI shell (~196 lines). `OpportunitiesTable` (prices + positions + queue), `AccountPanel` (balance display), `OrderLog` (filled/total + queue position). `AddGamesScreen` + `BidScreen` modals. `TalosApp` delegates all polling and actions to `TradingEngine`; owns only widget wiring and Textual lifecycle.
6. **Automation** — progressively takes over decision-making from the human. Current stage: semi-auto bid adjustment (Layer 4). See [[principles#2. Human in the Loop]] for the graduation path: manual → assisted → supervised → autonomous.

See [[codebase/index]] for the full module map and gotchas.

## API Reference

- REST: `https://api.elections.kalshi.com/trade-api/v2` (prod) / `https://demo-api.kalshi.co/trade-api/v2` (demo)
- WS: `wss://api.elections.kalshi.com/trade-api/ws/v2` (prod) / `wss://demo-api.kalshi.co/trade-api/ws/v2` (demo)
- Auth: RSA-PSS SHA-256 signing of `timestamp_ms + method + path`
- Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`
