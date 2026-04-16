# Scanner Tree Redesign — Milestone-Driven Discovery and Selection

**Date:** 2026-04-16
**Status:** Approved for implementation planning

## Problem

Two failures, shared root cause.

### Failure 1 — SURVIVOR adverse selection

On 2026-04-15, Talos continued to accept fills on `KXSURVIVORMENTION-26APR16-MRBE` during the live episode. The last fill was at 21:16:11 EDT — 76 minutes into a 90-minute episode that began at 20:00 EDT. The last fill bought YES at 96¢; the market resolved NO. Classic adverse selection against an informed counterparty watching the broadcast.

Cause: `Engine._check_exit_only` relies on `GameStatusResolver` for event timing. For series outside the sports `SOURCE_MAP`, the resolver falls back to `estimate_start_time(expected_expiration_time) = expiration − 3h` (`_DEFAULT_OFFSET` in `game_status.py`). Kalshi's `expected_expiration_time` for this ticker was 2026-04-16T14:00:00Z (10:00 AM EDT Apr 16, +12.5 hours after episode end). `estimate_start_time` therefore placed the event-start at **07:00 AM EDT Apr 16** — nine hours after the actual episode began. The preemptive exit-only trigger (30 min before estimated start) would have fired at 06:30 AM Apr 16, long after Talos had already finished trading during the live episode.

The same broken proxy would have failed in the opposite direction for FED markets. `KXFEDMENTION-26APR` has `expected_expiration_time = 2026-04-29T14:00:00Z` (10 AM EDT Apr 29) but the Powell presser is at **14:30 EDT Apr 29** — 4.5 hours *after* expiration. Expiration-minus-offset is not a usable proxy for event-start on mention markets. It lands on random sides of the actual event depending on market type.

### Failure 2 — Narrow non-sports coverage

Investigation of the settlement history (`~/Downloads/Kalshi-Recent-Activity-Settlement.csv`, 6,849 rows) plus the discovery pipeline (`GameManager.scan_events`) showed that Talos is only looking at 6 of Kalshi's 19 categories and only at events closing within 7 days. Specifically:

- `DEFAULT_NONSPORTS_CATEGORIES` includes `Companies, Politics, Science and Technology, Mentions, Entertainment, World` — excluding Elections (1,260 series), Economics (508), Financials (177), Crypto (225), and others, totalling ~2,700 series across 13 excluded categories.
- `_nonsports_max_days = 7` excludes events like `KXFEDMENTION-26APR` (closes Apr 30, 14 days away).
- Volume gate is hardcoded `> 0`; no configurable threshold.
- No geo / availability filter.
- No milestone awareness.

Settlement history shows near-zero P&L across mentions markets (KXTRUMPMENTION: 7,333 contracts for +$55 total; SURVIVOR: 2,550 contracts for +$1), suggesting the structurally-protected pair-arbitrage was extracting close to no edge after the info-asymmetry tax. Coverage-widening without scheduling protection would amplify the SURVIVOR-class problem.

### Investigation outcome

`https://api.elections.kalshi.com/trade-api/v2/milestones` is a public paginated endpoint that returns curated event-start/end times with `related_event_tickers`. 2,894 upcoming milestones cover most mention markets Talos touches (KXFEDMENTION, KXTRUMPMENTION, KXEARNINGSMENTION*, KXMADDOWMENTION, KXPSAKIMENTION, KXHEARINGMENTION, KXNBAMENTION, KXMLBMENTION, and many more) with real start times sourced by Kalshi staff. A residual ~2-3 series (KXSURVIVORMENTION, KXSNLMENTION) are not curated and would need manual entry.

The fix is therefore not a scheduling heuristic — it is a **source-of-truth change** for event timing, plus a UI that surfaces coverage decisions to the user.

## Goal

Replace `GameManager.scan_events`-driven auto-discovery and `_expiration_fallback`-driven scheduling with:

1. A **discovery layer** that exposes the full Kalshi event catalog to the user.
2. A **user-curated selection** model (tree UI with tickboxes) as the sole source of what Talos monitors.
3. A **milestone-driven resolver cascade** as the sole source of event-start timing for exit-only decisions.
4. A **commit-time validator** that refuses to monitor events without a schedule source.

