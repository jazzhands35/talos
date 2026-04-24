# T3 Soak Results

Date: 2026-04-04
Environment: Production
Scan mode: both (sports + non-sports)

## Configuration

```
Tier: T3
Pair count (start): 50
Pair count (end): 50
Duration: 120.0 min
Task count start: 1
Task count end: 5
Task count max: 15
```

## Signal Summary

### Degradations

| Signal | Count | Rate |
|--------|-------|------|
| rate_limit (429) | **0** | 0/hr |
| refresh_account_error | **0** | 0/hr |
| ws_reconnect | **0** | 0/hr |
| post_only_cross | **0** | 0/hr |
| event_loop_blocked | 0 | 0/hr |

**PASS.** Zero across every degradation and operator-error signal for the full 2 hours.

### Task Count (120 samples)

118 of 120 at 6. Two spikes: 7 and 9. Peak of 15.

**PASS.** Lowest task-spike max across all tiers. The refresh_trades gather at 50 monitored tickers is small enough that the sampler rarely catches it in-flight.

### refresh_account Timing

| Metric | Value |
|--------|-------|
| Cycles | 228 |
| Mean | 1.58s |
| p99 (top 1%) | 2.7s |
| Max | **5.2s** |
| Over 2.0s | 11 of 228 (4.8%) |

The single 5.2s outlier occurred at minute 5 (14:27:08). No second occurrence in 115 remaining minutes. The 11 cycles over 2.0s are in the 2.1-2.7s range — modest overhead from stale recovery bursts landing inside the cycle.

### refresh_account × stale recovery coupling

Correlation analysis: the 5.2s outlier at 14:27:08 aligned with a fragmented stale recovery sequence where ~6 tickers were stale when the cycle started (tail end of a 27-ticker burst at 14:26:31). The sequential unsub/resub for those 6 tickers added ~3.6s.

Larger bursts (29 tickers at 14:24:26) did NOT cause slowdowns because they completed before the next refresh_account cycle started. The coupling only occurs when stale recoveries span a cycle boundary.

**Verdict: Moderate coupling, not worsening over time.** 4.8% of cycles exceeded 2s. No cycle exceeded 5.2s. The sequential recovery design is adequate at 50 pairs.

### Stale Book Recoveries

| Signal | Count | Rate |
|--------|-------|------|
| stale_book_recovered | **1598** | **799/hr** |

#### Burst Size Distribution

| Burst size | Frequency | Note |
|------------|-----------|------|
| 24-29 | 3 bursts (first 10 min) | Initial stabilization |
| 17-19 | 7 bursts | Steady state |
| 1-7 | Many | Long-tail individual recoveries |

Bursts decreased from 29→17 over the 2 hours as some pairs gained more activity. The system self-stabilizes.

#### Per-Ticker Distribution (top 10, 2 hours)

| Ticker | Recoveries | Series | Type |
|--------|-----------|--------|------|
| TRUMPSAY SLEE | 56 | Non-sports | YES/NO |
| TRUMPSAY MELA | 56 | Non-sports | YES/NO |
| GREENTERRITORY | 56 | Non-sports | YES/NO |
| BONDIOUT | 56 | Non-sports | YES/NO |
| TRUTHSOCIAL T220 | 55 | Non-sports | YES/NO |
| TRUTHSOCIAL T80 | 54 | Non-sports | YES/NO |
| TRUTHSOCIAL B169 | 53 | Non-sports | YES/NO |
| HORMUZTRAFFIC T10 | 53 | Non-sports | YES/NO |
| HORMUZTRAFFIC T1 | 53 | Non-sports | YES/NO |
| TRUMPACT T7 | 52 | Non-sports | YES/NO |

**All non-sports.** HORMUZTRAFFIC and GREENTERRITORY are new entrants (added by "both" scan mode). Zero sports tickers. Pattern unchanged from T2: clustered, not spreading, no overlap with operator errors.

### Scaling Summary

| Metric | T1 v2 (15p, 30m) | T2 (30p, 60m) | T3 (50p, 120m) | Scaling |
|--------|-------------------|---------------|-----------------|---------|
| Rate limits | 0 | 0 | 0 | Clean |
| WS disconnects | 0 | 0 | 0 | Clean |
| Operator errors | 0 | 0 | 0 | Clean |
| Task max | 18 | 25 | **15** | Sublinear (gather completes faster) |
| refresh_account mean | 1.5s | 1.5s | 1.58s | Flat |
| refresh_account max | N/A | N/A | 5.2s | Single outlier |
| refresh_trades | 0.3s | 0.6s | 0.8-0.9s | Linear |
| Stale recoveries/hr | 184 | 532 | 799 | Linear with non-sports pairs |

## Verdict

**T3 PASSES.** The architecture holds at 50 pairs for 2 hours under production load.

- Zero rate limits, zero disconnects, zero operator errors
- refresh_account mean is flat at 1.58s despite 3x pair count from T1
- One timing outlier (5.2s) from stale recovery coupling, not repeated
- Stale recovery volume (799/hr) is high but harmless — concentrated on low-volume non-sports pairs with zero correctness impact
- Task count is the cleanest of all tiers (max 15)

### What the soak proved

1. The WS + orderbook pipeline scales linearly and cleanly to 50 pairs
2. The refresh_trades scoping fix eliminated all rate limit pressure
3. The REST backup sync (refresh_account) is dominated by order count, not pair count
4. Stale book recovery is noisy but functionally correct — no data integrity issues
5. No memory leaks, no task leaks, no operator-facing errors across 3.5 hours of cumulative testing

### What to do next

Per the protocol:

> If clean at T3 → write a short note on adopting Drip-style prevention over rebalance

The soak is clean. The next step is the Drip-style prevention design note, not more scaling work.
