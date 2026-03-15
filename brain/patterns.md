# Patterns

Recurring patterns and conventions in this codebase.

## REST client method pattern

Positional args for IDs, keyword-only for filters, return Pydantic models. Exemplar: `rest_client.py`.

## Pydantic model pattern

Pydantic v2 BaseModel. Money in cents (`int`), timestamps as `str`. Use `model_validator(mode="before")` for API quirks (not `model_post_init`). Exemplar: `models/market.py`.

## Test pattern

One file per module (`tests/test_{module}.py`). Mock HTTP via `AsyncMock(spec=httpx.AsyncClient)`. Assert on model fields. Exemplar: `tests/test_rest_client.py`.

## Pure state + async orchestrator split

Separate I/O orchestration from state management. See [[principles#13. Test Purity Drives Architecture]] and [[decisions]].

- **Pure state machine** (`OrderBookManager`, `ArbitrageScanner`, `PositionLedger`): No async, no I/O. Receives data, updates state, answers queries. Trivially testable — no mocks needed.
- **Async orchestrator** (`MarketFeed`, `GameManager`, `TradingEngine`): Owns I/O lifecycle. Routes data to the state machine. Tests mock the I/O boundaries.

## Callback-based layer decoupling

Wire layers together without direct module dependencies using optional callbacks. See [[principles#13. Test Purity Drives Architecture]].

```python
self.on_book_update: Callable[[str], None] | None = None
feed.on_book_update = scanner.scan  # wired at startup
```

The callback attribute is `None` by default (safe to ignore in tests). No event bus, no pub/sub library — just a function pointer. Applied in: `MarketFeed.on_book_update`, `GameManager.on_change`.

## Conditional wiring

Optional behavior is activated by injecting a dependency, not by setting a flag. If `self._dep is None`, the feature does not exist — no dead code paths, no untested branches. Applied in: `TalosApp` (conditional timers), `MarketFeed` (`on_book_update`), test mode (inject only `scanner`). See [[principles#4. Subtract Before You Add]].

## TUI dependency injection

`TalosApp` accepts `engine: TradingEngine | None` for full behavior and `scanner: ArbitrageScanner | None` for scanner-only test mode. Production constructs a `TradingEngine` in `__main__.py` and passes it. Tests inject only what they need.

```python
TalosApp(engine=engine)       # full behavior
TalosApp(scanner=scanner)     # scanner-only for table tests
TalosApp()                    # bare mount for widget tests
```

The engine owns all business state (orders, balance, positions, queue cache). The app pulls from engine properties after each poll and pushes to widgets. See [[decisions#2026-03-08 — TradingEngine extraction and position unification]].

## Isolate non-critical API calls

When a method chains multiple API calls, wrap non-critical enrichment calls in their own try/except so failures don't abort the critical path. See [[principles#9. Idempotency and Resilience]] and [[decisions#2026-03-06 — Queue position: separate fast polling with conservative merge]].

## Financial calculation precision

Carry exact values through the entire computation pipeline. Only format/round at the display boundary. Integer division truncation compounds linearly with contract count — a 0.58¢ rounding error × 1400 contracts = $8.12 discrepancy.

See [[decisions#2026-03-09 — Quadratic fee model and fill-time charging]] and [[principles#21. Authoritative Data Over Computed Data]].

- Store fill costs as **total cents** (`price × count` accumulated), not per-contract averages
- Pass totals through models (`LegSummary.total_fill_cost`) rather than dividing early
- Per-contract averages are acceptable for display labels (e.g., "avg 49.6¢") but never for P&L math
- Format dollar amounts with `:.2f` for cent-accurate display, not `:.0f`

Applied in: `scenario_pnl()` takes `total_cost_a`/`total_cost_b`, `LegSummary.total_fill_cost` carries exact costs, `_fmt_net_odds()` passes totals to P&L functions.

## Enrichment caching with split polling cadence

When primary data (orders) is expensive to fetch and enrichment data (queue positions) changes faster, use separate polling timers with conservative merge for monotonically improving values. Applied in: `TradingEngine` — `_orders_cache` + `_queue_cache` with `_merge_queue()`.

## Proposal expiry (superseded by new events)

When a proposed action is outstanding (awaiting human approval), a new event of the same type supersedes the old proposal rather than queuing behind it. The old proposal is discarded and logged. This prevents stale proposals from executing against a market that has already moved.

Applied in: `BidAdjuster` — if a new jump event fires on the same side while a proposal is pending, the old proposal is expired and a new one is computed from current state. See [[principles#20. Inaction Is a Decision — Make It Visible]].

**Why not queue:** Queued proposals would execute sequentially against progressively stale state. Each proposal assumes a specific market price and position — by the time the second one executes, those assumptions are invalid.

## Notification path separation (real-time vs periodic)

When the same logic runs from both real-time events (WS callbacks) and periodic sweeps (timers), separate the notification path from the proposal path. Real-time events fire toasts + create proposals; periodic sweeps silently ensure proposals exist without spamming.

Applied in: `TradingEngine` — `on_top_of_market_change()` calls `_generate_jump_proposal()` + fires toast; `reevaluate_jumps()` calls `_generate_jump_proposal()` only. Without separation, periodic re-evaluation re-fires the same toast every cycle after the operator already approved and executed the action.

**Why not deduplicate at the toast layer:** The proposal queue deduplicates proposals by key, but toasts are fire-and-forget UI events. Deduplication must happen at the call site — the periodic sweep simply skips the toast.

## Deferred action queue (blocked by precondition)

When an action is blocked by a precondition (e.g., dual-jump tiebreaker — only the most-behind side adjusts first), the blocked action is remembered and automatically re-evaluated when the precondition clears. This avoids relying on external events that may never fire.

Applied in: `BidAdjuster` — when both sides of a pair are jumped, the less-behind side is deferred. When the most-behind side's unit completes (precondition clears), the deferred side is re-evaluated for profitability and safety before proposing.

**Why not rely on fresh events:** `TopOfMarketTracker` fires on state changes. If side B was already flagged as jumped and the price hasn't moved, no new event fires. Without the deferred queue, the jump goes unhandled indefinitely. See [[principles#19. Most-Behind-First on Dual Jumps]].

## Lifecycle callback for audit logging

Pure state machines emit lifecycle events via optional callbacks. External consumers (logging, UI, metrics) subscribe without adding I/O to the state machine.

```python
class ProposalQueue:
    on_lifecycle: Callable[[str, Proposal], None] | None = None
    def _emit(self, action: str, proposal: Proposal) -> None:
        if self.on_lifecycle:
            self.on_lifecycle(action, proposal)
```

Wired in `__main__.py`: `engine.proposal_queue.on_lifecycle = suggestion_log.log`. The `SuggestionLog` appends human-readable entries to a file — no imports or coupling from the queue side.

Applied in: `ProposalQueue` emits PROPOSED, SUPERSEDED, APPROVED, REJECTED, EXPIRED → `SuggestionLog` writes to `suggestions.log`. See [[decisions#2026-03-10 — Runaway bidding: safety gate wiring and Kalshi-as-truth]].

## Stability reset as sync-gap buffer

After an action is approved and executed, reset the proposer's stability timer so it must re-observe stable conditions before re-proposing. This bridges the gap between order placement and the next `sync_from_orders` poll (~10s).

Without this, the sequence is: approve → place order → proposer sees stale state (resting=0) → immediately re-proposes → operator approves again → runaway. The stability reset forces a wait period that naturally covers the sync delay.

Applied in: `OpportunityProposer.record_approval()` pops the event from `_stable_since`, requiring `stability_seconds` of fresh observation before the next proposal. See [[decisions#2026-03-10 — Runaway bidding: safety gate wiring and Kalshi-as-truth]].

**Limitation:** Stability reset alone is insufficient when `sync_from_orders` runs within the same `refresh_account` call (using stale data fetched before placement). The generation-based stale-sync guard (below) provides the primary defense; stability reset is a secondary buffer. See [[decisions#2026-03-13 — Optimistic ledger with generation-based stale-sync guard]].

## Generation-based stale-sync protection

When an async orchestrator method (`refresh_account`) has multiple yield points, concurrent tasks (auto-accept) can mutate shared state between fetching data and acting on it. A generation counter prevents stale fetches from overwriting fresher state.

```
refresh_account:
  bump_sync_gen()          # gen = N
  orders = get_orders()    # stale if placement happens later
  sync_from_orders(orders) # may overwrite optimistic state
  await get_positions()    # ← yield point: auto-accept can place orders
  evaluate_opportunities() # sees whatever sync_from_orders left
```

The `_placed_at_gen` flag on `_SideState` records which generation the placement happened in. `sync_from_orders` refuses to clear resting when `placed_at_gen >= sync_gen` (stale data). The next `bump_sync_gen()` advances the counter, allowing fresh data to clear resting normally.

Applied in: `PositionLedger._placed_at_gen`, `PositionLedger.bump_sync_gen()`, `PositionLedger.record_placement()`. Engine calls `bump_sync_gen()` at start of `refresh_account` and `record_placement()` after successful order creation. See [[decisions#2026-03-13 — Optimistic ledger with generation-based stale-sync guard]].

**Design properties:**
- Guard expires after exactly one generation (~10s poll cycle)
- Fresh syncs that include the order in `resting_list` clear the guard immediately
- Explicit cancels (`record_cancel`) clear the guard
- `reset_pair()` clears the guard

## Monotonic state updates across data sources

When multiple data sources feed the same state, each source should only **increase** values, never decrease. This prevents sources from fighting each other due to gaps, archival, or timing differences.

Applied in: `PositionLedger` — `sync_from_orders` takes `max(orders_reported, current_ledger)` for fill counts; `sync_from_positions` only augments when `positions > ledger`. Neither method can decrease fills. See [[decisions#2026-03-10 — Positions API as second authoritative source for fills]].

**Why not "last writer wins":** When source A reports 10 fills and source B later reports 0 (due to archival), last-writer-wins erases real data. Monotonic updates guarantee that once a fill is recorded, it's permanent — matching the real-world invariant that fills can't unfill.

## Verify after every order action

After any action that changes Kalshi state (place, amend, cancel), immediately re-sync from Kalshi to confirm the outcome. Don't wait for the next polling cycle — the system must know truth within milliseconds of acting, not 10 seconds later.

Applied in: `_verify_after_action()` in `engine.py` — runs the full two-source sync (orders + positions) after every rebalance, adjustment, and bid placement. Wrapped in try/except so a failed verification never blocks the action itself. See [[principles#7. Kalshi Is the Source of Truth — Always]] and [[principles#15. Position Accuracy Is Non-Negotiable]].

**Why not rely on the next poll:** The polling cycle runs `check_imbalances` which skips events where `delta < unit_size`. If the action succeeded and resolved the imbalance, the next poll correctly sees "balanced" and does nothing — but the system never confirmed the action's outcome. If the action failed silently, the system assumes everything is fine for 10s. Immediate verification catches both cases.

## Multi-step execution with fail-safe ordering

When an action has multiple steps with different risk profiles, order them so each intermediate state is strictly better (or no worse) than before. If any step fails, halt — the partial result is still an improvement.

Applied in: `execute_rebalance` in `rebalance.py` — step 1 (reduce over-side resting) runs before step 2 (catch-up bid on under-side). If step 1 fails, step 2 is skipped. If step 2 fails, step 1 already reduced the imbalance. The delta never temporarily increases. See [[decisions#2026-03-10 — Position imbalance detection and two-step rebalance]].

**Why not atomic:** A single API call can't reduce one side and add to another. Sequential steps are inevitable. Fail-safe ordering means we never need rollback — each step is independently valuable.

## Defensive WS dispatch (never crash the listen loop)

The WS listen loop (`async for raw_msg in self._ws`) is the single point of failure for ALL real-time data — orderbooks, ticker, portfolio events. Wrap both `model_validate()` and callback execution in try/except inside `_dispatch()` so a single bad message or callback bug can't kill the loop.

Applied in: `ws_client.py` `_dispatch()` — catches parse errors and callback exceptions independently, logs with `ws_message_parse_error` / `ws_callback_error`, continues processing. Added after a production bug where newly-subscribed channels (user_orders, fill, ticker) could have crashed the entire WS pipeline on schema mismatch.

**Why not fail-fast:** Unlike REST (where one bad response means one failed operation), a crashed WS loop means ALL channels die — orderbook deltas stop, ticker updates freeze, portfolio notifications halt. The blast radius of one bad message type is disproportionate. Log and skip is correct here.

## Order-specific APIs need order-specific data

When calling an API that acts on a single order (amend, cancel, get), use data from that specific order — not aggregates from the position ledger. The ledger aggregates fills across all orders (including archived ones augmented by the positions API), but the amend API needs `count = order.fill_count + desired_remaining` for *that* order.

Applied in: `execute_rebalance` in `rebalance.py` uses `decrease_order(reduce_to=target)` which sidesteps the issue entirely — `reduce_to` is absolute, not relative to fill count. `BidAdjuster.execute` fetches `get_order(cancel_order_id)` and uses `fresh_order.fill_count + fresh_order.remaining_count` for the amend `count`. See [[decisions#2026-03-12 — Rebalance step 1: decrease_order replaces amend_order]] and [[decisions#2026-03-12 — BidAdjuster.execute: fetch fresh order before amend]].

**Lesson:** Prefer APIs with absolute targets (`reduce_to=N`) over relative ones (`count = fill_count + desired`) when possible. When amend is required (price changes), always fetch the order's own state first — never use ledger aggregates.

**Why not use aggregate:** If old orders were archived and a new one was placed, the aggregate might show 40 fills while the current order has 0. Using aggregate fills in the amend `count` makes `new_total` equal the order's existing total → `AMEND_ORDER_NO_OP`. The aggregate is correct for position display; the order's own state is correct for order-specific actions.

## Extract as functions, not classes

When a god-class method grows past ~200 lines with nested branches, extract it as a pair of standalone functions — not a new class. A pure detection function + async execution function avoids creating state that must stay in sync with the orchestrator's caches. The orchestrator becomes a thin loop calling the pure function and dispatching the result.

Applied in: `rebalance.py` — `compute_rebalance_proposal()` (pure, ~120 lines) + `execute_rebalance()` (async, ~180 lines) extracted from `engine.py`'s `check_imbalances()` + `_execute_rebalance()`. Engine's `check_imbalances` became a 15-line loop. See [[decisions#2026-03-13 — Rebalance extraction from TradingEngine]].

**Why not a class:** A `RebalanceExecutor` class would need injected references to rest_client, adjuster, scanner, and notify callback — making it a mini-engine with its own lifecycle. Functions receive these as parameters, have no state to manage, and can't get out of sync.

## Batch widget updates in Textual

When mutating multiple cells in a Textual `DataTable`, wrap all `add_row` / `remove_row` calls in `self.app.batch_update()`. Without this, each call triggers a layout invalidation and repaint.

Applied in: `OpportunitiesTable.refresh_from_scanner()`. Clear + re-add all rows in sorted order each cycle (required because `update_cell` cannot reorder rows).

## Textual DataTable gotchas

- **`update_cell` doesn't reorder rows** — it changes cell values in place. To re-sort, clear all rows and re-add in the desired order within `batch_update()`.
- **Widgets don't receive their own messages** — `DataTable.HeaderSelected` must be handled on the parent app, not on the `DataTable` subclass. Forward to the widget via a method call.
- **Don't use `**kwargs: object` in widget `__init__`** — causes Pyright errors. Use explicit named params: `name`, `id` (with `# noqa: A002`), `classes`.