Structural outcome: it should be impossible for Talos to trade a pinpoint-event market past its event-start without the user having either (a) seen Kalshi's curated start time or (b) explicitly entered one manually.

## Non-goals

- Changing `ArbitrageScanner.evaluate_pair`. The pair-evaluation state machine stays exactly as-is.
- Building a per-user geo-restriction filter. Category-level exclusion (Sports + Entertainment default) handles the pragmatic concern.
- Auto-subscription to new events as they appear in a ticked series. Manual-only selection, as explicit user preference.
- Replacing `GameStatusResolver` for sports. GSR retains its role for live/post-game state transitions in sports markets.
- UI work beyond the tree screen and commit-popup (e.g., no monitoring-screen redesign, no settings-screen revamp beyond adding tree-settings access).
- Migrating historical data. No `selections.json` exists today; new file is created empty on first tree-mode start.

## Design

### 1. Architecture

Four new components plus rewires to three existing ones.

**New components:**

| Component | File | Role |
|---|---|---|
| `DiscoveryService` | `src/talos/discovery.py` | Kalshi discovery cache (categories, series, events). Background refresh loops. Dedicated `asyncio.Semaphore(5)`. |
| `MilestoneResolver` | `src/talos/milestones.py` | Paginated `/milestones` ingest. In-memory index keyed by `event_ticker`. Atomic-swap refresh. |
| `SelectionStore` | `src/talos/selection_store.py` | Persists leaf-level selections to `brain/selections.json`. Emits add/remove events. |
| `TreeScreen` | `src/talos/ui/tree_screen.py` | Textual screen: tree render, tickboxes, filter, keybindings, commit-popup. |

**Modified components:**

- `GameManager` — loses `scan_events()`, `DEFAULT_NONSPORTS_CATEGORIES`, `_nonsports_max_days`, hardcoded `volume_24h > 0` checks. Gains `_on_selection_added` / `_on_selection_removed` handlers, `_winding_down` set, and inventory-aware removal.
- `Engine._check_exit_only` — replaced with resolver cascade (manual → milestone → sports GSR → nothing). `_expiration_fallback` path deleted.
- `GameStatusResolver` — narrowed to sports live/post signals. Scheduling role transferred to `MilestoneResolver`. `estimate_start_time` retained as a library utility.
- `automation_config.py` — gains startup/discovery settings; keeps `exit_only_minutes` single value.

**Data flow:**

```
DiscoveryService ──polls──► Kalshi REST API
      │ cached snapshot
      ▼
TreeScreen ──reads──► MilestoneResolver
      │ tick/untick/commit
      ▼
SelectionStore ──events──► GameManager ──► Scanner + Feeds
  (brain/selections.json)

Engine._check_exit_only ──reads──► MilestoneResolver + SelectionStore (manual overrides)
```

### 2. Data model

#### 2.1 Selection state — `brain/selections.json`

Leaf-level only. A tick at series or category level in the UI expands to individual event entries before persisting. No hierarchical memory of "intent to monitor series X."

```json
{
  "version": 1,
  "updated_at": "2026-04-16T19:42:11Z",
  "selections": [
    {
      "event_ticker": "KXFEDMENTION-26APR",
      "series_ticker": "KXFEDMENTION",
      "category": "Mentions",
      "selected_at": "2026-04-15T20:00:00Z",
      "markets": ["KXFEDMENTION-26APR-YIEL", "KXFEDMENTION-26APR-TRAD"]
    },
    {
      "event_ticker": "KXEARNINGSMENTIONJPM-26APR14",
      "series_ticker": "KXEARNINGSMENTIONJPM",
      "category": "Mentions",
      "selected_at": "2026-04-14T08:00:00Z",
      "markets": null
    }
  ]
}
```

