# Scanner Tree Redesign — Milestone-Driven Discovery and Selection

**Date:** 2026-04-16
**Status:** Approved for implementation planning (revised after Codex review, 2026-04-16)

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
- Migrating historical data. `games_full.json` already exists; schema gains one optional field (`source`). `tree_metadata.json` is created empty on first write.

## Design

### 1. Architecture

Three new components plus rewires to two existing ones.

**New components:**

| Component | File | Role |
|---|---|---|
| `DiscoveryService` | `src/talos/discovery.py` | Kalshi discovery cache (categories, series, events). Background refresh loops. Dedicated `asyncio.Semaphore(5)`. |
| `MilestoneResolver` | `src/talos/milestones.py` | Paginated `/milestones` ingest. In-memory index keyed by `event_ticker`. Atomic-swap refresh. |
| `TreeMetadataStore` | `src/talos/tree_metadata.py` | Persists **event-level** metadata (first-seen, reviewed-at, manual_event_start, deliberately-unticked set) to `tree_metadata.json`. Does NOT store pair selections — those live in `games_full.json`. |
| `TreeScreen` | `src/talos/ui/tree_screen.py` | Textual screen: tree render, tickboxes, filter, keybindings, commit-popup. Owns **in-memory** staged tick/untick changes until commit. |

**Modified components:**

- `Engine` — owns the monitored-pair lifecycle (existing). Gains two new entry points: `add_pairs_from_selection(records)` and `remove_pairs_from_selection(pair_tickers)`. Each handles the full orchestration that today's `add_games` / `remove_game` (at [engine.py:2839](src/talos/engine.py:2839) and [engine.py:2940](src/talos/engine.py:2940)) already performs — GameManager wiring, adjuster ledger creation/removal, GSR wiring, data_collector logging, and persistence via the existing `save_games_full()` ([persistence.py:84](src/talos/persistence.py:84)). Also gains the new resolver cascade in `_check_exit_only` (manual → milestone → sports GSR → nothing). `_expiration_fallback` call is deleted.
- `GameManager` — loses `scan_events()`, `DEFAULT_NONSPORTS_CATEGORIES`, `_nonsports_max_days`, hardcoded `volume_24h > 0` checks. Engine gains a `_winding_down` set and inventory-aware removal behavior (invoked from Engine entry points, not from direct store events). `GameManager`'s public API is unchanged.
- `GameStatusResolver` — narrowed to sports live/post signals. Scheduling role transferred to `MilestoneResolver`. `estimate_start_time` retained as a library utility.
- `persistence.py` — `save_games_full` / `load_saved_games_full` unchanged in shape. The games_full record schema gains an optional `source` field (observability only; engine treats all persisted entries identically). No new persistence file under `brain/` — all runtime state stays under `get_data_dir()`.
- `automation_config.py` — gains startup/discovery settings; keeps `exit_only_minutes` single value.

**Data flow:**

```
DiscoveryService ──polls──► Kalshi REST API
      │ cached snapshot
      ▼
TreeScreen ─────reads─────► MilestoneResolver
      │                    TreeMetadataStore (for event metadata/overrides)
      │
      │ staged changes held in memory until commit
      │
      │ on commit:
      ▼
Engine.add_pairs_from_selection / Engine.remove_pairs_from_selection
      │
      ├──► GameManager (feeds + scanner)
      ├──► BidAdjuster (ledger)
      ├──► GameStatusResolver (GSR wiring)
      ├──► DataCollector (replay log)
      └──► persistence.save_games_full() ──► games_full.json
                                              (single source of truth for
                                               "what Talos monitors")

Engine._check_exit_only reads:
  TreeMetadataStore.manual_event_start(kalshi_event_ticker)
  MilestoneResolver.event_start(kalshi_event_ticker)
  GameStatusResolver.get(event_ticker)
```

Key invariant: `games_full.json` is the single persistent record of monitored pairs. There is no separate `selections.json`. Committing a tree selection writes to `games_full.json` via the existing persistence path; unticking removes from the same file (after winding-down completes).

### 2. Data model

#### 2.1 Pair persistence — existing `games_full.json` (extended minimally)

Selections at the persistence layer are **ArbPair records**, not events. This matches the actual monitored-pair identity the engine creates today. See §10 for the rationale.

Existing schema (from [persistence.py:84](src/talos/persistence.py:84) + [game_manager.py:459 `restore_game`](src/talos/game_manager.py:459)):

```json
[
  {
    "talos_id": 1,
    "event_ticker": "KXFEDMENTION-26APR-YIEL",
    "ticker_a": "KXFEDMENTION-26APR-YIEL",
    "ticker_b": "KXFEDMENTION-26APR-YIEL",
    "side_a": "yes",
    "side_b": "no",
    "kalshi_event_ticker": "KXFEDMENTION-26APR",
    "series_ticker": "KXFEDMENTION",
    "fee_type": "quadratic_with_maker_fees",
    "fee_rate": 0.0175,
    "close_time": "2026-04-30T14:00:00Z",
    "expected_expiration_time": "2026-04-29T14:00:00Z",
    "sub_title": "On Apr 29, 2026",
    "label": "Powell April press",

    "source": "tree"
  }
]
```

**Schema extensions (minimal):**

- **`source`** (string, optional) — provenance tag. Values: `"tree"` (added via tree commit), `"manual_url"` (added via URL paste/command), `"restore"` (reconstituted at startup), `"migration"` (seeded during Phase 3 flag-flip migration). Engine does **not** branch on this field. It is observability / audit only.

- **`engine_state`** (string, optional) — persisted engine state for restart-safety. Values: `"active"` (default; absent field treated as this), `"winding_down"`, `"exit_only"`. Engine does branch on this during restore — see §5.1b for semantics.

**Implementation requirement for schema durability:** The `ArbPair` Pydantic model gains optional fields `source: str | None = None` and `engine_state: str = "active"`. The legacy persistence writer in [__main__.py:345 `_persist_games`](src/talos/__main__.py:345) is updated to include these in its serialized dict — gated on `if pair.source is not None:` and unconditional for `engine_state` with the `"active"` default — so that **legacy flag-off sessions preserve the fields round-trip** rather than stripping them. Without this update, a `tree_mode=False` session triggered by any `GameManager.on_change` would overwrite `games_full.json` without the new fields, breaking restart durability for wound-down pairs across a flag-flip.

Legacy records without `source` or `engine_state` are read as having `source = null`, `engine_state = "active"`; engine behavior is identical to today. The preserve-round-trip property means that once a record is written by a tree-mode session, flag-off sessions read and re-write it faithfully — Phase 3 dual-run is valid.

**Pair shape recap by event type:**

