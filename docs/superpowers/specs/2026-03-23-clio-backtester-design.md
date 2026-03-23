# Clio Sequential Placement Backtester — Design Spec

**Goal:** A configurable backtester that replays 182M historical Kalshi trades to simulate different order placement strategies, comparing net PnL against the current simultaneous-placement baseline. Accessed via a new dashboard view with interactive parameter controls.

**Data source:** `kalshi_history.db` — 182M trades, 38K+ settled binary events, Jan 2025 – Mar 2026.

---

## Architecture

Three layers in `clio/backtest.py`:

### 1. Price Path Builder

For each event, queries trades from `kalshi_history.db` and builds a time-indexed DataFrame per market:

```
columns: timestamp, ticker, no_price_cents, count, taker_side
```

**Caching:** Price paths are built per-series and saved as parquet in `clio/output/paths/` to avoid re-querying 182M rows on each run. Cache is invalidated on pipeline refresh.

**Estimated start time:** Uses `expected_expiration_time` minus sport offset (same logic as `clio/prices.py`) to anchor the time axis relative to game start.

### 2. Strategy Engine

Each strategy is a dataclass with slider-controllable parameters. A strategy receives a price path + config and returns a simulation result.

**Fill simulation model (taker-side filtered):**
1. Scan forward through trades on the target ticker from placement time T
2. Only count trades where `taker_side = "yes"` (someone sold into the bid side — equivalent of our NO bid getting filled)
3. Only trades where `no_price_cents <= P` (at or better than our bid price)
4. Accumulate `count` until unit size reached or timeout expires
5. Return fill result: `filled`, `fill_qty`, `avg_fill_price`, `time_to_fill`

**PnL calculation:**
- Both sides fill: `pnl = 100 - avg_price_a - avg_price_b - fee_a - fee_b` (per contract × fill_qty)
- One side fills: `pnl = -(fill_qty × avg_price / 100)` (naked loss at settlement)
- Neither fills: `pnl = 0`

All results expressed as NET dollars, not cents.

### 3. Runner

Iterates events, applies strategy, collects results into a DataFrame. Supports filtering by sport, league, date range, combined price range. Returns results to the dashboard API.

**Performance target:** A few minutes for full dataset, under 1 minute for single-sport or filtered subsets. Overnight batch runs acceptable for parameter sweeps.

---

## Strategies

### Simultaneous (baseline)
Place both sides at the same time. Both orders use the price path's NO bid at the entry timestamp. No additional parameters.

### Sequential: Thin Side First
Place the side with less trade activity (fewer trades in the pre-game window) first. Wait for fill confirmation. Then place the other side.

Parameters:
- `wait_timeout_seconds: int` (default 300) — max time to wait for first leg fill
- `price_tolerance_cents: int` (default 2) — abort if second leg price moved more than this from entry signal

### Sequential: Spread-Informed
Same as Sequential but picks the first leg based on bid/ask spread proxy. The side with the wider spread (derived from the gap between taker_side=yes and taker_side=no trade prices) is placed first.

Parameters:
- `wait_timeout_seconds: int` (default 300)
- `price_tolerance_cents: int` (default 2)

### Time-Gated Entry
Only enter events within a specified window before estimated game start.

Parameters:
- `max_minutes_before_start: int` (default 120)
- `min_minutes_before_start: int` (default 15)

### Price Threshold Entry
Only enter when combined NO price is within a specified range.

Parameters:
- `min_combined_price: int` (default 70)
- `max_combined_price: int` (default 95)

### Dynamic Unit Sizing
Vary unit size based on event characteristics.

Parameters:
- `base_unit: int` (default 20)
- `high_risk_unit: int` (default 10)
- `high_risk_vol_ratio: float` (default 2.0) — events with volume ratio above this get reduced unit
- `high_risk_max_combined: int` (default 75) — events with combined price below this get reduced unit

### Composability
Strategies are composable — the runner applies them as filters/modifiers in sequence:
1. Entry filters (PriceThreshold, TimeGated) decide whether to enter at all
2. Placement strategy (Simultaneous, Sequential) decides how to place orders
3. Sizing (DynamicUnit) decides how many contracts

