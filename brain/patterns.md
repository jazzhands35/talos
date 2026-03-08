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

- **Pure state machine** (`OrderBookManager`, `ArbitrageScanner`, `compute_event_positions`): No async, no I/O. Receives data, updates state, answers queries. Trivially testable — no mocks needed.
- **Async orchestrator** (`MarketFeed`, `GameManager`): Owns I/O lifecycle. Routes data to the state machine. Tests mock the I/O boundaries.

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

The Textual app accepts optional dependencies for testability. See [[principles#13. Test Purity Drives Architecture]].

```python
class TalosApp(App):
    def __init__(self, *, scanner=None, game_manager=None, rest_client=None,
                 market_feed=None, tracker=None, initial_games=None):
```

Tests inject only what they need (usually just `scanner`). Production wires the full chain. Conditional timers keep tests fast.

## Isolate non-critical API calls

When a method chains multiple API calls, wrap non-critical enrichment calls in their own try/except so failures don't abort the critical path. See [[principles#9. Idempotency and Resilience]] and [[decisions#2026-03-06 — Queue position: separate fast polling with conservative merge]].

## Financial calculation precision

Carry exact values through the entire computation pipeline. Only format/round at the display boundary. Integer division truncation compounds linearly with contract count — a 0.58¢ rounding error × 1400 contracts = $8.12 discrepancy.

- Store fill costs as **total cents** (`price × count` accumulated), not per-contract averages
- Pass totals through models (`LegSummary.total_fill_cost`) rather than dividing early
- Per-contract averages are acceptable for display labels (e.g., "avg 49.6¢") but never for P&L math
- Format dollar amounts with `:.2f` for cent-accurate display, not `:.0f`

Applied in: `scenario_pnl()` takes `total_cost_a`/`total_cost_b`, `LegSummary.total_fill_cost` carries exact costs, `_fmt_net_odds()` passes totals to P&L functions.

## Enrichment caching with split polling cadence

When primary data (orders) is expensive to fetch and enrichment data (queue positions) changes faster, use separate polling timers with conservative merge for monotonically improving values. Applied in: `TalosApp` — `_orders_cache` + `_queue_cache` with `_merge_queue()`.

## Proposal expiry (superseded by new events)

When a proposed action is outstanding (awaiting human approval), a new event of the same type supersedes the old proposal rather than queuing behind it. The old proposal is discarded and logged. This prevents stale proposals from executing against a market that has already moved.

Applied in: `BidAdjuster` — if a new jump event fires on the same side while a proposal is pending, the old proposal is expired and a new one is computed from current state.

**Why not queue:** Queued proposals would execute sequentially against progressively stale state. Each proposal assumes a specific market price and position — by the time the second one executes, those assumptions are invalid.

## Deferred action queue (blocked by precondition)

When an action is blocked by a precondition (e.g., dual-jump tiebreaker — only the most-behind side adjusts first), the blocked action is remembered and automatically re-evaluated when the precondition clears. This avoids relying on external events that may never fire.

Applied in: `BidAdjuster` — when both sides of a pair are jumped, the less-behind side is deferred. When the most-behind side's unit completes (precondition clears), the deferred side is re-evaluated for profitability and safety before proposing.

**Why not rely on fresh events:** `TopOfMarketTracker` fires on state changes. If side B was already flagged as jumped and the price hasn't moved, no new event fires. Without the deferred queue, the jump goes unhandled indefinitely. See [[principles#19. Most-Behind-First on Dual Jumps]].
