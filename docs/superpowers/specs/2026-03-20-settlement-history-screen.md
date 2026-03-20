# Settlement History Screen — Design Spec

## Overview

A full-screen modal showing settled events grouped by day, with per-event two-row detail and estimated vs actual P&L comparison. Accessed via `h` hotkey.

## Data Source

`rest.get_settlements(limit=200)` returns `list[Settlement]` with fields:
- `ticker`, `event_ticker`, `market_result`, `revenue` (cents), `fee_cost` (cents)
- `no_count`, `no_total_cost`, `yes_count`, `yes_total_cost`, `settled_time` (ISO string)

Group settlements by `event_ticker` to pair the two legs. Sort by `settled_time` descending (newest day first).

## Screen Layout

Full-screen `ModalScreen[None]` following ScanScreen pattern. Contains a `DataTable` with day separator rows.

### Day Separator Rows

Non-selectable header rows styled dim/bold:
```
─── Mar 20 ──────────────────────── Day P&L: $38.50 ───
```

Day P&L = sum of `revenue - cost` for all settlements in that day (PT timezone).

### Event Rows (2 per event)

Same two-row pattern as the main table:

| Col | Name | Width | Row A | Row B |
|-----|------|-------|-------|-------|
| 0 | Team | auto | Team A name (from sub_title or ticker) | Team B name |
| 1 | Lg | 5 | League abbr | blank |
| 2 | Result | 5 | `W` / `L` (market_result) | `W` / `L` |
| 3 | NO | 5 | NO price (derived: `no_total_cost / no_count`) | same |
| 4 | Qty | 5 | `no_count` contracts | same |
| 5 | Cost | 8 | `no_total_cost` in dollars | same |
| 6 | Revenue | 8 | `revenue` in dollars (row A only, event-level) | blank |
| 7 | Profit | 8 | `revenue - total_cost` in dollars | blank |
| 8 | Est P&L | 8 | Our `locked_profit_cents` if available, else `—` | blank |
| 9 | Actual | 8 | Kalshi's `revenue - cost` (same as Profit) | blank |
| 10 | Settled | 9 | Time in PT (`HH:MM PM`) | blank |

**Team name extraction:** Use `extract_leg_labels()` from `game_manager.py` if `event_ticker` is in the game manager's `subtitles` dict. Otherwise fall back to the market ticker.

**Result column:** `market_result` from Settlement — typically `"yes"` or `"no"`. Show `W` (green) if the result matches the side we bought (NO arb = we want `"no"` result = W), `L` (red) otherwise. Since we always buy NO, `"no"` result = `W`, `"yes"` result = `L`.

**Est P&L:** From `engine.position_summaries` matched by `event_ticker`. Only available for events still in the position tracker. Shows `—` for events already cleaned up. This is event-level (sum of matched pair profit).

**Actual P&L:** `revenue - (no_total_cost + yes_total_cost)` summed across both legs of the event. This is the authoritative Kalshi number.

## Hotkey & Wiring

- Bind `h` → `action_settlement_history` in `TalosApp.BINDINGS`
- Action dispatches `@work(thread=False)` async method that:
  1. Fetches settlements via `self._engine._rest.get_settlements(limit=200)`
  2. Pushes `SettlementHistoryScreen(settlements, position_summaries, subtitles)`
- Screen is dismiss-only (Escape), returns `None`

## File Structure

| File | Changes |
|------|---------|
| `src/talos/ui/screens.py` | Add `SettlementHistoryScreen(ModalScreen[None])` |
| `src/talos/ui/app.py` | Add `h` binding, `action_settlement_history`, async fetch+push |
| `tests/test_settlement_history.py` | New: test grouping, day headers, P&L calc, two-row layout |

## Grouping Logic

```python
# Group by event_ticker
events: dict[str, list[Settlement]] = {}
for s in settlements:
    events.setdefault(s.event_ticker, []).append(s)

# Group by day (PT timezone)
days: dict[str, list[tuple[str, list[Settlement]]]] = {}
for evt, legs in events.items():
    day_str = parse_settled_time(legs[0].settled_time).strftime("%b %d")
    days.setdefault(day_str, []).append((evt, legs))

# Sort days descending, events within day by settled_time descending
```

## Styling

- Day separator rows: `SURFACE2` color, bold, non-selectable
- Profit positive: `GREEN`, negative: `RED`
- Est vs Actual match: normal. Discrepancy > 5¢: `YELLOW` warning highlight on the row
- Reuse existing theme colors from `ui/theme.py`
