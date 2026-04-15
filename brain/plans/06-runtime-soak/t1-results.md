# T1 Soak Results

Date: 2026-04-04
Environment: Production
Execution mode: Headless (no TUI, no bid placement)

## Configuration

```
Tier: T1
Pair count (start): 15 (capped from 590 saved)
Pair count (end): 15
Duration: 30.0 min
Memory start: ~104 MB
Memory end: ~107 MB
Task count start: 1 (before loops)
Task count end: 5 (after shutdown cancel)
```

## Recoveries

| Signal | Count | Rate |
|--------|-------|------|
| stale_book_recovered | 84 | 168/hr |
| ws_reconnecting | 0 | 0/hr |
| adjustment_stale_dismissed | 0 | N/A (no bids) |

**Verdict: FAIL** — 84 stale book recoveries in 30 min (168/hr) far exceeds the 6+/hr threshold. Root cause: the 15 pairs include YES/NO same-ticker pairs (TRUTHSOCIAL, TRUMPSAY, TRUMPACT) where the underlying markets have very low orderbook activity. With a 120s staleness threshold, any ticker with 2+ minutes between orderbook updates triggers recovery. These are not dangerous (recovery works), but the volume is noisy.

## Degradations

| Signal | Count | Rate |
|--------|-------|------|
| refresh_trades_timeout | 0 | 0/hr |
| queue_positions_fetch_failed | 0 | 0/hr |
| **rate_limit (429)** | **542** | **1084/hr** |
| adjustment_pool_timeout | 0 | N/A |
| post_action_verify_pool_timeout | 0 | N/A |
| event_loop_blocked | 0 | 0/hr |

**Verdict: FAIL** — 542 rate limit hits in 30 min. Breakdown:
- 540 from `/markets/trades` (refresh_trades)
- 2 from `/portfolio/orders` (refresh_account, caused by trades/orders overlap)

Root cause: `_active_market_tickers()` returns ALL tickers with resting orders, not just the 15 monitored pairs. The user has ~200 tickers with orders on the exchange. Each refresh_trades cycle fires ~200 trade fetches (sem=5, 30s timeout). At ~7 rate limit errors per cycle × ~50 cycles = ~350 expected. The extra come from the overlap between refresh_trades and refresh_account sharing the API rate budget.

## Operator-Visible Errors

| Signal | Count |
|--------|-------|
| post_only_cross | 0 |
| refresh_account_error | 1 (429 overlap) |
| WS disconnected | 0 |
| UI freeze | N/A (headless) |

**Verdict: PASS** — 1 transient refresh_account_error caused by trades/orders rate limit overlap. No operator-misleading behavior.

## Task Count

| Elapsed | Tasks | Note |
|---------|-------|------|
| 1s | 1 | Before loops start |
| 61-541s | 6 | Baseline stable |
| 601s | **398** | Spike during refresh_trades gather |
| 661s | 6 | |
| 721s | **283** | |
| 841s | **172** | |
| 961s | **69** | |
| 1021-1441s | 6 | Stable |
| 1501s | **281** | |
| 1621s | **121** | |
| 1681-1801s | 5-6 | Clean shutdown |

**Verdict: INVESTIGATE** — Task count spikes to 398 are caused by `asyncio.gather(*[_fetch(t) for t in tickers])` in `refresh_trades`. With ~200 tickers, this creates ~200 coroutines per cycle. The spikes are transient (resolve in <60s) and the tasks are properly cleaned up. But this is the exact pattern that precedes freezes at higher loads.

## Timing

| Operation | Time | Note |
|-----------|------|------|
| refresh_account | 1.4-2.2s | 57 cycles, all OK except 1 rate limit |
| refresh_trades | 9.1-9.7s | 36 cycles, consistent |
| WS delta throughput | ~76 lines/sec | Smooth at 15 pairs |

## Memory

~3 MB growth over 30 min. No leak.

## Key Findings (ranked by severity)

### 1. refresh_trades fetches ALL tickers, not just monitored ones

`_active_market_tickers()` reads from `_orders_cache` which contains all resting orders across the entire exchange account. At 590 saved pairs, this means ~200 tickers with orders. Each refresh_trades cycle fires ~200 API calls, overwhelming the rate limit.

**Impact:** 542 rate limits / 30 min. This is not a tuning problem — it's a scoping bug. refresh_trades should only fetch trades for monitored pairs.

### 2. Transient task spikes from refresh_trades gather

`asyncio.gather(*[_fetch(t) for t in tickers])` creates one coroutine per ticker. With ~200 tickers, the gather creates ~200 tasks. These are throttled by sem=5 but all exist as pending asyncio tasks simultaneously.

**Impact:** Task count spikes to 398. Transient and self-resolving, but at 590 pairs this becomes ~600 simultaneous tasks per refresh cycle.

### 3. Stale book recovery volume on low-activity pairs

YES/NO same-ticker pairs with low volume (TRUTHSOCIAL, TRUMPACT) trigger stale book recovery every ~2 minutes (120s threshold). The recovery works but generates 84 unsub/resub cycles in 30 min.

**Impact:** Adds WS overhead. Not dangerous but wastes API/WS capacity. Could consider a longer staleness threshold for YES/NO pairs, or skip stale recovery for pairs where the spread hasn't changed.

### 4. Windows encoding issue with structlog tracebacks

`logger.exception()` uses Unicode arrows (→ U+2192) in rich traceback formatting. On Windows with stdout redirected to a file, cp1252 encoding can't handle these. The logger call itself crashes with UnicodeEncodeError, masking the actual error.

**Impact:** Silent error masking in production. The TUI avoids this because Textual handles its own output, but any log redirection would hit it.

## Next Steps

Do NOT proceed to T2. Fix finding #1 first (refresh_trades scoping), then re-run T1. The current rate limit volume makes higher pair counts meaningless — the system would just hit rate limits harder.

Specifically:
1. **Fix `_active_market_tickers`** to scope to monitored pairs only (or add a `monitored_only` flag)
2. **Re-run T1** with the fix — expect rate limits to drop to near zero
3. Then consider whether stale book volume warrants a threshold change