A backtest config specifies which layers are active and their parameters.

---

## API Endpoints

### POST /api/backtest
Accepts strategy config JSON. Runs simulation server-side. Returns results.

Request body:
```json
{
  "placement": "sequential_thin",
  "placement_params": { "wait_timeout_seconds": 300, "price_tolerance_cents": 2 },
  "entry_filters": {
    "price_threshold": { "min_combined": 70, "max_combined": 95 },
    "time_gated": { "max_minutes": 120, "min_minutes": 15 }
  },
  "sizing": { "base_unit": 20, "high_risk_unit": 10, "vol_ratio_threshold": 2.0 },
  "filters": { "sport": "Hockey", "league": null, "min_date": null, "max_date": null }
}
```

Response:
```json
{
  "summary": {
    "events_tested": 1255,
    "events_entered": 890,
    "events_both_filled": 620,
    "events_one_filled": 180,
    "events_no_fill": 90,
    "net_pnl": 57064,
    "baseline_pnl": 6869,
    "vs_baseline": 50195,
    "fill_rate": 0.697,
    "trap_rate": 0.202
  },
  "events": [
    {
      "event_ticker": "KXNHLGAME-26MAR02DETNSH",
      "sport": "Hockey",
      "league": "NHL",
      "entry_time": "2026-03-02T18:30:00Z",
      "price_a": 47,
      "price_b": 44,
      "combined": 91,
      "filled_a": true,
      "filled_b": true,
      "fill_time_a": 45.2,
      "fill_time_b": 3.1,
      "unit_size": 20,
      "pnl": 874,
      "result": "clean"
    }
  ]
}
```

### GET /api/backtest/status
Returns `{ "running": true/false, "progress": 0.45, "events_processed": 560, "events_total": 1255 }`.

Results cached in memory — re-rendering dashboard doesn't re-run.

---

## Dashboard View: /backtest

### Left Panel — Strategy Builder
- **Placement strategy** radio: Simultaneous / Sequential (Thin First) / Sequential (Spread-Informed)
- **Entry filters** checkboxes with parameter inputs:
  - Price Threshold: min/max combined price sliders
  - Time Gate: min/max minutes before start
- **Sizing** checkbox with inputs:
  - Base unit, high-risk unit, vol ratio threshold, max combined threshold
- **Data filters:** Sport dropdown, league dropdown, date range
- **Run Backtest** button
- Progress bar while running

### Right Panel — Results
- **KPI row:** Net PnL, vs Baseline, Events Tested, Fill Rate, Trap Rate
- **Comparison table:** When multiple runs have been done in the session, shows them side by side with net PnL and vs-baseline for each
- **Per-event results table:** Sortable by PnL, fill time, sport. Click to expand event detail. Color-coded: green for clean fills, red for traps, gray for no-entry.

---

## Files

| File | Purpose |
|------|---------|
| `clio/backtest.py` | Price path builder, fill simulator, strategy engine, runner |
| `clio/strategies.py` | Strategy dataclasses and composability logic |
| `clio/dashboard/src/views/Backtest.tsx` | Dashboard view with strategy builder + results |
| `clio/dashboard/src/api.ts` | New fetch functions for backtest endpoints |
| `clio/dashboard/api.py` | New `/api/backtest` and `/api/backtest/status` endpoints |
| `tests/test_clio/test_backtest.py` | Unit tests for fill simulation, strategy logic, PnL calculation |

---

## Limitations

- **No orderbook depth** — fill simulation uses trade occurrence as proxy. A trade at our price doesn't guarantee we'd have been filled (queue position unknown).
- **Taker-side filtering is conservative** — may understate fill rates for liquid markets where resting bids get filled frequently.
- **Price paths from trades have gaps** — illiquid markets may have no trades for hours. The simulation treats these as "no fill opportunity."
- **Post-settlement snapshot OI** — volume/OI from kalshi_history.db is a single snapshot, not the state at simulated entry time.
- **No slippage model** — assumes our order doesn't move the market. Reasonable for unit=20 but may overstate results at larger sizes.
