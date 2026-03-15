# Data Collection Pipeline — Design Spec

**Date:** 2026-03-15
**Status:** Draft

## Problem

Talos has no systematic data collection. Order history, market conditions at entry time, fill rates, and P&L outcomes are either lost or scattered across ad-hoc logs. This blocks future ML work (market selection, fill prediction, dynamic unit sizing) and makes it impossible to analyze trading performance.

## Solution

A write-only SQLite database (`talos_data.db`) that captures every observable event in Talos. A single `DataCollector` class receives events via method calls (callback pattern) and appends rows to typed tables. No reads at runtime — analysis happens offline.

## Design Principle

**If we have the information, save it.** Every data point is clearly labeled so future analysis knows exactly what each value represents. Better to have data we don't need than to wish we had data we didn't record.

## Storage

- **Format:** SQLite (single file, zero infrastructure, queryable, exportable to CSV/Parquet/Postgres)
- **Location:** `talos_data.db` in project root
- **Size:** ~50MB/day at 146 games (dominated by market_snapshots at 10s interval), ~1.5GB/month
- **Retention:** No automatic cleanup. Archive to Parquet when needed.
- **Concurrency:** `PRAGMA journal_mode=WAL` for safe concurrent reads during analysis

## Common Columns

Every table has:

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PRIMARY KEY | Auto-increment row ID |
| `ts` | TEXT | ISO-8601 timestamp in UTC when the row was recorded |

## Tables

### `scan_results` — one row per scan invocation

Logged when: user presses `c` and scan completes

| Column | Type | Description |
|--------|------|-------------|
| `events_found` | INTEGER | Total events returned by scan before filtering |
| `events_eligible` | INTEGER | Events passing 2-market + not-monitored filters |
| `events_selected` | INTEGER | Events the user chose to add (0 if cancelled) |
| `series_scanned` | INTEGER | Number of series queried |
| `duration_ms` | INTEGER | How long the scan API calls took |

### `scan_events` — one row per event discovered in a scan

Logged when: scan completes, one row per discovered event

| Column | Type | Description |
|--------|------|-------------|
| `scan_id` | INTEGER | FK to scan_results.id |
| `event_ticker` | TEXT | Kalshi event ticker |
| `series_ticker` | TEXT | Kalshi series ticker (e.g., KXNHLGAME) |
| `sport` | TEXT | Sport abbreviation (HOC, BKB, ESP, TEN) |
| `league` | TEXT | League abbreviation (NHL, AHL, CS2, ATP) |
| `title` | TEXT | Event title from Kalshi |
| `sub_title` | TEXT | Event sub_title from Kalshi |
| `volume_a` | INTEGER | 24h volume on market A |
| `volume_b` | INTEGER | 24h volume on market B |
| `no_bid_a` | INTEGER | Best NO bid on market A in cents |
| `no_ask_a` | INTEGER | Best NO ask on market A in cents |
| `no_bid_b` | INTEGER | Best NO bid on market B in cents |
| `no_ask_b` | INTEGER | Best NO ask on market B in cents |
| `edge` | REAL | Fee-adjusted edge in cents at scan time |
| `selected` | INTEGER | 1 if user chose to add this event, 0 if not |

### `game_adds` — one row each time a game is added to monitoring

Logged when: game is added via scan, manual entry, or startup restore

| Column | Type | Description |
|--------|------|-------------|
| `event_ticker` | TEXT | Kalshi event ticker |
| `series_ticker` | TEXT | Series ticker |
| `sport` | TEXT | Sport abbreviation |
| `league` | TEXT | League abbreviation |
| `source` | TEXT | How it was added: "scan", "manual", "startup" |
| `ticker_a` | TEXT | Market ticker for leg A |
| `ticker_b` | TEXT | Market ticker for leg B |
| `volume_a` | INTEGER | 24h volume on market A at add time |
| `volume_b` | INTEGER | 24h volume on market B at add time |
| `fee_type` | TEXT | Fee model (e.g., quadratic_with_maker_fees) |
| `fee_rate` | REAL | Fee multiplier |
| `scheduled_start` | TEXT | Game start time from game status resolver (ISO-8601 UTC, NULL if unknown) |

### `orders` — one row per order state change

Logged when: order placed, filled (from WS _on_order_update), amended, or cancelled

| Column | Type | Description |
|--------|------|-------------|
| `event_ticker` | TEXT | Parent event ticker |
| `order_id` | TEXT | Kalshi order ID |
| `ticker` | TEXT | Market ticker this order is on |
| `side` | TEXT | "yes" or "no" |
| `action` | TEXT | "buy" or "sell" |
| `status` | TEXT | "resting", "executed", "canceled" |
| `price` | INTEGER | NO price in cents |
| `initial_count` | INTEGER | Original order quantity |
| `fill_count` | INTEGER | Contracts filled so far |
| `remaining_count` | INTEGER | Contracts still resting |
| `maker_fill_cost` | INTEGER | Maker fill cost in cents |
| `maker_fees` | INTEGER | Maker fees in cents |
| `source` | TEXT | What triggered this: "auto_accept", "manual", "amend", "cancel" |