| Event type | Records in games_full | Shape |
|---|---|---|
| Sports (e.g., NHL game) | 1 per event | `event_ticker == kalshi_event_ticker`, `ticker_a != ticker_b` (cross-NO arb on the two markets) |
| Non-sports, 1 active market | 1 per market | `event_ticker == ticker_a == ticker_b == market_ticker`, `kalshi_event_ticker` different (YES/NO self-arb) |
| Non-sports, N active markets | up to N per event | One record per market; all share the same `kalshi_event_ticker`; each pair has `event_ticker == its_own_market_ticker` |

For `KXFEDMENTION-26APR` with 46 markets where the user ticks 5, five records are persisted — each with the same `kalshi_event_ticker` but different market-level `event_ticker`s.

#### 2.2 Event-level metadata — new `tree_metadata.json` under `get_data_dir()`

Event-level tracking and overrides that don't belong on a per-pair record. Keyed by `kalshi_event_ticker` (not pair ticker) because these are decisions about the underlying event, not the arbitrage instrument.

```json
{
  "version": 1,
  "updated_at": "2026-04-16T19:42:11Z",

  "event_first_seen": {
    "KXTRUMPMENTION-26APR18": "2026-04-16T18:32:00Z"
  },
  "event_reviewed_at": {
    "KXTRUMPMENTION-26APR15": "2026-04-13T09:32:11Z"
  },

  "manual_event_start": {
    "KXSURVIVORMENTION-26APR23": "2026-04-22T20:00:00-04:00",
    "KXSNLMENTION-26APR25": "none"
  },

  "deliberately_unticked": [
    "KXTRUMPMENTION-26APR17"
  ],

  "deliberately_unticked_pending": [
    "KXFEDMENTION-26APR"
  ]
}
```

`manual_event_start` values:
- ISO 8601 datetime — explicit event-start.
- `"none"` — explicit user opt-out from exit-only for this event.
- Missing key — no manual override; resolver cascade consults milestone/GSR.

`deliberately_unticked` is the **applied** set that renders as `[·]` in the tree (as opposed to `[ ]` for never-ticked). Events drop out of this set only when Kalshi changes their status to closed/finalized.

`deliberately_unticked_pending` is the **deferred** set — events the user unticked but whose `[·]` has not yet applied because some pairs are still winding down. Survives restart; entries promote into `deliberately_unticked` when the engine emits `event_fully_removed` (either mid-session or shortly after restart as ledgers clear). See §5.1b.

**Rationale for a sidecar file** (rather than folding into games_full.json): these entries describe events the user may not be actively monitoring. `manual_event_start` for KXSURVIVORMENTION-26APR23 should persist even after the user unticks it, so re-ticking later doesn't lose the override. `deliberately_unticked` entries have no corresponding games_full record by definition.

#### 2.3 Tree UI settings — new `tree` sub-object under existing `settings.json`

Settings live in the same `settings.json` ([persistence.py:50](src/talos/persistence.py:50)) as other UI prefs. A new sub-object isolates tree-specific keys:

```json
{
  "...existing_keys": "...",
  "tree": {
    "excluded_categories": ["Sports", "Entertainment"],
    "min_volume_24h": 100,
    "min_open_interest": 0,
    "max_spread_cents": 99,
    "hide_events_past_close": true,
    "ui_state": {
      "expanded_categories": ["Politics", "Companies"],
      "expanded_series": ["KXEARNINGSMENTION", "KXTRUMPMENTION"],
      "last_refresh": "2026-04-16T19:42:11Z"
    }
  }
}
```

Missing `tree` sub-object → defaults. Legacy settings.json files load identically.

Filter settings (`excluded_categories`, `min_volume_24h`, etc.) affect **tree rendering only**. They do not auto-remove entries from `games_full.json`. A selected pair whose market falls below `min_volume_24h` keeps being monitored; the filter just hides it from the tree view.

#### 2.4 ArbPair record construction from discovery cache

The tree commit path materializes full `ArbPair` records from the in-memory discovery cache — **no REST calls on the commit hot-path**. Each record is built from:

| Field | Source |
|---|---|
| `event_ticker` | market ticker (non-sports) or Kalshi event ticker (sports) |
| `ticker_a`, `ticker_b` | market tickers from `EventNode.markets` + sports/non-sports shape rule |
| `side_a`, `side_b` | `"yes"/"no"` for non-sports YES/NO; both `"no"` for sports cross-NO |
| `kalshi_event_ticker` | `EventNode.ticker` |
| `series_ticker` | `SeriesNode.ticker` |
| `fee_type` | `SeriesNode.fee_type` (hydrated at bootstrap, §3.1) |
| `fee_rate` | `maker_fee_rate(SeriesNode.fee_type, SeriesNode.fee_multiplier)` — pure function applied at construction |
| `close_time` | `EventNode.close_time` or `MarketNode.close_time` |
| `expected_expiration_time` | same source |
| `sub_title`, `label` | `EventNode.sub_title` / derived via existing `extract_leg_labels()` (pure function over sub_title) |
| `source` | `"tree"` |
| `volume_24h_a` (new field on record) | `MarketNode.volume_24h` for the matching ticker_a market |
| `volume_24h_b` (new field on record) | `MarketNode.volume_24h` for the matching ticker_b market (same as ticker_a for non-sports YES/NO) |

**Why volumes are on the record, not fetched later:** today's non-tree paths ([game_manager.py:366](src/talos/game_manager.py:366) and [game_manager.py:441](src/talos/game_manager.py:441)) populate `GameManager._volumes_24h` during add. `restore_game()` does NOT populate it ([game_manager.py:459](src/talos/game_manager.py:459)) — startup reconciliation fills volumes from persisted `volume_a`/`volume_b` fields in `games_full.json` via [engine.py:921](src/talos/engine.py:921). The tree commit path uses `restore_game`, so without explicit seeding the new pairs would have zero volume in UI tables, zero in `data_collector.log_game_add` payloads, and no persisted `volume_a`/`volume_b` for the next restart.

Engine's `add_pairs_from_selection` therefore seeds `GameManager._volumes_24h` from the record before any downstream consumer reads it (see §5.1 step 1.5). The `_persist_games` writer then reads those volumes back as usual — one volume-origin path for both live-add and commit-path, no divergence.

**Property: commit is a pure local operation.** If any required field is missing from the cache (which can happen if the user commits immediately after a discovery-fetch error left a SeriesNode stale), the commit validator surfaces this as a blocker — "cannot build pair record for KXFOO-26APR: fee metadata unavailable, refresh discovery (`r`) and retry." The user-facing escape hatch is a manual refresh, not a silent background REST.

This matters for the SURVIVOR replay test and for offline resilience: commits cannot block on Kalshi's availability at commit time.

#### 2.5 Staged (uncommitted) tree edits — in-memory only

