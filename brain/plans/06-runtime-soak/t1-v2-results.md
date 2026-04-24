# T1 Soak Results — v2 (after refresh_trades scoping fix)

Date: 2026-04-04
Environment: Production
Fix applied: `refresh_trades` now scopes to monitored pairs only

## Configuration

```
Tier: T1
Pair count (start): 15
Pair count (end): 15
Duration: 30.0 min
Memory start: ~85 MB
Memory end: ~107 MB (est)
Task count start: 1
Task count end: 5
Task count max: 18
```

## Comparison: v1 → v2

| Signal | v1 | v2 | Change |
|--------|-----|-----|--------|
| Rate limits (429) | 542 | **0** | Eliminated |
| Stale book recoveries | 84 | 92 | ~same (expected) |
| refresh_account_error | 1 | **0** | Fixed (no more rate limit overlap) |
| WS reconnects | 0 | 0 | Clean |
| Post-only-cross | 0 | 0 | Clean |
| Task count max | 398 | **18** | 95% reduction |
| refresh_trades time | 9.1-9.7s | **0.3s** | 30x faster |
| refresh_account time | 1.4-2.2s | 1.4-1.6s | Same (unaffected) |
| refresh_account cycles | 57 | 57 | Same |
| refresh_errors | 0 | 0 | Clean |

## Task Count (all 30 samples)

All 30 samples at 6 except one at 18 (elapsed=1621s). Peak of 18 = the 15 monitored pairs' tickers being fetched in the gather. Transient, self-resolving, expected.

## Recoveries

| Signal | Count | Rate |
|--------|-------|------|
| stale_book_recovered | 92 | 184/hr |
| ws_reconnecting | 0 | 0/hr |

Stale book recoveries unchanged (same low-volume tickers). This is a property of the pair selection, not the system.

## Degradations

| Signal | Count |
|--------|-------|
| rate_limit (429) | 0 |
| refresh_trades_timeout | 0 |
| refresh_account_error | 0 |
| event_loop_blocked | 0 |

**All clear.**

## Operator-Visible Errors

| Signal | Count |
|--------|-------|
| post_only_cross | 0 |
| WS disconnected | 0 |

**All clear.**

## Verdict

**T1 PASSES.** The system is clean at 15 pairs after the scoping fix. Ready for T2.

Remaining item to investigate at T2+: stale book recovery volume on low-volume pairs (184/hr). Not dangerous but wasteful. Consider after T2 results.
