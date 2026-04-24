# Phase 5 — UI display for estimated start times

Back to [[plans/05-expiration-start-time/overview]]

## Goal

Make estimated start times visible in the main table's Date and Game columns. Distinguish estimates from confirmed times so the operator knows the precision level (P22, P20).

## Changes

**`src/talos/ui/widgets.py`** — Modify `_fmt_game_date` and `_fmt_game_status` to handle estimated times:

1. `_fmt_game_date`: No change needed — it already renders `scheduled_start` as `MM/DD`. Estimated times will display the same.

2. `_fmt_game_status`: When `GameStatus.detail` contains `"~est"`, prefix the time display with `~` to indicate it's approximate. E.g., `"~1:30 PM"` instead of `"1:30 PM"`, or `"~in 25m"` instead of `"in 25m"`.

**Tests** — Test `_fmt_game_status` with a `GameStatus(state="pre", scheduled_start=..., detail="~est")` and verify the `~` prefix appears.

## Data Structures

No new data structures — uses existing `GameStatus.detail` field as a signal.

## Verification

### Static
- `pyright` passes
- `ruff check` clean

### Runtime
- Test: `_fmt_game_status(GameStatus(state="pre", scheduled_start=..., detail="~est"))` → `"~1:30 PM"` or `"~in 5m"`
- Test: `_fmt_game_status(GameStatus(state="pre", scheduled_start=..., detail="Q2 5:30"))` → no `~` prefix (confirmed time)
- Visual: launch Talos with a CBA or AFL game, verify the Date column shows `MM/DD` and Game column shows `~HH:MM PM`
- Visual: verify exit-only triggers with toast showing estimated time
