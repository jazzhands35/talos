# Clio ML Strategy Optimizer — Key Findings

First run: 2026-03-23. Data: 38,557 synthetic events from kalshi_history.db, 234 real Talos outcomes from talos_data.db.

## Model Results

- **Stage 1 (Theoretical Edge):** AUC 1.0 — `combined_price` perfectly predicts arb existence (it's a deterministic function). Useful only as an input feature to Stage 2.
- **Stage 2 (Trap Prediction):** AUC 0.672 — moderately above random (0.5), above 0.60 actionable threshold. Top features: `a_total_volume`, `combined_price`, `oi_ratio`.

## Combined Price Is The #1 Lever

| Combined Price | Trap Rate | PnL |
|---|---|---|
| < 60 | 98% | -$82.28 |
| 60-70 | 100% | -$15.81 |
| 70-80 | 50% | +$70.16 |
| 80-90 | 62% | +$146.42 |
| 90-95 | 56% | +$28.47 |
| 95+ | 42% | -$70.36 |

**Action:** min_combined_price of 70 eliminates the death zone. Sweet spot is 70-90.

## Trap Side Analysis

- 134/234 (57%) of Talos trades were traps (one-sided fills)
- 99% of losses come from trapped trades
- **Which side fails to fill is NOT reliably predicted by volume.** Using lifetime volume as proxy: low-vol side unfilled 42%, high-vol side unfilled 58%. Lifetime volume is a poor proxy for orderbook depth at placement time.
- `market_snapshots` in talos_data.db has OI/volume columns but they're always zero — Talos doesn't write them.
- `kalshi_history.db` has OI per market but it's a single post-settlement snapshot, not a time series.

## Strategy Comparison (Net Impact vs Current $68.69)

| Strategy | Net PnL | vs Current |
|---|---|---|
| Sequential placement (hard leg first) | $570.64 | +$501.95 |
| Targeted unit=10 on high-risk only | $123.51 | +$54.82 |
| Unit=10 on all events | $34.34 | -$34.34 |
| Unit=5 on all events | $17.17 | -$51.52 |

## Data Gaps Identified

1. **Talos `market_snapshots`** has volume_a/b and open_interest_a/b columns but never writes them. The Kalshi API returns this data — Talos just doesn't save it. Easy fix.
2. **No orderbook depth data** anywhere — `yes_bid_size_fp`/`yes_ask_size_fp` available from Kalshi API but not collected.
3. **Can't reconstruct OI from trades** — trades don't distinguish opening vs closing positions.
4. **kalshi_history.db trades** (182M) can reconstruct price paths and volume time series, but not OI or book depth.

## Next: Sequential Placement Backtester

Use 182M trades from kalshi_history.db to simulate:
- Place thin side first, wait for fill signal, then place thick side
- Reconstruct price paths from trade timestamps
- Estimate fill probability from trade frequency and taker_side patterns
- Compare net PnL vs simultaneous placement across 38K events
