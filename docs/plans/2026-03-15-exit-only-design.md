# Exit-Only Mode — Design Spec

**Date:** 2026-03-15
**Status:** Draft

## Problem

Games approaching start time are dangerous for NO+NO arb — liquidity drops, sharps pick off stale bids, and getting trapped long one side becomes likely. Currently Talos keeps bidding right up to game time with no way to wind down gracefully.

## Solution

Per-event "exit-only" mode that stops new bidding and either balances the position or cancels everything. Auto-triggers before game start, manually toggleable with `e` key.

## Logic

When exit-only activates for an event:

### If imbalanced (filled_a ≠ filled_b):
- Keep resting bids on the **behind** side (to catch up to delta neutral)
- Cancel resting bids on the **ahead** side
- Don't place new bids on the ahead side
- If behind side fills to match → now balanced → cancel everything

### If balanced (filled_a == filled_b, including 0 == 0):
- Cancel ALL resting bids on both sides
- Don't place any new bids
- Event is done — auto-remove after cancellation

## Triggers

### Auto-trigger
- Every refresh cycle, check each active event's game status
- If `game_state == "pre"` and `scheduled_start` is within `exit_only_minutes` of now → activate
- If `game_state == "live"` → activate immediately
- Events with `game_state == "unknown"` → don't auto-trigger
- Default: `exit_only_minutes = 30` (configurable in `AutomationConfig`)

### Manual trigger
- Press `e` on highlighted row to toggle exit-only on/off
- Toggling off re-enables normal bidding

## Status Column Display

| State | Display | Description |
|-------|---------|-------------|
| Exit-only, balanced, bids cancelled | `EXIT` | Done, waiting for auto-remove |
| Exit-only, imbalanced | `EXIT -10 B` | Behind by 10 on side B, still bidding B |
| Exit-only, cancellation in progress | `EXITING` | Just activated, cancelling bids |

## Architecture

### State
`_exit_only_events: set[str]` on `TradingEngine`. No persistence — resets on restart, auto-trigger re-activates based on game time.

### Gate checks
- `OpportunityProposer.evaluate()` — if event is exit-only, don't propose new bids
- `BidAdjuster.evaluate_jump()` — if event is exit-only on the ahead side, don't propose amend
- `_compute_event_status()` — show EXIT status variants

### Enforcement
New `_enforce_exit_only(event_ticker)` method on engine:
1. Get ledger for event
2. If balanced → cancel all resting bids on both sides
3. If imbalanced → cancel resting on ahead side only, leave behind side
4. Uses existing `rest_client.cancel_order()`

### Auto-check
New `_check_exit_only()` method, called in refresh cycle:
1. For each active event NOT already in exit-only:
   - Get game status from resolver
   - If `state == "live"` or (`state == "pre"` and `scheduled_start - now < exit_only_minutes`) → activate
2. For each event IN exit-only:
   - Check if balanced + no resting → auto-remove game

### Auto-remove
When exit-only + balanced + no resting → `remove_game()` to unsubscribe WS and free slots. This also addresses the 250+ game WS overload problem.

### Configuration
Add `exit_only_minutes: float = 30.0` to `AutomationConfig`.

### Wiring
- `_check_exit_only()` called from `_refresh_proposals` (1s timer) or dedicated timer
- `action_toggle_exit_only()` on app for `e` keybinding
- Engine exposes `is_exit_only(event_ticker) -> bool` for proposer/adjuster checks

## Testing

- Unit test: exit-only logic for balanced vs imbalanced positions
- Unit test: auto-trigger timing (30 min before start)
- Unit test: proposer gate (no new bids when exit-only)
- Unit test: status display (EXIT, EXIT -10 B)

## Data Collection

Exit-only events are logged to `event_outcomes` when they auto-remove. The `game_state_at_fill` field captures whether the last fill happened pre or during exit-only mode.
