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
Replaces `compute_event_positions()`. Feeds both the UI display (opportunities table, P&L) AND the safety gates for bid adjustment. One system, not two — if the UI shows it, the safety logic agrees with it.

## Layers

1. **API Client** (Layer 1) — **COMPLETE**
   Auth, REST, WebSocket, Pydantic models, error hierarchy.
2. **Market Data** (Layer 2) — **COMPLETE**
   Pure `OrderBookManager` + async `MarketFeed` orchestrator.
3. **Strategy Engine** (Layer 3) — **COMPLETE**
   Pure `ArbitrageScanner` + async `GameManager` orchestrator. Scanner computes both raw and fee-adjusted edges via `fees.py`.
4. **Execution** (Layer 4) — **IN PROGRESS**
   `TopOfMarketTracker`: detects penny jumps on resting NO bids in real-time via WS deltas. TUI shows toast alerts and `!!` prefix in Q columns. Foundation for bid adjustment.
   `PositionLedger`: per-event single source of truth for filled counts, resting orders, avg prices, and safety gates. Replaces `compute_event_positions()` for both UI and safety. See [[#PositionLedger (single source of truth)]].
   `BidAdjuster`: async orchestrator that responds to jumps — queries ledger, checks profitability gate, proposes cancel-then-place adjustments. Semi-auto (propose → human approves) graduating to full-auto.
   Bid modal uses `all_snapshots` fallback so any monitored pair is always selectable.
5. **UI (Textual TUI)** (Layer 5) — **COMPLETE**
   `OpportunitiesTable` (prices + positions + queue), `AccountPanel` (balance display), `OrderLog` (filled/total + queue position). `AddGamesScreen` + `BidScreen` modals. `TalosApp` orchestrates polling: `refresh_account` (10s, orders + balance) and `refresh_queue_positions` (3s, fast queue enrichment with conservative merge).
6. **Automation** — progressively takes over decision-making from the human. Current stage: semi-auto bid adjustment (Layer 4). See [[principles#2. Human in the Loop]] for the graduation path: manual → assisted → supervised → autonomous.

See [[codebase/index]] for the full module map and gotchas.

## API Reference

- REST: `https://api.elections.kalshi.com/trade-api/v2` (prod) / `https://demo-api.kalshi.co/trade-api/v2` (demo)
- WS: `wss://api.elections.kalshi.com/trade-api/ws/v2` (prod) / `wss://demo-api.kalshi.co/trade-api/ws/v2` (demo)
- Auth: RSA-PSS SHA-256 signing of `timestamp_ms + method + path`
- Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`
