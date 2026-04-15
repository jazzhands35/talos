# Runtime Soak Protocol

Observation exercise. No feature work. Run Talos at increasing pair counts, record what happens, decide what to fix next based on evidence.

## Setup

- **Environment:** Demo (default)
- **Execution mode:** Record whether Auto or Manual for each tier
- **Logging:** `python -m talos 2> soak_tN.log`
- **Freeze log:** `%LOCALAPPDATA%\talos\talos_freeze.log` (written by the watchdog)

Record at the start and end of each tier:
- Exact pair count (number of monitored arb pairs)
- Python process memory (Task Manager → Details → python.exe → Memory)
- Asyncio task count (see Sampling below)

## Tiers

| Tier | Target Pairs | Duration | Stress Target |
|------|-------------|----------|---------------|
| T1 | 10-15 (record exact) | 30 min | Baseline. Confirm steady-state is clean. |
| T2 | 25-30 (record exact) | 1 hour | WS delta volume, scanner eval time, REST backup sync. |
| T3 | 50+ (record exact) | 2 hours | REST semaphore contention (sem=20), stale book recovery at scale, refresh_trades 30s timeout, memory footprint. |

Advance to the next tier only if the current tier passes.

## Sampling Task Count

The freeze watchdog (`app.py:377`) already logs `event_loop_blocked` with `task_count` — but only fires on freezes (>2s). To sample task count during normal operation, open a Python console against the running process is impractical. Instead:

**Quick method:** At the start and end of each tier, trigger a brief UI freeze by suspending the process for ~3s (Task Manager → right-click → Suspend Process → Resume). The watchdog will fire and log the task count. Check `talos_freeze.log`.

**Better method (if needed later):** Add a periodic task-count log line to the 10s `_log_market_snapshots` callback. One-line change. Only worth adding if T1 shows anomalies.

## Signals to Record

### Category 1: Internal Recoveries

Self-healing behavior. Expected in small quantities. Concerning only if frequent.

| Signal | Log key | Where |
|--------|---------|-------|
| Stale book recovery | `stale_book_recovered` | `engine.py:3148` — sequential unsub+resub per ticker |
| WS reconnect | `ws_reconnecting` | `engine.py:744` |
| Stale adjustment dismissed | `adjustment_stale_dismissed` | `bid_adjuster.py:436` |

### Category 2: Degraded Behavior

System is losing ground. Operator doesn't see errors yet, but capacity is being exceeded.

| Signal | Log key | Where |
|--------|---------|-------|
| Trade fetch timeout | `refresh_trades_timeout` | `engine.py:1102` — sem=5 with 30s cap |
| Queue position fetch fail | `queue_positions_fetch_failed` | `engine.py:932` |
| Rate limit hit | `KalshiRateLimitError` | REST layer — sem=20 should prevent |
| Adjustment pool timeout | `adjustment_pool_timeout` | `engine.py:2615` |
| Verify pool timeout | `post_action_verify_pool_timeout` | `engine.py:2904` |
| Event loop blocked | `event_loop_blocked` | `app.py:401` — task_count included |

### Category 3: Operator-Visible Errors

Things the operator sees or that affect trading correctness.

| Signal | Log key | Where |
|--------|---------|-------|
| Post-only-cross | `post_only_cross` | `engine.py:1945,2375` — local book diverged from Kalshi |
| Refresh account error | `refresh_account_error` | `engine.py:1054` |
| WS disconnected (toast) | `WEBSOCKET DISCONNECTED` | `engine.py:736` — operator sees stale data warning |
| Failed proposal | grep for proposal + error | proposal placement failures |
| UI freeze | `event_loop_blocked` with elapsed >5s | `app.py:401` + `talos_freeze.log` |

## Post-Soak Analysis

Run from Git Bash (available on Windows via Git installation):