TreeScreen holds staged tick/untick changes in process memory (not persisted). Structure:

```python
class StagedChanges:
    to_add: list[ArbPairRecord]      # pair records to pass to Engine.add_pairs_from_selection
    to_remove: list[str]             # pair tickers to remove
    to_set_unticked: list[str]       # event tickers to mark deliberately_unticked
    to_clear_unticked: list[str]     # event tickers to clear from deliberately_unticked
    to_set_manual_start: dict[str, str]   # kalshi_event_ticker -> ISO datetime or "none"
```

Cleared on commit success. Preserved across tree screen push/pop within the same session. Lost on process exit — any unfinalized edits must be re-done after restart. Footer shows `* N changes pending` when non-empty.

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
    # Fee metadata — populated from the /series bootstrap response.
    # Required at commit time to construct ArbPair records without additional REST.
    fee_type: str                            # e.g., "quadratic_with_maker_fees"
    fee_multiplier: float                    # raw multiplier; maker_fee_rate() applied at pair build
    events: dict[str, "EventNode"] | None    # None = not fetched yet
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

1. `GET /series` — returns all ~9,700 series in one response (~11 MB). Each response object carries `ticker, title, category, tags, frequency, fee_type, fee_multiplier`.
2. Group by category. Build `CategoryNode` tree. Each `SeriesNode` is populated with **all** series-level metadata including `fee_type` and `fee_multiplier` — not a stub.
3. Parallel: paginate `GET /milestones?minimum_start_date=<now>&limit=200` until cursor exhausted. Build `MilestoneIndex` by `related_event_ticker`.
4. Emit `discovery_ready`.

Cost: ~16 API calls, ~3 seconds elapsed.

**Fee metadata is fully hydrated at bootstrap.** This is required so that tree commits can construct `ArbPair` records without any additional REST calls. The `/series` endpoint already returns what we need — we just weren't exposing it.

Events are not fetched at startup. Series nodes are stubs on their `events` field only (the `events: dict | None = None` signals lazy-load); all other series-level fields are populated.

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
- Fields persist in `tree_metadata.json` (see §2.2).

#### 4.6 Commit flow

Selections are **staged** until commit (`c`). Reasons:

- Ticking a series that expands to 10 events, then unticking 2 before commit, produces one clean batch — not 10 subscribes + 2 unsubscribes.
- Inventory-impacting unticks get a single consolidated confirmation.
- Commit validator can inspect all staged changes at once.

Uncommitted state shows in footer: `* 3 changes pending`. `escape` with uncommitted changes prompts confirmation.

#### 4.7 Commit-time schedule validator

On commit, before invoking `Engine.add_pairs_from_selection()`:

1. For each staged pair addition, extract its `kalshi_event_ticker` and resolve via the cascade (manual override in TreeMetadataStore → milestone in MilestoneResolver → sports GSR).
2. If no source returns a value AND the user has not set `manual_event_start: "none"`, flag as needing-schedule. Deduplicate by `kalshi_event_ticker` so a 46-market Fed event is prompted for once, not 46 times.
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

4. On `save all & commit`: collected times are staged into `staged.to_set_manual_start`; commit flow continues as in §5.1 (manual overrides persist to TreeMetadataStore, then `Engine.add_pairs_from_selection()` fires).
5. On `cancel commit`: staged selections preserved in tree. No overrides persisted. No engine mutations. User returns to tree.
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

#### 5.1 Commit flow — TreeScreen ↔ Engine

Engine owns the monitored-pair lifecycle. TreeScreen commits push staged changes through two new Engine entry points, which orchestrate the same add/remove steps that today's `add_games` ([engine.py:2839](src/talos/engine.py:2839)) and `remove_game` ([engine.py:2940](src/talos/engine.py:2940)) already perform.

```python
# TreeScreen.on_commit():
async def on_commit(self):
    staged = self._staged_changes
    try:
        # 1. Validator — ensures all additions have a schedule source or manual override
        needs_schedule = self._validate_schedules(staged.to_add)
        if needs_schedule:
            # Popup: user fills manual_event_start for each. Cancel aborts commit.
            entries = await self._show_schedule_popup(needs_schedule)
            if entries is None:
                return   # user cancelled; staged preserved
            staged.to_set_manual_start.update(entries)

        # 2. Write manual_event_start BEFORE engine ops. Rationale: this is a
        #    user-intent override; it is correct to have it on disk regardless
        #    of whether the corresponding engine add/remove succeeds. If add
        #    fails and is retried later, the override is already present.
        if staged.to_set_manual_start:
            self._tree_metadata_store.apply(
                manual_event_start=staged.to_set_manual_start,
            )

        # 3. Engine add/remove (engine handles full wiring + persistence)
        added: list[ArbPair] = []
        remove_outcomes: list[RemoveOutcome] = []
        if staged.to_add:
            added = await self._engine.add_pairs_from_selection(staged.to_add)
        if staged.to_remove:
            remove_outcomes = await self._engine.remove_pairs_from_selection(
                staged.to_remove
            )

        # 4. Apply untick-state mutations per §5.1a reconciliation rules.
        #    - clear_unticked: applied when ALL staged pairs for the event were
        #      successfully added.
        #    - set_unticked: applied when ALL staged pairs for the event came
        #      back as "removed" (none winding_down, none failed).
        added_keys = {p.kalshi_event_ticker or p.event_ticker for p in added}
        staged_remove_set = set(staged.to_remove)
        applied_set_unticked = [
            k for k in staged.to_set_unticked
            if _should_set_unticked(k, remove_outcomes, staged_remove_set)
        ]
        applied_clear_unticked = [
            k for k in staged.to_clear_unticked if k in added_keys
        ]
        if applied_set_unticked or applied_clear_unticked:
            self._tree_metadata_store.apply(
                set_unticked=applied_set_unticked,
                clear_unticked=applied_clear_unticked,
            )

        # 5. Reconcile staged state.
        #
        # Winding-down pairs are NOT kept in staged.to_remove — the engine
        # owns their lifecycle via its _winding_down set. When a winding
        # pair goes flat the engine emits winding_down_completed(kalshi_et),
        # and the tree applies any deferred [·] at that point (see §5.1b).
        #
        # Failed pairs are kept in staged.to_remove for an explicit retry.
        #
        # Unapplied untick flags stay in staged until the corresponding
        # engine events confirm they should fire.
        unapplied_set = set(staged.to_set_unticked) - set(applied_set_unticked)
        unapplied_clear = set(staged.to_clear_unticked) - set(applied_clear_unticked)
        failed_removes = [o.pair_ticker for o in remove_outcomes
                          if o.status == "failed"]
        if unapplied_set or unapplied_clear or failed_removes:
            self._staged_changes = StagedChanges(
                to_set_unticked=list(unapplied_set),
                to_clear_unticked=list(unapplied_clear),
                to_remove=failed_removes,
            )
            # Deferred [·] markers tracked separately from staged changes
            # so they don't appear as "pending edits" in the footer.
            for k in unapplied_set:
                if _any_pair_for_event_winding(k, remove_outcomes):
                    self._deferred_set_unticked.add(k)
            logger.info("tree_commit_partial",
                        unapplied_set=list(unapplied_set),
                        deferred_for_winding=list(self._deferred_set_unticked),
                        failed_removes=failed_removes)
        else:
            self._staged_changes = StagedChanges.empty()
    except Exception:
        logger.exception("tree_commit_failed")
        # Staged changes preserved for retry
```

