# Scanner Integration — Design Spec

**Date:** 2026-03-14
**Status:** Draft

## Problem

Discovering new events to trade requires running a standalone CLI scanner, copying tickers, and pasting them into the Add Games modal. This is tedious and disconnected from the main workflow.

## Solution

A built-in scan feature triggered by pressing `c`. Fetches all open events from known Kalshi series, filters to valid arb pairs not already monitored, resolves game status for start times, and presents results in a modal DataTable for selection.

## Trigger

Keybinding `c` ("Scan") opens the scan modal. Shows "Scanning..." while fetching, then populates the table.

## Discovery

### Series to Scan

All unique series prefixes from `SOURCE_MAP` plus tennis series:

```python
SCAN_SERIES = [
    # From SOURCE_MAP
    "KXNHLGAME", "KXNBAGAME", "KXMLBGAME", "KXNFLGAME", "KXWNBAGAME",
    "KXCFBGAME", "KXCBBGAME", "KXMLSGAME", "KXEPLGAME",
    "KXAHLGAME",
    "KXLOLGAME", "KXCS2GAME", "KXVALGAME", "KXDOTA2GAME", "KXCODGAME",
    # Tennis
    "KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH",
    "KXATPDOUBLES",
]
```

### API Call

For each series, fetch concurrently with a semaphore (max 4 concurrent requests to avoid rate limiting):
```
GET /events?series_ticker={series}&status=open&with_nested_markets=true&limit=200
```

Pagination is not implemented — max 200 events per series. Acknowledged as a limitation; unlikely to be hit in practice.

### Filters

1. **Exactly 2 markets** — valid arb pair (mutually exclusive)
2. **Not already monitored** — `event_ticker` not in the set of active event tickers (built from `game_manager.active_games`)
3. **Markets still open** — at least one market has `status != "settled"` and `status != "determined"`

## Modal Display

Full-screen `ScanScreen` modal with a `DataTable`. Shows "Scanning..." while API calls are in progress, then populates the table.

| Column | Width | Source |
|--------|-------|--------|
| Spt | 4 | `_SPORT_LEAGUE[series_prefix][0]` |
| Lg | 5 | `_SPORT_LEAGUE[series_prefix][1]` |
| Date | 6 | Game status or `_extract_date_from_ticker` fallback (Pacific) |
| Time | 8 | Game status `scheduled_start` (Pacific), or `—` if unavailable |
| Event | auto | `sub_title` label (same format as main table) |
| V-A | 6 | `markets[0].volume_24h` |
| V-B | 6 | `markets[1].volume_24h` |

Sorted by date/time ascending (soonest first).

### Interactions

- **Arrow keys** — navigate rows
- **Space** — toggle selection on highlighted row (visual indicator: row highlight or checkbox)
- **Enter** — add all selected events to Talos, close modal
- **a** — select ALL rows (then Enter to add)
- **Escape** — close modal without adding

### After Adding

Selected event tickers are passed to `engine.add_games()` which calls `game_manager.add_game()` for each. The modal closes after adding.

## Date/Time Resolution

Two-tier approach for Date/Time columns:

1. **Primary: Game status resolver** — `resolver.resolve_batch()` on discovered events. Works for all series in `SOURCE_MAP` (NHL, AHL, esports, etc.)
2. **Fallback: Ticker date extraction** — `_extract_date_from_ticker()` parses the date from the event ticker (e.g., `KXATPMATCH-26MAR14...` → `2026-03-14`). Works for ALL series including tennis. Provides Date but not Time.

Tennis events will show Date (from ticker) but no Time (no game status provider). All other sports show both.

## Sport/League Mapping

Add missing tennis entries to `_SPORT_LEAGUE` in `widgets.py`:

```python
"KXWTAMATCH": ("TEN", "WTA"),
```

(Other tennis series already mapped: KXATPMATCH, KXATPDOUBLES, KXATPCHALLENGERMATCH, KXWTACHALLENGERMATCH.)

## Architecture

### `GameManager.scan_events() -> list[Event]`

New async method on `GameManager`:
1. Collect series list from `SCAN_SERIES` constant
2. Build set of active event tickers: `{p.event_ticker for p in self.active_games}`
3. Fetch all series concurrently via `asyncio.gather` with `asyncio.Semaphore(4)`
4. Flatten results, filter to 2-market events not already monitored
5. Return list of `Event` objects (with nested markets)

Each individual series fetch is wrapped in try/except — failures log a warning and return empty, never crash the scan.

### `ScanScreen(ModalScreen[list[str] | None])` (new modal in `ui/screens.py`)

- Receives list of `Event` objects + game status dict + volume data
- Renders DataTable with columns above
- Tracks selected rows in a `set[str]`
- Returns list of selected event tickers on Enter, `None` on Escape
- Pattern follows existing `AddGamesScreen` / `BidScreen`

### `TalosApp`

- New binding: `("c", "scan", "Scan")`
- `action_scan()`:
  1. Show toast "Scanning..."
  2. Call `engine.game_manager.scan_events()`
  3. Call `resolver.resolve_batch()` on results (for date/time)
  4. Open `ScanScreen` with results
  5. On modal return: call `engine.add_games(selected_tickers)`

## Error Handling

- API failures during scan: show partial results with a toast notification
- Empty results: show "No new events found" message in the modal
- Individual series fetch failures: log warning, continue with other series
- Rate limit (429): already handled by `KalshiRateLimitError` in REST client; semaphore reduces likelihood

## Testing

- Unit test for `scan_events()`: mock REST responses, verify filtering logic (2-market filter, already-monitored filter)
- Unit test for `ScanScreen`: verify DataTable rendering and selection toggle