### `fills` — one row per individual fill from WS

Logged when: `_on_fill` WS callback fires

| Column | Type | Description |
|--------|------|-------------|
| `event_ticker` | TEXT | Parent event ticker |
| `trade_id` | TEXT | Kalshi trade ID |
| `order_id` | TEXT | Parent order ID |
| `ticker` | TEXT | Market ticker |
| `side` | TEXT | "yes" or "no" |
| `price` | INTEGER | Fill price (YES side) in cents |
| `count` | INTEGER | Contracts filled in this trade |
| `fee_cost` | INTEGER | Fee for this fill in cents |
| `is_taker` | INTEGER | 1 if we were the taker, 0 if maker |
| `post_position` | INTEGER | Our position after this fill |
| `queue_position` | INTEGER | Queue position at time of fill (from cache, NULL if unknown) |
| `time_since_order` | REAL | Seconds between order placement and this fill |

### `market_snapshots` — periodic snapshot of every monitored market

Logged when: every 10 seconds via dedicated timer

| Column | Type | Description |
|--------|------|-------------|
| `event_ticker` | TEXT | Parent event ticker |
| `ticker_a` | TEXT | Market ticker for leg A |
| `ticker_b` | TEXT | Market ticker for leg B |
| `no_a` | INTEGER | Best NO ask on A in cents (what scanner shows) |
| `no_b` | INTEGER | Best NO ask on B in cents |
| `edge` | REAL | Fee-adjusted edge in cents |
| `volume_a` | INTEGER | All-time volume on A (from ticker WS) |
| `volume_b` | INTEGER | All-time volume on B |
| `open_interest_a` | INTEGER | Open interest on A |
| `open_interest_b` | INTEGER | Open interest on B |
| `game_state` | TEXT | "pre", "live", "post", "unknown" |
| `status` | TEXT | Talos status string (e.g., "Bidding", "Settled", "Ready") |
| `filled_a` | INTEGER | Filled contracts on A |
| `filled_b` | INTEGER | Filled contracts on B |
| `resting_a` | INTEGER | Resting contracts on A |
| `resting_b` | INTEGER | Resting contracts on B |

### `settlements` — one row when a market is determined or settled

Logged when: lifecycle feed fires `on_determined` or `on_settled`

| Column | Type | Description |
|--------|------|-------------|
| `event_ticker` | TEXT | Parent event ticker |
| `ticker` | TEXT | Market ticker |
| `event_type` | TEXT | "determined" or "settled" |
| `result` | TEXT | "yes" or "no" |
| `settlement_value` | INTEGER | Settlement price in cents |
| `total_pnl` | INTEGER | P&L for this event in cents (NULL if not calculable) |

## Architecture

### DataCollector class

```python
class DataCollector:
    def __init__(self, db_path: Path) -> None: ...
    def log_scan(self, events_found, events_eligible, events_selected, series_scanned, duration_ms, events) -> None: ...
    def log_game_add(self, pair, source, event=None, game_status=None) -> None: ...
    def log_order(self, event_ticker, order, source) -> None: ...
    def log_fill(self, event_ticker, fill_msg, queue_position=None, order_placed_at=None) -> None: ...
    def log_market_snapshots(self, scanner, positions, statuses, tracker, resolver) -> None: ...
    def log_settlement(self, event_ticker, ticker, event_type, result, settlement_value, total_pnl=None) -> None: ...
    def close(self) -> None: ...
```

- Creates all tables on init (`CREATE TABLE IF NOT EXISTS`)
- Each method does a single synchronous `INSERT` (~0.1ms)
- Write-only at runtime — no reads, no queries
- `PRAGMA journal_mode=WAL` for concurrent read safety

### Wiring

`DataCollector` instantiated in `__main__.py`, passed to engine and app:

- **Engine:**
  - `place_bids()` → `collector.log_order()`
  - `_on_order_update()` → `collector.log_order()`
  - `_on_fill()` → `collector.log_fill()`
  - `_on_market_determined()` → `collector.log_settlement()`
  - `_on_market_settled()` → `collector.log_settlement()`

- **App:**
  - `_run_scan()` → `collector.log_scan()`
  - `add_games()` → `collector.log_game_add()`
  - New 10s timer → `collector.log_market_snapshots()`

## Testing

- Unit test: create DataCollector with temp DB, call each log method, verify rows inserted
- Schema test: verify all tables exist after init
- No integration tests needed — collector is pure I/O, no business logic

## Out of Scope

- Automatic cleanup / retention policy
- Compression / archival
- Read-side queries or dashboards
- ML model training (separate project)
