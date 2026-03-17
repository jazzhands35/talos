# Auto-Accept Mode Design

**Date:** 2026-03-13
**Status:** Approved

## Summary

Add an "auto-accept" mode to the TUI that automatically approves all pending proposals for a user-specified duration. The TUI remains fully interactive — auto-accept just acts like a fast human pressing Y on every proposal. A JSONL session logger captures full state snapshots on each action for post-session analysis.

## Requirements

- Toggle via keybinding (A) with duration prompt
- Auto-accepts all proposal types: bids, adjustments, rebalances, holds
- All existing safety gates remain in force (no bypasses)
- JSONL log per session with full state snapshots
- Timer expiry reverts to manual mode silently — resting orders stay, TUI stays open
- Manual Y/N keys still work during auto-accept

## Approach

**TUI-level auto-accept loop** — a 1-second interval timer in `app.py` drains the proposal queue by calling the same `_execute_approval()` path the Y keypress uses. Zero changes to the engine, queue, safety gates, or execution pipeline.

### Auto-Accept State

```python
@dataclass
class AutoAcceptState:
    active: bool = False
    started_at: datetime | None = None
    duration: timedelta | None = None
    accepted_count: int = 0
```

### Execution Flow

Each tick (1s):
1. Check `auto_accept.active` and not expired
2. Get `proposal_queue.pending()` (oldest first)
3. Call `_execute_approval(key)` for first pending proposal
4. One proposal per tick — avoids API flooding, gives sync time
5. Increment `accepted_count`

### JSONL Session Logger

New file: `src/talos/auto_accept_log.py`
Output: `auto_accept_sessions/YYYY-MM-DD_HHMMSS.jsonl`

**Events:**
- `session_start` — config snapshot (edge threshold, stability, unit size, duration)
- `auto_accepted` — proposal detail + full state snapshot (positions, balance, resting orders, top-of-market, scanner opportunities, session stats)
- `auto_accept_error` — approval failure with error detail + state
- `session_end` — summary (total accepted, duration, final positions)

### TUI Integration

- **A key**: Toggle on (prompt for hours) / off (early stop)
- **Footer**: Shows `AUTO-ACCEPT 1:23:45 remaining` countdown when active
- **Toast**: Start/stop messages with summary stats
- **Proposal panel**: Continues rendering — proposals appear and auto-disappear
- **S/Y/N/U keys**: All still functional during auto-accept

## Non-Goals

- No headless mode (requires TUI)
- No additional safety limits beyond existing gates (can add after reviewing sessions)
- No auto-exit or order cancellation on timer expiry
