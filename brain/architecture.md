# Architecture

Talos is a Kalshi arbitrage trading system designed for progressive automation.

## Design Philosophy

**Manual-first:** The system starts with full manual control over trade decisions. Automation is added incrementally as confidence grows. The human is always able to override or intervene.

## Layers

1. **API Client** (Layer 1) — **COMPLETE**
   Auth, REST, WebSocket, Pydantic models, error hierarchy.
2. **Market Data** (Layer 2) — **COMPLETE**
   Pure `OrderBookManager` + async `MarketFeed` orchestrator.
3. **Strategy Engine** (Layer 3) — **COMPLETE**
   Pure `ArbitrageScanner` + async `GameManager` orchestrator. Scanner computes both raw and fee-adjusted edges via `fees.py`.
4. **Execution** — places and manages orders (in progress)
   `TopOfMarketTracker`: detects penny jumps on resting NO bids in real-time via WS deltas. TUI shows toast alerts and `!!` prefix in Q columns. Foundation for future order amendment.
5. **UI (Textual TUI)** (Layer 5) — **COMPLETE**
   `OpportunitiesTable` (prices + positions + queue), `AccountPanel` (balance display), `OrderLog` (filled/total + queue position). `AddGamesScreen` + `BidScreen` modals. `TalosApp` orchestrates polling: `refresh_account` (10s, orders + balance) and `refresh_queue_positions` (3s, fast queue enrichment with conservative merge).
6. **Automation** — progressively takes over decision-making from the human

See [[codebase/index]] for the full module map and gotchas.

## API Reference

- REST: `https://api.elections.kalshi.com/trade-api/v2` (prod) / `https://demo-api.kalshi.co/trade-api/v2` (demo)
- WS: `wss://api.elections.kalshi.com/trade-api/ws/v2` (prod) / `wss://demo-api.kalshi.co/trade-api/ws/v2` (demo)
- Auth: RSA-PSS SHA-256 signing of `timestamp_ms + method + path`
- Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`
