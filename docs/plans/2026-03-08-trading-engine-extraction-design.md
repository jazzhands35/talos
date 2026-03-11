# Trading Engine Extraction — Design

## Problem

`TalosApp` (490 lines) is both the UI layer and the application logic layer. It directly manages polling, caching, position sync, ledger coordination, bid adjustment approval, order placement, and game lifecycle — a fan-out of 7 direct dependencies. This violates the codebase's own "pure state + async orchestrator" pattern and makes the logic untestable without Textual.

As Talos grows in capability, this god class will only get worse. New features would need to be added to the UI class, making it harder to test, reason about, and eventually support alternative frontends.

Additionally, two systems derive position state from orders independently (`compute_event_positions()` and `PositionLedger`), creating a dual source of truth that the architecture doc already flags for unification.

## Goals

1. Extract a `TradingEngine` class that owns all non-UI application logic
2. Unify position computation: `PositionLedger` as single data source, pure formatter for display
3. Fix encapsulation violations (`BidAdjuster` accessing `ledger._sides`)
4. Move queue cache logic out of the UI layer

## Non-Goals

- Changing any external behavior or API contracts
- Adding new features
- Modifying test coverage philosophy (tests should still pass)

## Design

### TradingEngine (new: `src/talos/engine.py`)

Async orchestrator that coordinates all subsystems. Follows the same pattern as `MarketFeed` and `GameManager`.

```python
class TradingEngine:
    def __init__(
        self,
        *,
        scanner: ArbitrageScanner,
        game_manager: GameManager,
        rest_client: KalshiRESTClient,
        market_feed: MarketFeed,
        tracker: TopOfMarketTracker,
        adjuster: BidAdjuster,
        initial_games: list[str] | None = None,
    ) -> None: ...
```

**Responsibilities (moved from TalosApp):**
- `refresh_account()` — fetch balance + orders, sync ledgers, derive positions
- `refresh_queue_positions()` — fast-cadence queue polling with conservative merge
- `refresh_trades()` — trade ingestion for CPM tracking
- `start_feed()` — WS connect, game restore, listen
- `place_bids()` — order placement on both legs
- `add_games()` / `remove_game()` / `clear_games()` — game lifecycle
- `approve_adjustment()` / `reject_adjustment()` — bid adjustment execution
- Queue cache management (`_merge_queue`, `_queue_cache`, prune logic)
- CPM enrichment
- Position ledger sync loop
- Top-of-market callback handling and jump evaluation

**State exposed to UI (read-only):**
- `balance` — latest balance/portfolio values
- `orders` — latest order list (with queue positions applied)
- `position_summaries` — latest `EventPositionSummary` list for table rendering
- `scanner` — reference to scanner for opportunity data
- `tracker` — reference to tracker for top-of-market warnings
- `adjuster` — reference to adjuster for proposal queries

**Callbacks (UI subscribes):**
- `on_notification: Callable[[str, str, int], None]` — (message, severity, timeout)
- `on_proposal: Callable[[ProposedAdjustment], None]` — new adjustment proposal

### TalosApp (slimmed: `src/talos/ui/app.py`)

Thin UI shell. Takes a `TradingEngine` (or None for test mode).

```python
class TalosApp(App):
    def __init__(self, *, engine: TradingEngine | None = None, scanner: ArbitrageScanner | None = None) -> None: ...
```

**Remaining responsibilities:**
- Widget composition (`compose()`)
- Timer setup (`on_mount()`) — calls engine methods
- Rendering engine state into widgets (opportunities table, account panel, order log)
- Routing user input to engine (add games, remove game, bid confirmation, adjustment approval)
- Displaying engine notifications as Textual toasts

Test mode: inject `scanner` directly (no engine needed), same as today.

### Position Unification

**PositionLedger** remains the single source of truth for filled/resting state per side. No changes to its core API.

**New pure function** added to `position_ledger.py`:

```python
def compute_display_positions(
    ledgers: dict[str, PositionLedger],
    pairs: list[ArbPair],
    queue_cache: dict[str, int],
    cpm_tracker: CPMTracker,
) -> list[EventPositionSummary]:
    """Build display summaries from ledger state. Pure function."""
```

This replaces `compute_event_positions()` in `position.py`. Instead of re-parsing raw orders, it reads pre-computed state from the ledger and adds display-specific derived values (matched pairs, locked profit, exposure, queue positions, CPM/ETA).

**`position.py` is deleted.** Its test file (`test_position.py`) is rewritten to test `compute_display_positions`.

### Encapsulation Fix

Replace all `ledger._sides[side]` accesses in `BidAdjuster` with public accessors:

| Before | After |
|--------|-------|
| `ledger._sides[side].filled_count` | `ledger.filled_count(side)` |
| `ledger._sides[side].resting_count` | `ledger.resting_count(side)` |
| `ledger._sides[side].filled_total_cost` | `ledger.filled_total_cost(side)` |
| `ledger._sides[side].resting_price` | `ledger.resting_price(side)` |

### Dependency Flow

```
Before:  TalosApp → {Scanner, Adjuster, Ledger, Feed, REST, Tracker, CPM}  (fan-out: 7)
After:   TalosApp → TradingEngine → {Scanner, Adjuster, Ledger, Feed, REST, Tracker, CPM}  (fan-out: 1)
```

## Files Changed

| File | Change |
|------|--------|
| `src/talos/engine.py` | **New** — `TradingEngine` class (~300 lines) |
| `src/talos/ui/app.py` | **Rewrite** — thin UI shell (~150 lines) |
| `src/talos/position.py` | **Delete** — replaced by `compute_display_positions` |
| `src/talos/position_ledger.py` | **Add** `compute_display_positions()` function |
| `src/talos/bid_adjuster.py` | **Edit** — replace `_sides` access with public accessors |
| `src/talos/__main__.py` | **Edit** — construct `TradingEngine`, pass to `TalosApp` |
| `tests/test_engine.py` | **New** — engine tests (polling, sync, placement) |
| `tests/test_position.py` | **Rewrite** — test `compute_display_positions` |
| `tests/test_ui.py` | **Edit** — adapt to engine-based TalosApp |
| `tests/test_bid_adjuster.py` | **Edit** — verify public accessor usage |
| `brain/` | **Update** — architecture, codebase index, decisions, patterns |

## Risks

- **Regression in position display:** Changing the data source for `EventPositionSummary` could introduce subtle differences. Mitigation: test both old and new functions against identical inputs during transition.
- **Test rewrites:** `test_position.py` (265 lines) and `test_ui.py` (317 lines) need significant changes. Mitigation: rewrite tests to match new architecture, don't just patch.
- **Engine callback wiring:** The notification callback pattern is new for the app layer. Mitigation: same pattern already proven in `MarketFeed.on_book_update` and `BidAdjuster.on_proposal`.