**Ordering invariant:** metadata writes that represent *pure user intent* (manual_event_start) persist before engine ops; metadata writes that *describe the monitored state* (deliberately_unticked set/clear) persist only after the corresponding engine mutation succeeds. This prevents partial-failure desync where `tree_metadata.json` says an event is deliberately unticked while the engine is still actively monitoring it, or vice versa.

`Engine.add_pairs_from_selection(records)` mirrors today's `add_games` orchestration exactly (see [engine.py:2839](src/talos/engine.py:2839)). Every step of today's URL-add path is preserved, including the critical `resolve_batch()` call that today populates GSR state for newly-added sports pairs:

**On batch atomicity and the `on_change` callback.** Today's `GameManager.restore_game()` fires `self.on_change()` on every pair add ([game_manager.py:528](src/talos/game_manager.py:528)), and `__main__.py` wires `on_change` to `save_games_full()` ([__main__.py:345](src/talos/__main__.py:345)). If the commit path called `restore_game` in a loop without suppressing this, `games_full.json` would be rewritten N times during an N-pair batch, with partial batch states visible to any concurrent reader (and to a crash mid-batch). The spec requires batch-final persistence; the implementation must suppress the per-pair callback during batch commits.

Proposed mechanism: add a small context manager on GameManager that pauses `on_change` emission. The Engine batch paths wrap the restore loop in it; persistence is called exactly once at batch end. Non-batch callers (URL-add via `Engine.add_games`, manual clear-all, etc.) are unaffected — they keep firing on_change per-pair as today.

```python
async def add_pairs_from_selection(
    self, records: list[ArbPairRecord]
) -> list[ArbPair]:
    """Commit path for tree-selected pairs. Full engine wiring + persistence.

    Mirrors Engine.add_games (engine.py:2839) step-for-step so sports pairs
    get initial GSR resolution immediately, not on the next periodic refresh.
    """
    pairs: list[ArbPair] = []

    # Step 1: reconstitute each pair through the existing restore pathway.
    # suppress_on_change() is a new GameManager context manager that pauses
    # the on_change callback — otherwise restore_game would fire
    # save_games_full() per pair (game_manager.py:528 -> __main__.py:345)
    # and break batch atomicity.
    with self._game_manager.suppress_on_change():
        for r in records:
            try:
                pair = self._game_manager.restore_game({**r, "source": "tree"})
                if pair is None:
                    continue
                # Step 1.5: seed 24h volume on GameManager so data_collector
                # logging, UI renders, and the next _persist_games write all
                # have accurate volume data. restore_game() does NOT do this
                # by itself (game_manager.py:459) — only the live-add paths
                # at game_manager.py:366 and :441 do. Without this seeding,
                # tree-added pairs would log/render volume=0 and would
                # persist without volume_a/volume_b fields, degrading
                # next-restart cache quality.
                vol_a = r.get("volume_24h_a")
                vol_b = r.get("volume_24h_b")
                if vol_a is not None:
                    self._game_manager._volumes_24h[pair.ticker_a] = int(vol_a)
                if vol_b is not None and pair.ticker_b != pair.ticker_a:
                    self._game_manager._volumes_24h[pair.ticker_b] = int(vol_b)
                pairs.append(pair)
            except Exception:
                logger.warning("tree_add_failed",
                               pair_ticker=r.get("event_ticker"), exc_info=True)

    # Step 2: wire adjuster ledgers
    for pair in pairs:
        self._adjuster.add_event(pair)

    # Step 3: wire GameStatusResolver — set_expiration THEN resolve_batch.
    # resolve_batch() is non-optional. Without it, the sports branch of the
    # exit-only cascade returns None (no scheduled_start, no live/post state)
    # until the next periodic GSR refresh, which can be up to an hour later.
    if self._game_status_resolver is not None and pairs:
        for pair in pairs:
            self._game_status_resolver.set_expiration(
                pair.event_ticker, pair.expected_expiration_time
            )
        batch = [
            (p.event_ticker,
             self._game_manager.subtitles.get(p.event_ticker, ""))
            for p in pairs
        ]
        await self._game_status_resolver.resolve_batch(batch)

    # Step 4: feed subscriptions
    for pair in pairs:
        await self._feed.subscribe(pair.ticker_a)
        if pair.ticker_b != pair.ticker_a:
            await self._feed.subscribe(pair.ticker_b)

    # Step 5: data_collector logging — now that GSR has scheduled_start, log it
    if self._data_collector is not None:
        for pair in pairs:
            gs = (self._game_status_resolver.get(pair.event_ticker)
                  if self._game_status_resolver else None)
            self._data_collector.log_game_add(
                event_ticker=pair.event_ticker,
                series_ticker=pair.series_ticker,
                source="tree",
                ticker_a=pair.ticker_a,
                ticker_b=pair.ticker_b,
                volume_a=self._game_manager.volumes_24h.get(pair.ticker_a, 0),
                volume_b=self._game_manager.volumes_24h.get(pair.ticker_b, 0),
                fee_type=pair.fee_type,
                fee_rate=pair.fee_rate,
                scheduled_start=(gs.scheduled_start.isoformat()
                                 if gs and gs.scheduled_start else None),
            )

    # Step 6: persist games_full after the full batch succeeds
    self._persist_active_games()
    return pairs
```

**Correspondence with today's code** ([engine.py:2839-2884](src/talos/engine.py:2839)):

