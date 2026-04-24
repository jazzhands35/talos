# Non-Sports Event Scanning

## Problem

Talos can only scan hardcoded sports series tickers. Non-sports events (weather, crypto, politics, etc.) have no discovery mechanism. The `NON_SPORTS_SERIES` list exists but is empty. Non-sports series are numerous and change frequently — a hardcoded list is unmaintainable.

## Solution

Broad paginated query of all open events on Kalshi, filtered client-side by category and close-time window. No hardcoded non-sports series list. True discovery — catches every new series automatically.

## Strategy

Non-sports events use the existing YES/NO single-market arbitrage path. `add_game()` already handles this: 1 active market → auto YES/NO pair, 2+ active markets → `MarketPickerNeeded` triggers the market picker modal.

## Design

### 1. REST Client — Paginated Event Query

Add `get_all_events()` to `rest_client.py`, following the existing `get_all_orders()` pattern:

- Accepts same params as `get_events()` (status, series_ticker, min_close_ts, with_nested_markets)
- Paginates via `cursor` until exhausted, with a `max_pages` safeguard (default 20) to prevent runaway pagination
- Returns complete `list[Event]`

**Volume estimate:** Kalshi has thousands of open events total, but `min_close_ts=now` excludes already-closed events. With 200 events/page and ~1000-2000 open events, expect 5-10 pages (sequential, not parallelizable). At Kalshi's 20 reads/sec limit, this adds ~1-2 seconds to scan time. Acceptable since scan is user-initiated (press `c`), not a polling loop.

**No server-side `max_close_ts`** — the Kalshi API only supports `min_close_ts`. The 7-day window must be applied client-side.

The sports scan path (per-series with `limit=200`) is unchanged.

### 2. GameManager — Non-Sports Scan Path

Modify `scan_events()` in `game_manager.py` to add a second scan path alongside sports:

**Non-sports path:**
1. Call `get_all_events(status="open", with_nested_markets=True, min_close_ts=<now_unix>)` — no `series_ticker`, paginate all results
2. Filter client-side:
   - `event.category in enabled_categories` (configurable set)
   - `event.series_ticker not in _SPORTS_SET` (avoid double-counting)
   - At least one market with `close_time` ≤ now + `max_days` (configurable, default 7)
   - At least one active market (`market.status == "active"`)
   - Exactly 1 active market for auto-add, OR 2+ for market picker (both pass scan filter)
   - Not already monitored (`event_ticker not in active_tickers`)
3. Merge with sports results into one list

**Multi-market non-sports events:** Events with 2+ active markets appear in scan results. When added in batch (selecting multiple from scan), `add_games()` swallows `MarketPickerNeeded` and logs a skip. When added individually (selecting just one), the market picker modal fires. This matches existing behavior — document in the scan screen header so the user knows to add multi-market events one at a time.

**Constructor changes:**
- Accept `nonsports_categories: list[str]` (enabled categories)
- Accept `nonsports_max_days: int` (time window, default 7)
- Remove `NON_SPORTS_SERIES` list entirely

**Time filter helper:** Pure function that checks whether any market on an event has `close_time` within the configured window. Handles `close_time` as ISO string → datetime comparison. Events where all markets have `close_time=None` are excluded.

### 2b. `refresh_volumes()` Fix

`refresh_volumes()` extracts the series prefix via `pair.event_ticker.split("-")[0]`. For non-sports YES/NO pairs, `pair.event_ticker` is the market ticker (e.g., `KXBTC-26MAR28-T100000`), and `split("-")[0]` yields `KXBTC` — which may not match the actual series ticker.