`markets: null` means "all active markets on this event" (today's `add_game` default behavior). `markets: [list]` means only the specified markets.

Rationale for leaf-level: matches "manual-only, no auto-subscribe." A new event appearing in a ticked series is NOT auto-selected — it renders as `[ ]` with a `·NEW` badge, awaiting explicit review.

#### 2.2 Tree settings — `brain/tree_settings.json`

User-tunable via the tree screen. Separate file from selections because change cadence differs (settings: occasional; selections: per-commit).

```json
{
  "version": 1,
  "excluded_categories": ["Sports", "Entertainment"],
  "min_volume_24h": 100,
  "min_open_interest": 0,
  "max_spread_cents": 99,
  "hide_events_past_close": true,

  "manual_event_start": {
    "KXSURVIVORMENTION-26APR23": "2026-04-22T20:00:00-04:00",
    "KXSNLMENTION-26APR25": "none"
  },

  "event_first_seen": {
    "KXTRUMPMENTION-26APR18": "2026-04-16T18:32:00Z"
  },
  "event_reviewed_at": {
    "KXTRUMPMENTION-26APR15": "2026-04-13T09:32:11Z"
  },

  "ui_state": {
    "expanded_categories": ["Politics", "Companies"],
    "expanded_series": ["KXEARNINGSMENTION", "KXTRUMPMENTION"],
    "last_refresh": "2026-04-16T19:42:11Z"
  }
}
```

`manual_event_start` values:
- ISO 8601 datetime — explicit event-start.
- `"none"` — explicit user opt-out from exit-only for this event.
- Missing key — no manual override; resolver cascade consults milestone/GSR.

Filter settings (`excluded_categories`, `min_volume_24h`, etc.) affect **tree rendering only**. They do not auto-remove entries from `selections.json`. A selected event that falls below `min_volume_24h` keeps being monitored; the filter just hides it from the tree view.

#### 2.3 Discovery cache — in-memory only

Pydantic v2 models, rebuilt each session. Not persisted.

```python
class CategoryNode(BaseModel):
    name: str
    series_count: int
    series: dict[str, "SeriesNode"]

class SeriesNode(BaseModel):
    ticker: str                              # "KXFEDMENTION"
    title: str
    category: str
    tags: list[str]
    frequency: str                           # one_off | weekly | annual | ...
    events: dict[str, "EventNode"] | None   # None = not fetched yet
    events_loaded_at: datetime | None

class EventNode(BaseModel):
    ticker: str
    series_ticker: str
    title: str
    sub_title: str
    close_time: datetime | None
    milestone: Milestone | None              # resolved from MilestoneIndex
    markets: list[MarketNode]
    fetched_at: datetime

class MarketNode(BaseModel):
    ticker: str
    title: str
    yes_bid: int | None
    yes_ask: int | None
    volume_24h: int
    open_interest: int
    status: str
```

#### 2.4 Milestone index — in-memory only

```python
class Milestone(BaseModel):
    id: str
    category: str
    type: str                              # one_off_milestone | fomc_meeting | ...
    start_date: datetime
    end_date: datetime
    title: str
    related_event_tickers: list[str]

class MilestoneIndex:
    by_event_ticker: dict[str, Milestone]
    last_refresh: datetime
```

Exposed as `MilestoneResolver.event_start(event_ticker) -> datetime | None`.

### 3. Discovery pipeline

#### 3.1 Startup bootstrap

Runs once, in background (does not block Engine startup beyond the gate in §5.3).

1. `GET /series` — returns all ~9,700 series in one response (~11 MB).
2. Group by category. Build `CategoryNode` tree skeleton with `SeriesNode` stubs.
3. Parallel: paginate `GET /milestones?minimum_start_date=<now>&limit=200` until cursor exhausted. Build `MilestoneIndex` by `related_event_ticker`.
4. Emit `discovery_ready`.

Cost: ~16 API calls, ~3 seconds elapsed.

Events are not fetched at startup. Series nodes are stubs until the user expands them.

#### 3.2 Lazy event fetch

Triggered by tree-expand of a series node.

```
SeriesNode.events is None OR events_loaded_at older than 5 min?
  → GET /events?series_ticker=<ticker>&status=open&with_nested_markets=true&limit=200
  → Build EventNode + MarketNode list, attach Milestone from index
  → Cache in SeriesNode.events
  → Emit series_events_loaded(series_ticker)
```

Cost: 1 API call per expand. Cached for 5 min (re-expand within window is free).

#### 3.3 Milestone refresh loop

Background timer, 5-minute interval (configurable via `automation_config.milestone_refresh_seconds`).

```python
async def milestone_refresh_loop():
    while running:
        try:
            async with discovery_sem:
                new_index = await fetch_all_upcoming_milestones()
                milestone_index.replace_atomic(new_index)
                emit("milestones_refreshed")
        except Exception:
            logger.warning("milestone_refresh_failed", exc_info=True)
        await asyncio.sleep(milestone_refresh_seconds)
```

Atomic replacement — readers (Engine cascade) never see partial updates. Cost per tick: ~15 calls, ~3 seconds, 0.05 req/s average.

#### 3.4 Manual refresh

TreeScreen action (keybinding `r`):

1. Re-run §3.1 bootstrap.
2. Clear all `events_loaded_at` to force re-fetch on next expand.
3. Preserve UI state (expanded nodes, selection, scroll position).

#### 3.5 Semaphore topology

```
DiscoveryService.discovery_sem (5 slots)
         ↓
KalshiRESTClient._sem (20 slots)
         ↓
httpx.AsyncClient
```

Trading calls (order/cancel/position) acquire only the 20-slot pool. Discovery calls acquire both. Worst case: 5 discovery slots held simultaneously, 15 slots remain for trading. Discovery cannot starve trading.

#### 3.6 Error handling

| Failure | Response |
|---|---|
| HTTP 4xx on `/series` | Keep last good snapshot. UI: startup-failed banner. |
| HTTP 4xx on `/events?series_ticker=X` | Mark that SeriesNode stale. Keep cached events. UI: stale badge on row. |
| HTTP 4xx on `/milestones` | Keep old index. Retry next cycle. UI: "milestones: stale" top banner. |
| HTTP 429 | Exponential backoff: 2s → 4s → 8s → cap 30s. Warning-level log. |
| Timeout | Same as 4xx: keep cache, mark stale, retry on next trigger. |
| Network down | Tree renders from cache with global "offline" banner. Selections still saveable. |

Discovery failures never block trading. Engine cascade degrades gracefully through resolver levels.

### 4. Tree UI

#### 4.1 Screen layout

```
┌─────────────────────────────────────────────────────────────────────┐
│ Talos › Tree Selection                      [refresh: 2m ago] [?]   │
├─────────────────────────────────────────────────────────────────────┤
│ filter: [____________________]  category: [all ▼]  □ hide no-vol   │
├─────────────────────────────────────────────────────────────────────┤
│   Tree area (scrollable)                                            │
├─────────────────────────────────────────────────────────────────────┤
│ Selection: 14 events · 6 series · 23 markets · 2 no-milestone · 7 NEW│
└─────────────────────────────────────────────────────────────────────┘
```

Pushable screen — does not replace the main monitoring view. `escape` returns.

#### 4.2 Tickbox states

| Glyph | Meaning |
|---|---|
| `[ ]` | Never ticked, already reviewed |
| `[ ] ·NEW` | Never ticked, never reviewed |
| `[·]` | Was ticked, you unticked on purpose |
| `[W]` | Was ticked, unticked, winding down (has inventory) |
| `[-]` | Category/series partial state |
| `[✓]` | Currently ticked |

Events remain visible in the tree until Kalshi changes event `status` to `closed` or `finalized`. The distinction between `[ ]` (reviewed-and-passed) and `[·]` (deliberately-unticked) preserves user intent across sessions.

#### 4.3 Node rendering

```
[ ] Politics                                    245/312 open · 12 NEW
  [-] KXTRUMPMENTION                            3/4 ·NEW
    [✓] KXTRUMPMENTION-26APR15                  begins in 2h 14m  "Mornings with Maria"
    [✓] KXTRUMPMENTION-26APR16                  begins in 7h 05m  "Roundtable on No Tax on Tips"
    [·] KXTRUMPMENTION-26APR17                  begins in 1d 3h   "Remarks on Economy"
    [ ] KXTRUMPMENTION-26APR18 ·NEW             begins in 2d 8h   "Press conference"
```

Low-volume markets (below `min_volume_24h`) under an expanded event are folded into a single expander:

```
    ├── [✓] KXFEDMENTION-26APR-YIEL     (liquid)
    └── ▸ 38 low-volume markets hidden  (expand to show)
```

Expanding the "hidden" row inlines them below, individually tickable.

#### 4.4 Keybindings

| Key | Action |
|---|---|
| `↑` / `↓` | Move cursor |
| `enter` / `→` | Expand node (marks events reviewed) |
| `←` | Collapse node |
| `shift+→` | Expand all descendants (sweeps all NEW flags in one stroke) |
| `shift+←` | Collapse all descendants |
| `space` | Toggle tickbox on current node (single-level expand-to-leaves) |
| `shift+space` | Toggle tickbox on all visible descendants |
| `/` | Focus filter |
| `c` | Commit staged changes |
| `r` | Manual refresh |
| `n` | Jump to next NEW or conflicted node |
| `e` | Edit manual event-start for current node (pre-commit override) |
| `?` | Help |
| `escape` | Back |

#### 4.5 NEW indicator

- `event_first_seen[ticker]` — written the first time DiscoveryService reports an event the store hasn't seen.
- `event_reviewed_at[ticker]` — written when user expands the event OR ticks it.
- An event is NEW iff `first_seen_at is set AND reviewed_at is unset`.
- Propagation: SeriesNode is NEW iff any descendant is NEW. CategoryNode is NEW iff any descendant series is NEW.
- Fields persist in `tree_settings.json`.

#### 4.6 Commit flow

Selections are **staged** until commit (`c`). Reasons:

- Ticking a series that expands to 10 events, then unticking 2 before commit, produces one clean batch — not 10 subscribes + 2 unsubscribes.
- Inventory-impacting unticks get a single consolidated confirmation.
- Commit validator can inspect all staged changes at once.

Uncommitted state shows in footer: `* 3 changes pending`. `escape` with uncommitted changes prompts confirmation.

#### 4.7 Commit-time schedule validator

On commit, before firing events to `GameManager`:

1. For each staged addition, resolve the event's start time via the cascade (manual → milestone → sports GSR).
2. If no source returns a value AND the user has not set `manual_event_start: "none"`, flag as needing-schedule.
3. If any needing-schedule events exist, open popup:

```
┌─── Event-start times required ─────────────────────────────────────┐
│ 3 selected events have no milestone from Kalshi.                   │
│                                                                    │
│  KXSURVIVORMENTION-26APR23  "Episode 9"                            │
│   Event starts: [2026-04-22_20:00_EDT_______]  ○ no exit-only      │
│                                                                    │
│  KXSNLMENTION-26APR18       "April 18 broadcast"                   │
│   Event starts: [2026-04-18_23:30_EDT_______]  ○ no exit-only      │
│                                                                    │
│  KXWEIRDONEOFFMENTION-26APR20  "some new curation"                 │
│   Event starts: [____________________________]  ○ no exit-only     │
│                                                                    │
│         [cancel commit]       [save all & commit]                  │
└────────────────────────────────────────────────────────────────────┘
```

4. On `save all & commit`: times persist to `manual_event_start`, then `GameManager.add_game()` for each.
5. On `cancel commit`: staged selections preserved in tree. No events added. User returns to tree.
6. If no needing-schedule events: no popup. Commit proceeds immediately.

Optional escape hatch: `e` keybinding on a tree row opens the single-row version of this popup pre-commit.

#### 4.8 Schedule conflict handling

When a manual override exists AND a milestone subsequently appears (or an existing milestone's `start_date` shifts), compare `manual_event_start` to `milestone.start_date`. If `|delta| > schedule_conflict_threshold_minutes` (default 5 min), raise a conflict on that event:

- UI badge `⚠ schedule conflict` on the tree row.
- Count in footer: `2 conflicts`.
- `n` hotkey jumps to next conflict as well as next NEW.

Clicking opens:

```
┌─── Schedule conflict: KXSURVIVORMENTION-26APR23 ───┐
│ Your manual entry:    2026-04-22 20:00 EDT          │
│ Kalshi milestone:     2026-04-22 20:05 EDT          │
│ Difference:           5 min                         │
│                                                     │
│ ( ) Keep my manual entry                            │
│ ( ) Use Kalshi's milestone                          │
│ ( ) Edit manually...                                │
│                                                     │
│        [cancel]   [resolve]                         │
└─────────────────────────────────────────────────────┘
```

While unresolved, the **manual entry remains active**. User's explicit decision is not silently superseded.

### 5. Integration with existing systems

#### 5.1 SelectionStore ↔ GameManager

`SelectionStore` emits `selection_added(event_ticker, markets)` and `selection_removed(event_ticker)`.

```python
async def _on_selection_added(self, event_ticker, markets):
    try:
        pair = await self.add_game(event_ticker)
        if markets is not None:
            # Filter subscriptions to selected markets only
            await self._restrict_markets(pair, markets)
    except Exception:
        logger.warning("selection_add_failed", event_ticker=event_ticker, exc_info=True)
        # Do NOT remove from SelectionStore. User intent stands; retry on next restart
        # or manual refresh.

async def _on_selection_removed(self, event_ticker):
    pair = self._games.get(event_ticker)
    if pair is None:
        return
    ledger = self._engine.get_ledger(event_ticker)
    if ledger and (ledger.has_filled_positions() or ledger.has_resting_orders()):
        self._winding_down.add(event_ticker)
        await self._engine.enforce_exit_only(event_ticker)
        logger.info("winding_down_started", event_ticker=event_ticker,
                    filled_a=ledger.filled_count(Side.A),
                    filled_b=ledger.filled_count(Side.B))
        return
    await self.remove_game(event_ticker)
```

`_winding_down` set is checked each engine tick. When the ledger for a winding-down event clears, `remove_game` is called automatically and the event emits `winding_down_completed`.

#### 5.2 Engine._check_exit_only cascade

```python
def _check_exit_only(self):
    now = datetime.now(UTC)
    for pair in self._scanner.pairs:
        event = pair.event_ticker
        if event in self._exit_only_events:
            continue

        start_time, source = self._resolve_event_start(event)

        if source == "manual_opt_out":
            continue

        if source is None:
            self._log_once("exit_only_no_schedule", event=event)
            continue

        # Sports GSR supplies live/post state transitions
        if source == "sports_gsr":
            gs = self._gsr.get(event)
            if gs.state in ("live", "post"):
                self._flip_exit_only(event, reason=f"sports_{gs.state}")
                continue

        # Preemptive lead-time trigger
        lead_min = self._auto_config.exit_only_minutes
        if (start_time - now).total_seconds() < lead_min * 60:
            self._flip_exit_only(event, reason=source, scheduled_start=start_time)

def _resolve_event_start(self, event) -> tuple[datetime | None, str | None]:
    # 1. Manual override (user owns this)
    manual = self._selection_store.manual_event_start(event)
    if manual == "none":
        return (None, "manual_opt_out")
    if manual is not None:
        return (manual, "manual")

    # 2. Kalshi milestone
    ms = self._milestone_resolver.event_start(event)
    if ms is not None:
        return (ms, "milestone")

    # 3. Sports GSR
    gs = self._gsr.get(event)
    if gs and gs.scheduled_start:
        return (gs.scheduled_start, "sports_gsr")

    # 4. Nothing
    return (None, None)
```

`exit_only_minutes` stays as a single global setting (default 30.0). Applied uniformly across resolver sources.

#### 5.3 Startup sequence — safety-first gate

```
t=0.0   Process starts
t=0.1   selections.json + tree_settings.json loaded
t=0.5   SelectionStore restores pairs (GameManager.add_game loop, background)
t=0.5   DiscoveryService + MilestoneResolver start
t=2-5   Milestones fully loaded
t=5     Engine begins tick loop — all resolvers armed
t=30    Hard cap: if milestones still not loaded, Engine starts with red
        warning banner (exit-only scheduling degraded) and logs.
```

Engine does not begin the trading loop until either (a) `milestones_ready` emits, or (b) 30-second fallback expires. Hard cap avoids deadlock on Kalshi outages.

Rationale for Option B over Option A (proceed immediately): Principle "Safety over speed" — a ~5-second startup delay is recoverable; trading without an armed resolver cascade is not. See `brain/principles.md`.

#### 5.4 Deletions

| Code | Fate |
|---|---|
| `GameManager.scan_events()` | Deleted in Phase 5. |
| `DEFAULT_NONSPORTS_CATEGORIES` | Deleted. Tree filters replace. |
| `_nonsports_max_days` | Deleted. No close-time window gate. |
| `volume_24h > 0` hardcodes (`game_manager.py:559, 694`) | Deleted. `min_volume_24h` tree setting applies at discovery. GameManager trusts SelectionStore. |
| `SPORTS_SERIES` list | Retained, but narrowed role — sports live/post resolution only, not discovery. |
| `_expiration_fallback` in `GameStatusResolver` | Deleted. `estimate_start_time` retained as library utility. |
| Engine's scheduled call to `scan_events()` | Deleted from refresh loop. |

### 6. Settings inventory

#### 6.1 `automation_config.py` additions

```python
# Existing — unchanged
exit_only_minutes: float = 30.0

# New
tree_mode: bool = False                              # feature flag
startup_milestone_wait_seconds: float = 30.0         # hard cap on startup gate
schedule_conflict_threshold_minutes: float = 5.0     # delta that triggers conflict
discovery_concurrent_limit: int = 5                  # DiscoveryService semaphore
milestone_refresh_seconds: float = 300.0             # 5 min default
```

#### 6.2 Tree settings (JSON, see §2.2)

Changes via TreeScreen settings panel. Occasional cadence.

#### 6.3 Selection state (JSON, see §2.1)

Changes via TreeScreen commit. Per-commit cadence.

#### 6.4 Principles.md addition

New principle to be landed alongside Phase 1 scaffold. Working text (to be refined at write time):

> **Principle N: Safety over speed.** When trading and scheduling decisions are time-sensitive, prefer delay or pause over proceeding on incomplete data. A five-second delayed decision is recoverable; a decision made with stale or missing data is not. This applies to startup sequencing, resolver cascades, milestone conflicts, and any path where "trade now" competes with "verify first."

### 7. Migration plan

#### 7.1 Feature flag

`automation_config.tree_mode: bool = False` — all new behavior gated behind this.

- `tree_mode = False`: today's behavior unchanged. `scan_events` runs. `_expiration_fallback` used. No DiscoveryService, no TreeScreen.
- `tree_mode = True`: new behavior active. Old paths bypassed but not deleted until Phase 5.

#### 7.2 Phases

**Phase 1 — Scaffold.** Land new components (DiscoveryService, MilestoneResolver, SelectionStore, TreeScreen) and resolver cascade behind the flag. Add principle to `brain/principles.md`. Unit tests per module. Flag defaults `False`; normal sessions see no behavior change.

**Phase 2 — Dogfood.** Flip `tree_mode = True` locally. Tick a handful of representative events (one covered milestone, one uncovered, one sports, one earnings). Verify: milestone loading, resolver cascade, commit popup, conflict prompt, winding-down.

**Phase 3 — Dual-run.** Alternate sessions with flag on/off. Confirm old behavior still works when flag off (regression protection). Verify state files from tree_mode sessions don't break legacy sessions.

**Phase 4 — Default on.** Flip default to `tree_mode = True`. Legacy paths remain but unused in normal operation.

**Phase 5 — Cleanup.** Delete §5.4 listed code. Delete `tree_mode` flag. Single cleanup PR, easy to review.

#### 7.3 State migration

No existing state to migrate. `selections.json` and `tree_settings.json` created empty on first Phase 2 start.

Active `_games` in GameManager at flag-flip time: auto-seeded into SelectionStore as the initial selection set (one-time, on first tree_mode start). Prevents losing active monitoring state when switching modes.

#### 7.4 Rollback

Any phase: set `tree_mode = False`, restart. Old paths resume. `selections.json` untouched — next re-enable picks up where it left off.

Catastrophic bug discovered after Phase 5 cleanup: `git revert` of the cleanup commit restores legacy paths verbatim.

### 8. Testing strategy

**Unit tests:**
- `DiscoveryService`: mocked httpx, pagination, error handling, semaphore bounds.
- `MilestoneResolver`: index building, atomic replace, refresh loop.
- `SelectionStore`: persistence round-trip, event emission, three-state computation.
- `Engine._resolve_event_start`: cascade order, each branch, manual opt-out, missing schedule.
- Schedule conflict detection: threshold edge cases, time-zone correctness.

**Integration tests:**
- Commit flow: selection → add_game → pair registered.
- Uncurated event: commit popup required → override persists → resolver uses override.
- Conflict: milestone appears after manual entry → flagged → manual remains active.
- Winding-down: untick with inventory → retained in exit-only → flat → auto-removed.
- Startup gate: Engine waits for milestones OR proceeds after 30s with banner.
- Flag-off regression: `tree_mode = False` preserves today's behavior.

**Replay tests:**
- SURVIVOR scenario (Apr 15 tick + orderbook data): replay against `tree_mode = True` with manual override set. Assert no fills after exit-only trigger.
- FED scenario (hypothetical): verify exit-only fires 30 min before 14:30 EDT (the real presser), not at 07:00 EDT (the current broken estimate).

**Soak test:**
- Between Phase 4 and Phase 5, at least one full multi-day soak session with tree_mode on and representative selection set. Confirms stability under sustained load.

### 9. Observability

New structured log keys:

```python
logger.info("tree_selection_committed", added=[...], removed=[...], source="user")
logger.info("resolver_cascade", event=..., source=..., start_time=..., lead_min=...)
logger.info("milestone_conflict", event=..., manual=..., milestone=..., delta_min=...)
logger.info("milestone_conflict_resolved", event=..., choice=..., value=...)
logger.info("startup_gate_ready", elapsed_s=..., milestones_loaded=True)
logger.warning("startup_gate_timeout", elapsed_s=30, milestones_loaded=False)
logger.info("winding_down_started", event=..., filled_a=..., filled_b=...)
logger.info("winding_down_completed", event=..., duration_s=...)
logger.info("discovery_startup", series_count=..., milestone_count=..., elapsed_s=...)
logger.warning("discovery_fetch_failed", scope=..., exc_info=True)
logger.info("tree_manual_refresh", elapsed_s=...)
```

All route to the existing `data_collector` replay log. Post-hoc analysis (as was used to diagnose SURVIVOR) remains straightforward.

## Open questions deferred to implementation

- **`[·]` decay:** whether deliberately-unticked state should age back to plain `[ ]` after N days. Keeping sticky for v1.
- **Re-ticking a winding-down event:** whether this cancels exit-only immediately, or requires explicit confirmation. Current plan: confirmation dialog ("Re-ticking during exit-only will cancel winding-down and re-engage trading near event start. Confirm?"). Exact UX to be finalized during implementation.
- **Tree settings editing UI:** Phase 1 ships with JSON-file-editable settings; an in-tree settings panel is deferred to a follow-up.
- **Geo / broker-availability filter:** deferred. Category exclusion suffices for current needs.
- **Category-level refresh granularity:** for now the manual refresh is single-button (nuke + rebuild); per-node refresh deferred.

## Exit criteria

Design is complete. Implementation plan (via the `writing-plans` skill) is the next step.

Phase 1 implementation is considered done when:
- All new modules have passing unit tests.
- `tree_mode = True` sessions exhibit the resolver cascade producing correct exit-only triggers for at least 3 event types: (a) milestone-covered (KXFEDMENTION), (b) manually-overridden (KXSURVIVORMENTION), (c) sports GSR (KXNBAGAME).
- SURVIVOR replay test passes.
- No regressions under `tree_mode = False`.