| Today's step | Spec step | Preserved? |
|---|---|---|
| `game_manager.add_games(urls)` | Step 1 `restore_game` (non-REST path) | ✓ |
| `adjuster.add_event(pair)` loop | Step 2 | ✓ |
| `set_expiration` loop + `resolve_batch(batch)` | Step 3 | ✓ |
| `feed.subscribe(ticker)` per leg | Step 4 | ✓ (today's `add_games` defers to the later bulk subscribe; we inline for clarity) |
| `data_collector.log_game_add(...)` | Step 5 | ✓ |
| (new) persistence | Step 6 | (new behavior; today's path doesn't re-persist on every add) |

The only **semantic** change from today's flow is Step 6 — explicit persistence after each batch. Today, persistence happens at shutdown/session-save time; with tree-driven commits being the authoritative source, we persist immediately so a crash between commit and shutdown doesn't lose state.

`Engine.remove_pairs_from_selection(pair_tickers)` returns a structured outcome per pair so the commit reconciliation logic can decide untick metadata correctly for events with many market-pairs:

```python
class RemoveOutcome(BaseModel):
    pair_ticker: str
    kalshi_event_ticker: str         # grouping key for event-level decisions
    status: Literal["removed", "winding_down", "not_found", "failed"]
    reason: str | None = None        # e.g., "inventory filled=5,3" for winding_down

async def remove_pairs_from_selection(
    self, pair_tickers: list[str]
) -> list[RemoveOutcome]:
    """Commit path for tree-unticked pairs.

    Returns a structured per-pair outcome so the caller can decide per-event
    whether to mark deliberately_unticked, leave it winding, or retry later.

    Wraps the whole batch in suppress_on_change() for the same reason as
    add_pairs_from_selection — both GameManager.remove_game (line 595) and
    add_market_as_pair (line 450) fire on_change which today triggers
    save_games_full. One final persist at batch end, not N.
    """
    outcomes: list[RemoveOutcome] = []
    with self._game_manager.suppress_on_change():
      for pt in pair_tickers:
        pair = self._game_manager.get_game(pt)
        if pair is None:
            outcomes.append(RemoveOutcome(
                pair_ticker=pt,
                kalshi_event_ticker="",
                status="not_found",
            ))
            continue
        kalshi_et = pair.kalshi_event_ticker or pair.event_ticker

        try:
            # Inventory check — invariant #2 from §1
            ledger = self._adjuster.get_ledger(pt)
            if ledger and (ledger.has_filled_positions() or ledger.has_resting_orders()):
                self._winding_down.add(pt)
                await self.enforce_exit_only(pt)
                # Persist the winding_down state so it survives a restart
                self._mark_engine_state(pt, "winding_down")
                reason = (f"filled={ledger.filled_count(Side.A)},"
                          f"{ledger.filled_count(Side.B)} "
                          f"resting={ledger.resting_count(Side.A)},"
                          f"{ledger.resting_count(Side.B)}")
                logger.info("winding_down_started",
                            pair_ticker=pt, reason=reason)
                outcomes.append(RemoveOutcome(
                    pair_ticker=pt,
                    kalshi_event_ticker=kalshi_et,
                    status="winding_down",
                    reason=reason,
                ))
                continue

            # Clean removal: reverse of add_pairs_from_selection
            self._exit_only_events.discard(pt)
            self._stale_candidates.discard(pt)
            if self._game_status_resolver is not None:
                self._game_status_resolver.remove(pt)
            self._adjuster.remove_event(pt)
            await self._game_manager.remove_game(pt)
            outcomes.append(RemoveOutcome(
                pair_ticker=pt,
                kalshi_event_ticker=kalshi_et,
                status="removed",
            ))
        except Exception as e:
            logger.warning("tree_remove_failed",
                           pair_ticker=pt, exc_info=True)
            outcomes.append(RemoveOutcome(
                pair_ticker=pt,
                kalshi_event_ticker=kalshi_et,
                status="failed",
                reason=str(e),
            ))

    self._persist_active_games()
    return outcomes
```

**Same pattern for when winding-down completes later.** When a pair's ledger clears during a runtime tick, the engine internally calls `remove_pairs_from_selection([pt])` with that single ticker. The outcome returns `"removed"`, and the engine's background reconciliation can flip the pair's tree rendering state from `[W]` to gone.

#### 5.1a Commit reconciliation with multi-market events

For a Kalshi event with N market-pairs (Fed presser = up to 46), untick-at-event-level means the tree fans out to N removal records. Each can independently land in any of four states (`removed`, `winding_down`, `not_found`, `failed`). The `deliberately_unticked` flag at event level should only be set when the user's **entire event-level untick intent** was fully honored — i.e., **every** pair of that `kalshi_event_ticker` that was in the batch came back as `removed`.

Rule for applying `to_set_unticked[kalshi_event_ticker]`:

```python
def _should_set_unticked(
    kalshi_event_ticker: str,
    outcomes: list[RemoveOutcome],
    staged_pair_tickers: set[str],
) -> bool:
    """Apply [·] at event level only when every staged pair for this event
    was cleanly removed — none winding-down, none failed, none missing."""
    event_outcomes = [o for o in outcomes
                      if o.kalshi_event_ticker == kalshi_event_ticker
                      and o.pair_ticker in staged_pair_tickers]
    if not event_outcomes:
        return False  # no staged pairs for this event made it to engine
    return all(o.status == "removed" for o in event_outcomes)
```

What the tree renders per case:

| All staged pairs for event | Tree state |
|---|---|
| All `removed` | `[·]` event-level |
| Mix of `removed` + `winding_down` | event shows `[-]` partial, remaining market-pairs render `[W]`. No `[·]` at event level. |
| All `winding_down` | event shows `[W]` event-level (propagated). No `[·]` at event level. |
| Any `failed` / `not_found` | Retained in staged_changes for retry. Tree shows `* changes pending`. |

When winding-down pairs later complete (ledger clears), the engine's internal single-pair removal emits `winding_down_completed`, and tree rendering updates: if the remaining pairs of the event all come back `removed`, the tree can promote the event to `[·]`. This promotion is driven by the engine's background reconciliation, not by an explicit user commit.

#### 5.1b Deferred untick application via engine events

When an event-level untick commit produces a mix of `removed` and `winding_down` outcomes, the `[·]` flag cannot be applied yet — the event is still being actively monitored until the winding-down pairs go flat. The tree holds these in a `_deferred_set_unticked: set[str]` (keyed by `kalshi_event_ticker`), separate from `staged.to_set_unticked` so they don't render as "pending edits" to the user.

The engine emits two events the tree subscribes to:

- **`winding_down_completed(pair_ticker, kalshi_event_ticker)`** — fires when a single winding-down pair's ledger clears and it's cleanly removed.
- **`event_fully_removed(kalshi_event_ticker)`** — fires when the last active pair for a Kalshi event is removed (either via commit or via winding-down completion).

Tree handler for `event_fully_removed`:

```python
async def _on_event_fully_removed(self, kalshi_event_ticker: str) -> None:
    if kalshi_event_ticker in self._deferred_set_unticked:
        self._tree_metadata_store.apply(
            set_unticked=[kalshi_event_ticker],
        )
        self._deferred_set_unticked.discard(kalshi_event_ticker)
        logger.info("deferred_unticked_applied",
                    kalshi_event_ticker=kalshi_event_ticker)
    # Tree row for the event now renders [·] event-level on next refresh
```

**Symmetry for manual re-tick during winding:** If the user re-ticks a `winding_down` pair before it clears, the engine cancels the winding state (exits exit-only, re-engages trading — with confirmation, per §4.2), and emits `winding_down_cancelled(pair_ticker, kalshi_event_ticker)`. The tree handler removes the event from `_deferred_set_unticked` so the `[·]` is not later applied to an event the user changed their mind about.

**Restart durability — both winding state and deferred-untick intent survive process restart.** This is a safety-critical property: a pair the user has unticked must not silently resume normal trading after a crash or restart.

Two additions to persistence:

1. **Per-pair `engine_state` field in `games_full.json`**

   Each pair record gains an optional field:

   ```json
   {
     "event_ticker": "KXFEDMENTION-26APR-YIEL",
     ... existing fields ...,
     "engine_state": "winding_down"
   }
   ```

   Values: `"active"` (default; absent field treated as this), `"winding_down"` (user unticked but inventory remains), `"exit_only"` (resolver-triggered exit-only but not user-unticked).

   Written whenever a pair enters or exits these states. On startup, the restore loop applies per-state behavior:

   | Loaded state | Engine restore action |
   |---|---|
   | `"active"` or missing | Normal restore — subscribe feeds, register with scanner/adjuster. |
   | `"winding_down"` | Normal restore **plus** re-add to `_winding_down` set **plus** immediately flip `_exit_only_events`. Pair will not accept new bids; continues to wind down from the state it held pre-restart. |
   | `"exit_only"` | Normal restore plus immediately flip `_exit_only_events`. Resolver cascade may clear it on next tick if the event has passed. |

2. **`deliberately_unticked_pending` set in `tree_metadata.json`**

   ```json
   {
     ...,
     "deliberately_unticked_pending": ["KXFEDMENTION-26APR"]
   }
   ```

   The persisted mirror of `_deferred_set_unticked`. Populated when a commit produces mixed removed/winding outcomes and the `[·]` is deferred pending wind-down completion. On startup, `TreeMetadataStore` loads this into `_deferred_set_unticked`. When `event_fully_removed` fires (either mid-session or immediately post-restart as ledgers clear), the tree applies `[·]` and clears the event from both the in-memory set and the persisted list.

**Why this is non-optional:** without these, the SURVIVOR-class failure mode trivially reappears. Imagine you untick KXSURVIVORMENTION-26APR23 at 7:58 PM with 30 contracts filled, Talos enters winding-down + exit-only, then crashes at 7:59 PM. Without persistence, the 8:00 PM restart: pair is restored from `games_full.json` as a normal active pair, no exit-only trigger fires (uncurated event, no milestone, no manual-override-that-overrides-winding-state), and Talos resumes bidding during the live broadcast. The persistence above blocks that path structurally.

**What is still process-local:** the `_exit_only_events` set for resolver-triggered exit-only (milestone lead-time crossed) does NOT need persistence — the resolver cascade re-derives it on the first post-startup tick. Only **user-intent-driven** exit-only states (winding from untick, resolver-triggered combined with pending [·]) need persistence, which is what the `engine_state` field captures.

`_persist_active_games()` is a small helper that calls `save_games_full(records_from_current_active_pairs)` — reuses the existing persistence function. Persistence happens **exactly once** at the end of each add/remove batch.

**Mechanism — `GameManager.suppress_on_change()`:**

```python
@contextmanager
def suppress_on_change(self):
    """Pause on_change emission within a batch. Engine batches call this
    to prevent per-pair save_games_full writes; single final persist
    happens in _persist_active_games() at batch end."""
    prev = self.on_change
    self.on_change = None
    try:
        yield
    finally:
        self.on_change = prev
```

Inside the `with` block, all GameManager mutations (`restore_game`, `remove_game`, `add_market_as_pair`) skip their `on_change()` call. Non-batch call-sites — URL-paste adds via `Engine.add_games(urls)`, user-initiated `clear_all_games`, etc. — are unaffected; they continue to fire `on_change` per-pair as today, preserving existing behavior for those paths.

**Why not unwire `on_change` → `save_games_full` entirely?** Because that callback is a general-purpose "game set changed" signal, not a persistence-specific hook. Existing subscribers rely on it for UI re-renders; the [__main__.py:345](src/talos/__main__.py:345) wiring to `save_games_full` is one of potentially several subscribers. Suppressing the callback during batches keeps the contract simple: per-pair events still fire for full-runtime callers that depend on them; batch callers explicitly opt out.

#### 5.2 Engine._check_exit_only cascade

The cascade resolves per **Kalshi event ticker**, not per pair. For non-sports multi-market events, this means all pairs sharing a `kalshi_event_ticker` resolve to the same event-start time (one decision per underlying event, applied to all its market-pairs).

```python
def _check_exit_only(self):
    now = datetime.now(UTC)
    seen_events: set[str] = set()      # dedupe — one decision per kalshi_event_ticker
    for pair in self._scanner.pairs:
        # The "key" for scheduling is the underlying Kalshi event, not the pair
        key = pair.kalshi_event_ticker or pair.event_ticker
        if key in seen_events:
            continue
        seen_events.add(key)

        if pair.event_ticker in self._exit_only_events:
            continue

        start_time, source = self._resolve_event_start(key, pair)

        if source == "manual_opt_out":
            continue

        if source is None:
            self._log_once("exit_only_no_schedule", event=key)
            continue

        # Sports GSR supplies live/post state transitions
        if source == "sports_gsr":
            gs = self._game_status_resolver.get(key)
            if gs and gs.state in ("live", "post"):
                self._flip_exit_only_for_key(key, reason=f"sports_{gs.state}")
                continue

        # Preemptive lead-time trigger
        lead_min = self._auto_config.exit_only_minutes
        if (start_time - now).total_seconds() < lead_min * 60:
            self._flip_exit_only_for_key(key, reason=source, scheduled_start=start_time)

def _resolve_event_start(self, kalshi_event_ticker: str, pair: ArbPair
                         ) -> tuple[datetime | None, str | None]:
    # 1. Manual override (user owns this) — keyed by Kalshi event ticker
    manual = self._tree_metadata_store.manual_event_start(kalshi_event_ticker)
    if manual == "none":
        return (None, "manual_opt_out")
    if manual is not None:
        return (manual, "manual")

    # 2. Kalshi milestone — keyed by Kalshi event ticker
    ms = self._milestone_resolver.event_start(kalshi_event_ticker)
    if ms is not None:
        return (ms, "milestone")

    # 3. Sports GSR — keyed by event ticker (sports pairs: event_ticker == kalshi_event_ticker)
    gs = self._game_status_resolver.get(pair.event_ticker)
    if gs and gs.scheduled_start:
        return (gs.scheduled_start, "sports_gsr")

    # 4. Nothing
    return (None, None)
```

`_flip_exit_only_for_key(key, ...)` flips all pairs whose `kalshi_event_ticker == key` into exit-only simultaneously. Ensures the 46 market-pairs of a Fed presser all gate together, not one at a time.

`exit_only_minutes` stays as a single global setting (default 30.0). Applied uniformly across resolver sources.

#### 5.3 Startup sequence — safety-first gate

Talos does not have a single engine-owned tick loop. The runtime is a mix of: `TradingEngine.start_feed()` at [engine.py:746](src/talos/engine.py:746) (WebSocket + `_setup_initial_games`) and `TalosApp.on_mount()` at [ui/app.py:125](src/talos/ui/app.py:125) (~11 polling timers). The gate must be grounded in these concrete owners.

**What the gate blocks:**

The `Engine` exposes a single awaitable `ready_for_trading: asyncio.Event`. The following paths await it before proceeding:

| Path | Owner | Why gated |
|---|---|---|
| `_setup_initial_games()` restoration loop | `TradingEngine.start_feed` | Adds pairs to engine; must happen after TreeMetadataStore + MilestoneResolver are armed. |
| `_check_exit_only` callback | Engine refresh cycle | Resolver cascade would fall through on missing milestones; safety principle says wait. |
| `_auto_accept_tick` | TalosApp timer ([ui/app.py:135](src/talos/ui/app.py:135)) | Could accept a proposal against an event whose exit-only should be live but isn't. |
| Proposer/adjuster reactions to WS ticker updates | `_start_reaction_consumer` ([engine.py:760](src/talos/engine.py:760)) | Ticker-driven proposal generation — same reasoning as `_auto_accept_tick`. |

**What the gate does NOT block** (read-only / observational):

- `_poll_balance`, `_poll_account`, `_poll_queue`, `_poll_trades`, `_poll_settlements` — balance/account polling for display.
- `_log_market_snapshots` — replay logging.
- `_refresh_proposals` — UI-side refresh of already-generated proposals.
- Tree browsing itself — user can navigate the tree during the gate window; commits will queue and apply after the gate opens.
- WebSocket `connect()` + listen-loop start. Feeds can be subscribed once pairs are added; the gate just delays when pairs get added.

**Timeline:**

```
t=0.0   Process starts
t=0.1   settings.json + tree_metadata.json loaded synchronously
        TreeMetadataStore armed with:
          - manual_event_start overrides
          - deliberately_unticked_pending set (loaded into _deferred_set_unticked)
t=0.2   DiscoveryService + MilestoneResolver start (background task)
t=0.2   TalosApp.on_mount schedules polling timers (they fire, but gated
        callbacks await ready_for_trading before proceeding)
t=0.2   TradingEngine.start_feed() begins — WS connects, listen loop starts
t=0.3   _setup_initial_games() AWAITS ready_for_trading before restoration
t=2-5   Milestones fully loaded → ready_for_trading.set()
t=2-5   _setup_initial_games() proceeds: loads games_full.json, reconstitutes
        pairs through Engine entry points, subscribes feeds. For each pair:
          - engine_state == "active" (or missing) → normal restore
          - engine_state == "winding_down" → restore + _winding_down.add(pt)
                                           + _exit_only_events.add(pt)
          - engine_state == "exit_only"    → restore + _exit_only_events.add(pt)
t=5+    All gated callbacks resume; trading fully armed.
        Any winding-down pairs immediately see exit-only logic applied —
        no tick window where they could take new bids.
t=30    Hard cap: if milestones still not loaded, ready_for_trading.set()
        fires anyway with a structured warning (exit_only_degraded=True).
        Red banner in UI: "started without milestones — exit-only scheduling
        may be degraded." Manual overrides still work (they loaded at t=0.1).
        Persisted winding_down / exit_only states still work (they loaded
        from games_full.json).
```

**Rationale** for Option B over Option A (proceed immediately): Principle "Safety over speed" — a ~5-second startup delay is recoverable; running `_check_exit_only` or `_auto_accept_tick` without an armed resolver cascade is not. See `brain/principles.md`.

**Critical ordering:** TreeMetadataStore loads synchronously at t=0.1, before any gated callback can proceed. This ensures that when the first post-gate `_check_exit_only` tick runs, `manual_event_start` overrides are visible — so a restart 2 minutes before KXSURVIVORMENTION's 8 PM airtime correctly fires exit-only rather than falling through to milestone (which Kalshi doesn't curate) and then to sports GSR (which doesn't match) and finally to "no schedule."

#### 5.4 Deletions

| Code | Fate |
|---|---|
| `GameManager.scan_events()` ([game_manager.py:657](src/talos/game_manager.py:657)) | Deleted in Phase 5. |
| `DEFAULT_NONSPORTS_CATEGORIES` constant | Deleted. Tree filters replace. |
| `_nonsports_max_days` | Deleted. No close-time window gate. |
| `volume_24h > 0` hardcodes ([game_manager.py:559](src/talos/game_manager.py:559) and [game_manager.py:694](src/talos/game_manager.py:694)) | Deleted. `min_volume_24h` tree setting applies at discovery (rendering filter). Engine trusts records from games_full.json. |
| `SPORTS_SERIES` list | Retained, but narrowed role — sports live/post resolution only, not discovery. |
| `_expiration_fallback` path in `GameStatusResolver` | Deleted. `estimate_start_time` retained as library utility for possible future use. |
| Engine's scheduled call to `scan_events()` | Deleted from refresh loop. |
| Engine's current `add_games(urls, source="scan")` auto-scan callers | Retained (manual URL add-by-paste), deleted from scheduler. |

### 6. Settings inventory

Four surfaces, clean ownership. No new files under `brain/`.

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

#### 6.2 `games_full.json` (runtime persistence, `get_data_dir()`)

**Existing file, unchanged in shape.** Gains optional `source` field (see §2.1). Canonical "what Talos monitors" record. Written whenever the active pair set changes (add/remove batch).

#### 6.3 `tree_metadata.json` (NEW, `get_data_dir()`)

Event-level tracking and overrides. See §2.2. Change cadence: per-commit for `manual_event_start`, per-expand for `event_reviewed_at`, occasional for `deliberately_unticked`.

#### 6.4 `settings.json` `tree` sub-object (existing file, extended)

Tree UI filter prefs. See §2.3. Change cadence: occasional (user edits filters).

#### 6.5 `brain/principles.md` addition

New principle to be landed alongside Phase 1 scaffold. Working text (to be refined at write time):

> **Principle N: Safety over speed.** When trading and scheduling decisions are time-sensitive, prefer delay or pause over proceeding on incomplete data. A five-second delayed decision is recoverable; a decision made with stale or missing data is not. This applies to startup sequencing, resolver cascades, milestone conflicts, and any path where "trade now" competes with "verify first."

#### 6.6 File ownership summary

| File | Location | Owner | Change cadence |
|---|---|---|---|
| `automation_config.py` | source | code | rare (code review) |
| `games_full.json` | `get_data_dir()` | Engine (via existing persistence) | per add/remove batch |
| `tree_metadata.json` | `get_data_dir()` | TreeMetadataStore | per-commit / per-expand |
| `settings.json` | `get_data_dir()` | existing settings layer | occasional |
| `brain/principles.md` | repo | human | rare |

### 7. Migration plan

#### 7.1 Feature flag

`automation_config.tree_mode: bool = False` — all new behavior gated behind this.

- `tree_mode = False`: today's behavior unchanged. `scan_events` runs. `_expiration_fallback` used. No DiscoveryService, no TreeScreen.
- `tree_mode = True`: new behavior active. Old paths bypassed but not deleted until Phase 5.

#### 7.2 Phases

**Phase 1 — Scaffold.** Land new components (DiscoveryService, MilestoneResolver, TreeMetadataStore, TreeScreen), the two new Engine entry points (`add_pairs_from_selection`, `remove_pairs_from_selection`), the resolver cascade, and the `suppress_on_change()` context manager — all behind the flag. Extend `ArbPair` with optional `source` and `engine_state` fields; update the legacy `_persist_games` writer in [`__main__.py:345`](src/talos/__main__.py:345) to include both so flag-off sessions preserve them. Add principle to `brain/principles.md`. Unit tests per module, including a regression test that a flag-off session round-trips all `games_full.json` fields faithfully. Flag defaults `False`; normal sessions see no behavior change.

**Phase 2 — Dogfood.** Flip `tree_mode = True` locally. Tick a handful of representative events (one covered milestone, one uncovered, one sports, one earnings). Verify: milestone loading, resolver cascade, commit popup, conflict prompt, winding-down.

**Phase 3 — Dual-run.** Alternate sessions with flag on/off. Confirm old behavior still works when flag off (regression protection). Verify state files from tree_mode sessions don't break legacy sessions.

**Phase 4 — Default on.** Flip default to `tree_mode = True`. Legacy paths remain but unused in normal operation.

**Phase 5 — Cleanup.** Delete §5.4 listed code. Delete `tree_mode` flag. Single cleanup PR, easy to review.

#### 7.3 State migration

Talos already persists active pairs via `games_full.json`. The migration is **schema-additive only** — new optional `source` field; no file moves; no data transform.

- **First Phase 2 start with `tree_mode = True`:** existing `games_full.json` records are read unchanged. Engine stamps `source = "migration"` on any record missing the field, so they're distinguishable in logs from new tree-added records. Pairs continue running without interruption.
- **`tree_metadata.json`:** created empty on first write (first manual override, first tick marking review, or first deliberate untick).
- **`settings.json` `tree` sub-object:** created on first save after a tree-settings edit. Absent → defaults apply.

No existing state is moved, renamed, or reformatted. Rollback is a file-compatible no-op: legacy code paths read games_full.json identically regardless of the `source` field's presence.

#### 7.4 Rollback

Any phase: set `tree_mode = False`, restart. Old paths resume. `games_full.json` is read and re-written by the Phase-1-updated legacy writer, which preserves `source` / `engine_state` fields round-trip (they're read into `ArbPair`, persisted back on next `on_change` save). `tree_metadata.json` stays on disk untouched — next re-enable picks up with all prior overrides and deferred-untick flags intact. Winding-down pairs persisted with `engine_state = "winding_down"` survive the round-trip — a flag-off session re-reads them with that state preserved on disk, even though the legacy engine doesn't act on the field. Re-enabling tree_mode picks up the winding-down state correctly.

Catastrophic bug discovered after Phase 5 cleanup: `git revert` of the cleanup commit restores legacy paths verbatim.

### 8. Testing strategy

**Unit tests:**
- `DiscoveryService`: mocked httpx, pagination, error handling, semaphore bounds.
- `MilestoneResolver`: index building, atomic replace, refresh loop.
- `TreeMetadataStore`: persistence round-trip for manual overrides / first-seen / reviewed-at / deliberately-unticked set.
- Tree commit path: staged changes → Engine.add_pairs_from_selection / remove_pairs_from_selection → games_full.json updated + full engine wiring (adjuster, GSR, data_collector).
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

## 10. Persistence identity — why leaves are pairs, not events

The tree UI thinks in terms of **events** — that's how humans reason about Kalshi markets ("the Fed presser," "the Survivor episode"). But the engine's real unit of monitoring is the **ArbPair**, and a single Kalshi event can produce multiple independent pairs.

**Current engine model** (unchanged by this design):

- **Sports events** → exactly one `ArbPair` per event (cross-NO arb on the two markets). `event_ticker == kalshi_event_ticker`, `ticker_a != ticker_b`.
- **Non-sports with 1 market** → one `ArbPair` where `event_ticker == ticker_a == ticker_b == market_ticker`, sides are "yes"/"no" (YES/NO self-arb). `kalshi_event_ticker` stored as separate metadata.
- **Non-sports with N markets** → up to N independent `ArbPair`s. Each has `event_ticker == its_own_market_ticker`. All share the same `kalshi_event_ticker`.

**Persistence is keyed by `event_ticker` (the pair identity)**, not by `kalshi_event_ticker`. This is how `games_full.json` has worked since it was introduced; any scanner/adjuster/ledger lookup goes through the pair's `event_ticker`.

**What this design does:**

- **Leaf selections persist as pair records** — one record per ArbPair — in the existing `games_full.json`. No new persistence layer, no new identity model.
- **Tree UI presents an event-centric view** — the expandable "event" node is a convenience grouping. Expanding shows the markets underneath (which correspond 1:1 with pairs for non-sports, or a single pair for sports).
- **Ticking an event at event-level is syntactic sugar** for "tick all its active markets." The commit fans out to N pair records.
- **Event-level metadata** (`manual_event_start`, `first_seen`, `reviewed_at`, `deliberately_unticked`) is keyed by `kalshi_event_ticker` — because these decisions are about the underlying event, and should apply uniformly to all pairs sharing that event.

**What this design does NOT do:**

- Introduce a new "MonitoredEvent" entity that contains multiple child pairs.
- Rewrite the scanner, adjuster, or ledger to operate at event level.
- Change how `add_game` / `remove_game` / `restore_game` work internally.

The net effect: UI is event-centric; persistence is pair-centric; the engine is unchanged. Codex-surfaced concern addressed.

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