```bash
# Category 1: Internal Recoveries
echo "=== RECOVERIES ==="
grep -c "stale_book_recovered" soak_tN.log
grep -c "ws_reconnecting" soak_tN.log
grep -c "adjustment_stale_dismissed" soak_tN.log

# Category 2: Degraded Behavior
echo "=== DEGRADATIONS ==="
grep -c "refresh_trades_timeout" soak_tN.log
grep -c "queue_positions_fetch_failed" soak_tN.log
grep -c "rate_limit" soak_tN.log
grep -c "adjustment_pool_timeout" soak_tN.log
grep -c "post_action_verify_pool_timeout" soak_tN.log
grep -c "event_loop_blocked" soak_tN.log

# Category 3: Operator-Visible Errors
echo "=== OPERATOR ERRORS ==="
grep -c "post_only_cross" soak_tN.log
grep -c "refresh_account_error" soak_tN.log
grep -c "WEBSOCKET DISCONNECTED" soak_tN.log
```

PowerShell alternative (if not using Git Bash):

```powershell
# Example for one signal:
(Select-String -Path soak_tN.log -Pattern "stale_book_recovered").Count
```

## Pass / Investigate / Fail

| Signal Category | Pass | Investigate | Fail |
|----------------|------|-------------|------|
| **Recoveries** per hour | 0-2 total | 3-5 total | 6+ or continuous |
| **Degradations** per hour | 0 | 1-2 timeouts | 3+ timeouts or any rate limit |
| **Operator errors** per hour | 0 | 1 post-only-cross | 2+ or any refresh_account_error |
| Memory growth (start→end) | < 50 MB | 50-100 MB | > 100 MB |
| Task count (start→end) | Stable (±5) | Growing 10-20 | Growing 20+ (leak) |
| UI input→response | < 1s | 1-2s | > 2s consistently |

A system that self-heals 3 stale books/hour but never misleads the operator is passing. A system that shows 0 recoveries but places against stale state is failing.

## Known Pressure Points

Ranked by likelihood of being the first to break:

1. **`refresh_trades`** (`engine.py:1075`): sem=5, 30s timeout. At 50 tickers = 10 serial batches. Each `get_trades` is a paginated REST call. First thing to choke.
2. **`_recover_stale_books`** (`engine.py:3131`): Sequential unsub+resub per stale ticker. If 10 go stale simultaneously, that's 10 sequential WS roundtrips blocking `refresh_account`.
3. **REST semaphore** (`rest_client.py:33`): Global sem=20 shared by all REST calls. Overlap between `refresh_account` + `refresh_trades` + `_poll_balance` can queue up.
4. **`refresh_opportunities`** 2s interval (`app.py:127`): calls `scanner.scan()` over all pairs. Pure CPU, but 50+ pairs may exceed 2s budget.

## What Comes After

Based on soak results:

| Outcome | Next Step |
|---------|-----------|
| Clean at T3 | Write design note: Drip-style prevention vs. Talos rebalance |
| REST bottleneck | Tune semaphores + intervals (knobs, not architecture) |
| WS bottleneck | Revisit engine decomposition (architectural) |
| Task leak | Profile with asyncio debug mode, fix the specific leak |
| Memory leak | Profile with `tracemalloc`, likely unbounded cache (CPMTracker, order cache) |

## Recording Template

Copy this for each tier run:

```
Tier: T_
Date: ____-__-__
Execution mode: Auto / Manual
Pair count (start): __
Pair count (end): __
Duration: __ min
Memory start: __ MB
Memory end: __ MB
Task count start: __
Task count end: __

Recoveries:
  stale_book_recovered: __
  ws_reconnecting: __
  adjustment_stale_dismissed: __

Degradations:
  refresh_trades_timeout: __
  queue_positions_fetch_failed: __
  rate_limit: __
  adjustment_pool_timeout: __
  post_action_verify_pool_timeout: __
  event_loop_blocked: __ (max elapsed: __s)

Operator Errors:
  post_only_cross: __
  refresh_account_error: __
  ws_disconnected: __

UI feel: responsive / sluggish / frozen
Notes:
```