**Fix:** Store `series_ticker` on `ArbPair` (it's already available from the `Event` at add time). `refresh_volumes()` uses `pair.series_ticker` directly instead of parsing the event ticker. Falls back to `split("-")[0]` if `series_ticker` is not set (backward compat with restored pairs from `games_full.json`).

### 3. Settings & Configuration

New fields in `settings.json` (loaded/saved via `persistence.py`):

```json
{
  "unit_size": 1,
  "nonsports_categories": [
    "Climate and Weather",
    "Crypto",
    "Companies",
    "Politics",
    "Science and Technology",
    "Mentions",
    "Entertainment",
    "World"
  ],
  "nonsports_max_days": 7
}
```

- `nonsports_categories` — list of Kalshi API category strings. All 8 enabled by default. Empty list disables non-sports scanning.
- `nonsports_max_days` — max days until market close. Default 7.

**Category string mapping (website → API):**

| Website URL slug | API `category` field |
|------------------|---------------------|
| climate | `"Climate and Weather"` |
| crypto | `"Crypto"` |
| companies | `"Companies"` |
| financials | TBD — verify at implementation |
| science | `"Science and Technology"` |
| mentions | `"Mentions"` |
| culture | `"Entertainment"` |
| politics | `"Politics"` |

Note: API also has `"Elections"`, `"Health"`, `"World"` which are not on the user's initial website list. `"World"` is included in defaults. The exact string for "financials" needs verification against live API data.

### 4. Wiring (`__main__.py`)

Thread new settings from `load_settings()` into `GameManager` construction:

```python
nonsports_categories = settings.get("nonsports_categories", DEFAULT_CATEGORIES)
nonsports_max_days = int(settings.get("nonsports_max_days", 7))
game_mgr = GameManager(
    rest, feed, scanner,
    sports_enabled=auto_config.sports_enabled,
    nonsports_categories=nonsports_categories,
    nonsports_max_days=nonsports_max_days,
)
```

### 5. ScanScreen Adaptation (`screens.py`)

The ScanScreen modal adapts for mixed sports + non-sports results:

**Columns stay the same:** `✓ | Spt | Lg | Date | Time | Event | 24h A | 24h B`

**Per-row behavior for non-sports:**
- **Spt/Lg** — show short category abbreviation and series short code (reuse existing `_SPORT_LEAGUE` pattern, extend with category-based entries)
- **Date/Time** — derived from earliest market `close_time` (ISO 8601 string, parsed to local timezone). Non-sports events have no `GameStatus` and their tickers don't follow the sports date pattern, so the existing `_extract_date_from_ticker()` won't work. Add a third fallback path: if no `GameStatus` and no ticker date match, parse `close_time` from the first active market on the event. This requires passing market data alongside events to `ScanScreen`, or pre-computing a `close_time` map in `_run_scan()`.
- **Event** — `event.title` (non-sports `sub_title` is often empty)
- **24h A** — market volume
- **24h B** — `—` for single-market events (both legs are on the same ticker)

**Sort:** All events sorted by time ascending (soonest first), sports and non-sports mixed.

**Game status column in main table:** Non-sports events will show `—` in the Game column since there's no external status provider (ESPN/PandaScore). This is expected.

### 6. Category Label Mapping (`widgets.py`)

Add short category labels for non-sports display, extending the existing `_SPORT_LEAGUE` pattern:

```python
_CATEGORY_SHORT = {
    "Climate and Weather": "Clim",
    "Crypto": "Cryp",
    "Companies": "Comp",
    "Politics": "Pol",
    "Science and Technology": "Sci",
    "Mentions": "Ment",
    "Entertainment": "Ent",
    "World": "Wrld",
}
```

The ScanScreen and main table fall back to this mapping when a series ticker isn't found in `_SPORT_LEAGUE`. For the "Spt" column, use the category short label. For the "Lg" column, use the series ticker prefix (e.g., `KXBTC`, `KXRT`) as a stand-in — this provides useful info about which specific series the event belongs to.

## What Doesn't Change

- **Main OpportunitiesTable** — untouched, non-sports events already display fine
- **`add_game()` logic** — already handles single-market YES/NO and multi-market picker
- **`ArbitrageScanner`** — evaluates pairs regardless of sports/non-sports
- **`MarketFeed` / `PositionLedger`** — agnostic to event type
- **Fee calculations, order placement** — downstream of add, unchanged
- **`AutomationConfig`** — no new fields needed

## Files Modified

| File | Change |
|------|--------|
| `src/talos/rest_client.py` | Add `get_all_events()` with cursor pagination |
| `src/talos/game_manager.py` | Non-sports scan path, remove `NON_SPORTS_SERIES`, accept category/max_days config, fix `refresh_volumes()` to use `pair.series_ticker` |
| `src/talos/models/strategy.py` | Add `series_ticker` field to `ArbPair` |
| `src/talos/persistence.py` | No code changes — existing `load_settings`/`save_settings` handles new keys |
| `src/talos/__main__.py` | Thread new settings into `GameManager` |
| `src/talos/ui/screens.py` | ScanScreen adaptation for non-sports rows (close_time fallback for date/time) |
| `src/talos/ui/app.py` | Update data collector logging — `series_scanned` should reflect non-sports broad query |
| `src/talos/ui/widgets.py` | Category short label mapping |
| `settings.json` | New `nonsports_categories` and `nonsports_max_days` fields |

## Testing

- `test_rest_client.py` — pagination test for `get_all_events()` (multi-page, empty result, error on page N)
- `test_game_manager.py` — non-sports scan filtering:
  - Category inclusion/exclusion
  - Time window: events within/beyond `max_days`
  - Events with `close_time=None` on all markets excluded
  - Sports events excluded from non-sports results (dedup)
  - Already-monitored events excluded
  - `max_pages` safeguard stops pagination
- `test_game_manager.py` — `refresh_volumes()` with non-sports pairs (uses `series_ticker` not parsed prefix)
- `test_scan_screen.py` — mixed sports/non-sports display, close_time date/time derivation
- Manual smoke test — run scan with live API, verify non-sports events appear with correct categories and close times
