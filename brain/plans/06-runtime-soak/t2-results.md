# T2 Soak Results

Date: 2026-04-04
Environment: Production
Scan mode: both (sports + non-sports)

## Configuration

```
Tier: T2
Pair count (start): 30
Pair count (end): 30
Duration: 60.0 min
Memory: ~85 MB start (est from T1 scaling)
Task count start: 1
Task count end: 5
Task count max: 25
```

## Signal Summary

### Degradations

| Signal | Count |
|--------|-------|
| rate_limit (429) | **0** |
| refresh_trades_timeout | 0 |
| refresh_account_error | **0** |
| event_loop_blocked | 0 |

**PASS.** Zero rate limits for the entire hour. The refresh_trades scoping fix holds at 2x T1 pair count.

### Operator-Visible Errors

| Signal | Count |
|--------|-------|
| post_only_cross | **0** |
| WS disconnected | **0** |
| reconcile_fill_mismatch | 342 |

**PASS.** Zero operator-misleading errors. The `reconcile_fill_mismatch` warnings are expected — the soak seeds ledgers from saved state but doesn't run the initial full-history order sync that the TUI does, so ledger fill counts diverge from Kalshi positions. Not a real issue.

### Task Count (60 samples)

58 of 60 samples at 6. Two spikes:
- elapsed=961s: 25 tasks (refresh_trades gather in-flight)
- elapsed=2341s: 21 tasks (same)

**PASS.** Max 25, down from T1 v1's 398. Spikes are proportional to monitored pair count (30 tickers in gather), transient, and self-resolving.

### Recoveries — Stale Books

| Signal | Count | Rate |
|--------|-------|------|
| stale_book_recovered | **532** | **532/hr** |
| ws_reconnecting | 0 | 0/hr |

#### Per-Ticker Distribution (top 10)

| Ticker | Recoveries/hr | Series |
|--------|--------------|--------|
| TRUTHSOCIAL T80 | 28 | Non-sports |
| TRUTHSOCIAL T220 | 28 | Non-sports |
| TRUMPSAY SLEE | 28 | Non-sports |
| TRUMPSAY MOG | 28 | Non-sports |
| TRUMPSAY MELA | 28 | Non-sports |
| TRUTHSOCIAL B169 | 27 | Non-sports |
| TRUTHSOCIAL B149 | 27 | Non-sports |
| TRUMPSAY DISC | 26 | Non-sports |
| TRUMPSAY MARI | 25 | Non-sports |
| TRUMPSAY CRYP | 24 | Non-sports |

28 unique tickers triggered stale recovery. **All are non-sports YES/NO same-ticker pairs.** Zero sports pairs in the list.

#### Characterization

- **Clustered, not spreading:** Same 28 tickers every cycle. No new tickers appearing over time.
- **No overlap with operator errors:** Zero post_only_cross despite 532 recoveries. Recovery works — resubscribe delivers fresh snapshot, trading continues correctly.
- **Rate is ~28 recoveries per 2-min cycle** — the 120s threshold means each low-volume ticker fires once per cycle.
- **Harmless low-volume churn, not WS falling behind.** The WS is healthy (zero disconnects, zero seq gaps). These tickers simply don't have orderbook activity for 2+ minutes at a time.

### Timing

| Operation | Time | Note |
|-----------|------|------|
| refresh_account | 1.4-1.5s | 114 cycles, zero errors |
| refresh_trades | 0.6-0.7s | Linear from T1's 0.3s |

## Verdict

**T2 PASSES.** Clean across all categories. Ready for T3.

| Category | T1 v2 (15 pairs, 30m) | T2 (30 pairs, 60m) | Scaling |
|----------|----------------------|---------------------|---------|
| Rate limits | 0 | 0 | Clean |
| Task max | 18 | 25 | Linear with pairs |
| refresh_trades | 0.3s | 0.6s | Linear |
| refresh_account | 1.5s | 1.5s | Flat (order count dominates) |
| Stale recoveries/hr | 184 | 532 | ~linear with non-sports pairs |
| WS disconnects | 0 | 0 | Clean |
| Operator errors | 0 | 0 | Clean |
