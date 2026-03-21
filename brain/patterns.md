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

**Toast accumulation danger:** Textual's `self.notify()` creates a `ToastHolder` widget with its own asyncio task. Over hours, even rate-limited toasts (10/10s) accumulate thousands of tasks, freezing the event loop. **Fix:** Tiered notification system — automated events go to `ActivityLog` (RichLog widget, zero asyncio overhead), toasts reserved for errors and user-initiated actions only. `_notify(toast=True)` for the rare cases that need interruptive UI. See [[decisions#2026-03-17 — Tiered notifications: ActivityLog replaces toast accumulation]].

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
- Explicit cancels (`record_cancel`) set the guard to `sync_gen + 1` (see below)
- `reset_pair()` clears the guard

**Post-sync mutations need gen+1:** Mutations that happen AFTER `bump_sync_gen` in the same cycle (e.g., `record_cancel` in `check_imbalances`, which runs after `sync_from_orders`) must use `sync_gen + 1` to protect the NEXT cycle's sync. Using bare `sync_gen` only protects the current cycle's sync (which already ran).

**Order ID race during await:** `await cancel_order()` yields to the event loop. A WS fill handler can update the ledger's `resting_order_id` during the await, causing `record_cancel` to fail (order_id mismatch). Use `mark_side_pending()` as a fallback — it sets the gen guard without requiring an order_id match.

## Kalshi eventual consistency and recently-cancelled filter

Kalshi docs: "There is typically a short delay before exchange events are reflected in the API endpoints." The DELETE cancel response is synchronous (returns the zeroed order), but GET /portfolio/orders may still return the order as "resting" for 1-2 cycles.

**Pattern:** Track cancelled order IDs in `_recently_cancelled: set[str]` on the ledger. `sync_from_orders` filters these out of `kalshi_resting` before processing. IDs are pruned from the set when the GET confirms they're gone (no longer returned as resting).

Applied in: `PositionLedger._recently_cancelled`, populated by `record_cancel()` and `mark_order_cancelled()`. The rebalance sweep (`_cancel_all_resting`) registers all cancelled IDs. See also `has_pending_change()` which blocks new bid proposals while unconfirmed state exists.

**Why not just extend the gen guard duration:** The gen guard is time-based (expires after N generations). Kalshi's propagation delay is variable. The `_recently_cancelled` filter is data-driven — it persists until the GET confirms the order is gone, regardless of how many cycles that takes.

## Orphaned order sweep on cancel

When cancelling resting orders, cancel ALL orders on the target side — not just the one tracked in the ledger. The ledger tracks ONE resting order per side, but Kalshi may have multiple from previous sessions, race conditions, or the ledger overwriting old order IDs with new ones (`record_placement` replaces).

**Pattern:** `_cancel_all_resting()` in `rebalance.py`: (1) cancel the primary order from the proposal, (2) fetch all resting for the event, (3) cancel any remaining NO-buys on the same ticker. Returns `(count, list_of_cancelled_ids)` so the caller can register them with `mark_order_cancelled()`.

Applied in: `execute_rebalance` step 1 (target_resting=0 path). The sweep adds one extra API call (`get_all_orders(event_ticker=, status="resting")`) but eliminates the infinite cancel loop from orphaned orders.

## Monotonic state updates across data sources

When multiple data sources feed the same state, each source should only **increase** values, never decrease. This prevents sources from fighting each other due to gaps, archival, or timing differences.

