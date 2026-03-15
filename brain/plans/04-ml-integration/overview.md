# ML Integration — Overview

Future work: apply ML to operational decisions around the NO+NO arb strategy. The arb itself is structural (price_A + price_B < 100) and doesn't need ML. ML improves the decisions around WHICH markets to enter, WHEN, and at WHAT size.

## High-Value Applications (ordered by ROI)

### 1. Market Selection / Entry Scoring
- **Problem:** 150+ events scanned — which are worth entering?
- **Approach:** Classifier predicting P(profitable_fill) from (sport, league, time_to_start, volume_24h, spread, open_interest)
- **Model:** XGBoost/LightGBM — small dataset, tabular features, no deep learning needed
- **Data:** Order logs, fill history, P&L per event (need to start collecting systematically)
- **Integration:** Sort scan results by predicted profitability, auto-filter low-scoring events

### 2. Fill Rate / CPM Prediction
- **Problem:** Will a bid at 45¢ in queue #750 fill before game starts? Should I bid 46¢?
- **Approach:** Predict time-to-fill or P(fill_before_event) from (price, queue_position, volume, time_to_start, sport, spread)
- **Model:** Regression (XGBoost)
- **Data:** Order history + queue position logs
- **Integration:** Inform price selection and show predicted fill time in UI

### 3. Dynamic Unit Sizing
- **Problem:** Fixed 10-contract units. High-volume NHL supports 50, low-volume AHL supports 5
- **Approach:** Predict max safe unit size from (volume_24h, open_interest, spread, time_to_start)
- **Model:** Could start as simple rules (volume thresholds), graduate to ML
- **Data:** Historical fills vs market depth
- **Integration:** Auto-set unit size per event, or suggest in the scanner

### 4. Spread Timing
- **Problem:** Spreads widen/narrow throughout the day. When is the best entry time?
- **Approach:** Time-series model predicting spread width by (sport, hours_before_start, day_of_week)
- **Model:** Medium complexity — needs data collection first
- **Data:** Ticker feed snapshots (received via WS but not stored yet)
- **Integration:** Show "optimal entry window" in game status column

## Not Worth It

- **RL for order placement** — action space too small, lookup table beats RL
- **Price prediction** — we don't care who wins, we exploit the spread
- **LLM-based analysis** — deterministic rules beat LLMs for arb execution
- **Deep learning** — dataset too small, problem too simple. XGBoost > neural nets here

## Tech Stack

- `scikit-learn` or `xgboost` — not the heavy RL frameworks from awesome-ai-in-finance
- Plain tabular ML with manual feature engineering on our own data
- Trainable on a laptop in seconds

## Prerequisite: Data Collection Pipeline

Before any ML work, Talos needs to systematically log:
- Every scan result (event ticker, sport, volume, spread at scan time)
- Every entry decision (which events were added, which skipped)
- Every order: placement time, price, queue position, fill time, fill count
- Every CPM measurement over time
- Every P&L outcome per event (locked profit, fees, settlement)
- Ticker feed snapshots (spread width over time per market)

Currently some of this is logged ad-hoc (suggestions.log, auto_accept JSONL). Needs a structured pipeline writing to a database or structured CSV/Parquet files.

## Research Sources

- awesome-ai-in-finance repo: mostly stock/crypto, not prediction markets. No direct fits.
- `pyfolio` / `empyrical` — useful for P&L analytics and performance measurement
- `skfolio` — portfolio optimization (could help with capital allocation across events)