Applied in: `PositionLedger` — `sync_from_orders` takes `max(orders_reported, current_ledger)` for fill counts; `sync_from_positions` only augments when `positions > ledger`. Neither method can decrease fills. See [[decisions#2026-03-10 — Positions API as second authoritative source for fills]].

**Why not "last writer wins":** When source A reports 10 fills and source B later reports 0 (due to archival), last-writer-wins erases real data. Monotonic updates guarantee that once a fill is recorded, it's permanent — matching the real-world invariant that fills can't unfill.

## Verify after every order action

After any action that changes Kalshi state (place, amend, cancel), immediately re-sync from Kalshi to confirm the outcome. Don't wait for the next polling cycle — the system must know truth within milliseconds of acting, not 10 seconds later.

Applied in: `_verify_after_action()` in `engine.py` — runs the full two-source sync (orders + positions) after every rebalance, adjustment, and bid placement. Wrapped in try/except so a failed verification never blocks the action itself. See [[principles#7. Kalshi Is the Source of Truth — Always]] and [[principles#15. Position Accuracy Is Non-Negotiable]].

**Why not rely on the next poll:** The polling cycle runs `check_imbalances` which skips events where `delta < unit_size`. If the action succeeded and resolved the imbalance, the next poll correctly sees "balanced" and does nothing — but the system never confirmed the action's outcome. If the action failed silently, the system assumes everything is fine for 10s. Immediate verification catches both cases.

## API call scope must match call frequency

When calling REST APIs, the **scope** of data fetched must be proportional to the **frequency** of the call. Per-action calls (firing ~1/sec during auto-accept) must filter to a single event; per-cycle calls (every 30s) can fetch globally.

**The rule:** If a code path fires per-event or per-action, pass `event_ticker=` (or `ticker=`) to REST methods. If a code path is the global safety-net poll, fetching everything is correct.

| Call pattern | Frequency | Correct scope |
|-------------|-----------|---------------|
| `_verify_after_action` | Per action (~1/sec) | `event_ticker=event_ticker` |
| `execute_rebalance` catch-up | Per imbalance (~3+/cycle) | `event_ticker=rebalance.event_ticker` |
| `refresh_account` polling | Every 30s | Unfiltered (intentionally global) |

**Why this matters:** Unfiltered per-action calls cause rate-limit cascades. Three catch-ups in 3 seconds, each paginating through ALL orders across 50+ events, hit Kalshi's rate limit and every subsequent call fails. Filtering to single-event returns 2-4 records in a single page — no pagination, no rate limit.

**Corollary:** `get_all_orders()`, `get_positions()`, and similar methods accept filter parameters (`event_ticker=`, `ticker=`, `status=`). Always check whether the caller has a more specific context to pass through.

Applied in: `rebalance.py` (catch-up fresh sync), `engine.py` (`_verify_after_action`). Contrast with `refresh_account` which intentionally fetches globally — see [[decisions#2026-03-12 — Leaner polling reverted (event_ticker filter removed)]].

## Surface exception types in user notifications

When a try/except produces a user-visible notification, always include `type(e).__name__` in the message. Bare "failed" messages are undiagnosable — especially when structlog output is invisible (stderr captured by Textual).

```python
# Bad — user sees "Verify FAILED" with no clue why
except Exception:
    notify("Verify FAILED — position data may be stale", "warning")

# Good — user sees "Verify FAILED (KalshiRateLimitError)"
except Exception as e:
    notify(f"Verify FAILED ({type(e).__name__}) — position data may be stale", "warning")
```

**Why this matters:** structlog goes to stderr, which Textual hides. The notification is the ONLY user-visible feedback channel for errors. Without the exception type, diagnosing requires reproducing the issue with a debugger attached.

Applied in: `rebalance.py` (fresh sync failed), `engine.py` (_verify_after_action). Should be applied to all user-facing error notifications.

## Rate limit errors propagate to the scheduler

Rate-limit errors (`KalshiRateLimitError`) are categorically different from action failures. An action failure means "this broke" — log it, notify the operator, move on. A rate limit means "slow down" — the scheduler must back off, not the individual action handler.

**The rule:** Never swallow `KalshiRateLimitError` inside action try/except blocks. Re-raise it so the scheduling layer (auto-accept tick, polling loop) can set a cooldown timer using the `retry_after` header.

```python
# In engine action handlers:
except KalshiRateLimitError:
    raise  # Let auto-accept back off
except Exception as e:
    self._notify(f"Action FAILED: {type(e).__name__}: {e}", "error")

# In auto-accept tick:
except KalshiRateLimitError as e:
    backoff = max(e.retry_after or 2.0, 2.0)
    self._rate_limit_until = datetime.now(UTC) + timedelta(seconds=backoff)
```

**Exception:** Non-critical paths (like `_verify_after_action`) can catch `KalshiRateLimitError` silently — the action already succeeded and the 30s poll will sync. But the verify must catch it *specifically* (not via bare `except Exception`), and log at debug level.

Applied in: `engine.py` — adjustment and bid handlers re-raise; `_verify_after_action` catches silently. `app.py` — `_auto_accept_tick` catches and sets `_rate_limit_until` cooldown. See [[decisions#2026-03-18 — Scope per-action API calls to single event]].

## Multi-step execution with fail-safe ordering

When an action has multiple steps with different risk profiles, order them so each intermediate state is strictly better (or no worse) than before. If any step fails, halt — the partial result is still an improvement.

Applied in: `execute_rebalance` in `rebalance.py` — step 1 (reduce over-side resting) runs before step 2 (catch-up bid on under-side). If step 1 fails, step 2 is skipped. If step 2 fails, step 1 already reduced the imbalance. The delta never temporarily increases. See [[decisions#2026-03-10 — Position imbalance detection and two-step rebalance]].

**Why not atomic:** A single API call can't reduce one side and add to another. Sequential steps are inevitable. Fail-safe ordering means we never need rollback — each step is independently valuable.

## Defensive WS dispatch (never crash the listen loop)

The WS listen loop (`async for raw_msg in self._ws`) is the single point of failure for ALL real-time data — orderbooks, ticker, portfolio events. Wrap both `model_validate()` and callback execution in try/except inside `_dispatch()` so a single bad message or callback bug can't kill the loop.

Applied in: `ws_client.py` `_dispatch()` — catches parse errors and callback exceptions independently, logs with `ws_message_parse_error` / `ws_callback_error`, continues processing. Added after a production bug where newly-subscribed channels (user_orders, fill, ticker) could have crashed the entire WS pipeline on schema mismatch.

**Why not fail-fast:** Unlike REST (where one bad response means one failed operation), a crashed WS loop means ALL channels die — orderbook deltas stop, ticker updates freeze, portfolio notifications halt. The blast radius of one bad message type is disproportionate. Log and skip is correct here.

## Always set HTTP timeouts

Every `httpx.AsyncClient()` MUST have an explicit timeout. Without one, a single hung API response blocks the event loop forever. With auto-accept placing orders (4 REST calls per approval), one hung call = permanent freeze of the entire application.

Applied in: `rest_client.py` — `httpx.Timeout(15.0)` for Kalshi REST. `game_status.py` — `httpx.Timeout(10.0)` for external APIs (ESPN, OddsAPI, PandaScore).

**Why 15 seconds:** Kalshi normally responds in 200ms-3s. Anything over 15s is effectively dead. The timeout converts a permanent hang into a recoverable `TimeoutException` caught by existing error handlers.

## Order-specific APIs need order-specific data

When calling an API that acts on a single order (amend, cancel, get), use data from that specific order — not aggregates from the position ledger. The ledger aggregates fills across all orders (including archived ones augmented by the positions API), but the amend API needs `count = order.fill_count + desired_remaining` for *that* order.

Applied in: `execute_rebalance` in `rebalance.py` uses `decrease_order(reduce_to=target)` which sidesteps the issue entirely — `reduce_to` is absolute, not relative to fill count. `BidAdjuster.execute` fetches `get_order(cancel_order_id)` and uses `fresh_order.fill_count + fresh_order.remaining_count` for the amend `count`. See [[decisions#2026-03-12 — Rebalance step 1: decrease_order replaces amend_order]] and [[decisions#2026-03-12 — BidAdjuster.execute: fetch fresh order before amend]].

**Lesson:** Prefer APIs with absolute targets (`reduce_to=N`) over relative ones (`count = fill_count + desired`) when possible. When amend is required (price changes), always fetch the order's own state first — never use ledger aggregates.

**Why not use aggregate:** If old orders were archived and a new one was placed, the aggregate might show 40 fills while the current order has 0. Using aggregate fills in the amend `count` makes `new_total` equal the order's existing total → `AMEND_ORDER_NO_OP`. The aggregate is correct for position display; the order's own state is correct for order-specific actions.

## Extract as functions, not classes

When a god-class method grows past ~200 lines with nested branches, extract it as a pair of standalone functions — not a new class. A pure detection function + async execution function avoids creating state that must stay in sync with the orchestrator's caches. The orchestrator becomes a thin loop calling the pure function and dispatching the result.

Applied in: `rebalance.py` — `compute_rebalance_proposal()` (pure, ~120 lines) + `execute_rebalance()` (async, ~180 lines) extracted from `engine.py`'s `check_imbalances()` + `_execute_rebalance()`. Engine's `check_imbalances` became a 15-line loop. See [[decisions#2026-03-13 — Rebalance extraction from TradingEngine]].

**Why not a class:** A `RebalanceExecutor` class would need injected references to rest_client, adjuster, scanner, and notify callback — making it a mini-engine with its own lifecycle. Functions receive these as parameters, have no state to manage, and can't get out of sync.

## Guard interval-triggered workers with exclusive=True

When a Textual `@work(thread=False)` method is called from `set_interval`, it MUST use `exclusive=True, group="name"`. Without it, if the work takes longer than the interval, workers accumulate unboundedly. With slow I/O (REST timeouts), this causes hundreds of orphaned tasks that overwhelm the asyncio scheduler — freezing the event loop even though no individual call is blocking.

Applied in: `_poll_trades` (30s interval, each batch spawns 40+ REST calls), `_poll_queue` (3s interval). Both now use `exclusive=True`. `_poll_account` already had a manual `_poll_in_progress` flag — `exclusive=True` is the framework-native equivalent.

**Why not just a flag:** `exclusive=True` also cancels the previous worker's in-flight tasks, freeing resources. A boolean flag only prevents new starts — the old worker and its spawned tasks keep running.

See [[decisions#2026-03-16 — UI freeze: task accumulation from unbounded asyncio.gather]].

## Batch widget updates in Textual

When mutating multiple cells in a Textual `DataTable`, wrap all `add_row` / `remove_row` calls in `self.app.batch_update()`. Without this, each call triggers a layout invalidation and repaint.

Applied in: `OpportunitiesTable.refresh_from_scanner()`. Clear + re-add all rows in sorted order each cycle (required because `update_cell` cannot reorder rows).

## Textual DataTable gotchas

- **`update_cell` doesn't reorder rows** — it changes cell values in place. To re-sort, clear all rows and re-add in the desired order within `batch_update()`.
- **Widgets don't receive their own messages** — `DataTable.HeaderSelected` must be handled on the parent app, not on the `DataTable` subclass. Forward to the widget via a method call.
- **Don't use `**kwargs: object` in widget `__init__`** — causes Pyright errors. Use explicit named params: `name`, `id` (with `# noqa: A002`), `classes`.
- **Static with `height: 1fr`:** `self.update()` may not render. Override `render() -> str` + `self.refresh()` instead.
- **CSS height circular dependency:** `height: auto` parent + `height: 1fr` children = zero. Give parent fixed `height: N`.
- **`_row_locations` is a `TwoWayDict`:** Not subscriptable, `.get()` has no default arg. Use `.get(key)` + `is None` check.
- **Pair striping:** Override `_get_row_style(row_index, base_style)` with `row_index // 2 % 2` for event-pair-level zebra.
- **Overline separators:** `RichStyle(overline=True)` on segments in `_render_line_in_row` draws horizontal dividers without extra vertical space.

## New model fields require persistence updates

When adding a field to a Pydantic model that's part of a cached/persisted object, you must also:
1. Add the field to the persistence save function (e.g., `save_games_full` in `__main__.py`)
2. Add a backfill path for existing cached data missing the field
3. Verify the restore path handles the field being absent from old cache data

Without all three, the field works for newly added items but is silently `None` for everything restored from cache.

## Fallback chains must cover all failure paths

When adding a fallback data source (e.g., expiration-based start time), apply it everywhere the primary returns "unknown" — not just for items that lack a primary source. Mapped items whose provider fails to match still need the fallback. Check every code path that produces the "no data" state.

Applied in: `GameStatusResolver._expiration_fallback()` — used in `_prepare_entry` (unmapped leagues), `resolve_batch` provider-miss path, and provider-error path.

## Self-healing orderbook (resubscribe on proof of staleness)

When a post-only order is rejected with "post only cross", the exchange is telling us our bid price would immediately match — proof that our local orderbook is wrong. Use this signal to trigger an automatic resubscribe on the affected tickers, forcing a fresh snapshot from Kalshi.

Applied in: `engine.py` `place_bids()` — catches `KalshiAPIError` with "post only cross", unsubscribes and resubscribes both market tickers via `MarketFeed`. Combined with a proposer failure cooldown (`placement_failure_cooldown_seconds = 120`) to prevent re-proposing the same stale opportunity before the fresh snapshot corrects the book.

**Why resubscribe instead of REST fetch:** The WS subscription model sends a full snapshot on subscribe. Resubscribing is idempotent, resets the seq counter, and guarantees all future deltas are based on fresh state. A one-off REST fetch would give us a snapshot but wouldn't fix the WS delta stream.

**Root causes of orderbook drift (fixed 2026-03-19):**
- Bulk subscribe gap recovery only resubscribed one ticker per sid, orphaning the rest (`market_feed.py`)
- Gap-triggering deltas were dispatched before the fresh snapshot arrived (`ws_client.py`)
- Deltas arriving before their ticker's snapshot were silently dropped (`orderbook.py` — now buffered and replayed)
- Per-book seq tracking produced false stale flags for bulk subs (seq is per-SID, not per-ticker) — replaced with time-based staleness (120s threshold)
- `unsubscribe(ticker)` killed all sibling tickers on the same bulk subscription sid — now uses `update_subscription(delete_markets)` when siblings exist
- Time-based staleness catches ALL silent failure modes (orphaned subs, network issues, server bugs) — recovery resubscribes in the next 30s cycle

## REST vs WS field naming divergence

Kalshi uses DIFFERENT field names for the same data in REST vs WS responses. This is NOT a bug — it's by design. Always check BOTH OpenAPI and AsyncAPI specs.

| Data | REST Field | WS Field |
|------|-----------|----------|
| Orderbook levels (yes) | `yes_dollars` | `yes_dollars_fp` |
| Orderbook levels (no) | `no_dollars` | `no_dollars_fp` |
| Last traded price | `last_price_dollars` | `price_dollars` |
| NO-side BBA | `no_bid_dollars`, `no_ask_dollars` | *(not sent — derive from YES side)* |
| Trade side | `side` | `taker_side` (legacy `side` still sent) |

Applied in: `models/market.py` `OrderBook._coerce_levels` handles both REST and WS key names. `models/ws.py` `TickerMessage._migrate_fp` maps `price_dollars` and derives NO-side from YES-side. `TradeMessage._migrate_fp` falls back from `taker_side` to `side`.

## Kalshi `x-omitempty` fields

Some Kalshi Market fields have `x-omitempty: true` in the OpenAPI spec, meaning the key is **omitted entirely** when null (not sent as `null`). Pydantic handles this correctly with `field: str | None = None` — the field defaults to `None` when absent. But be aware when checking raw API responses: the key literally won't exist, not just be null.
