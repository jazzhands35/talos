# Patterns

Recurring patterns and conventions in this codebase.

## REST client method pattern

Every REST method: positional args for required IDs, keyword-only for optional filters; build params dict conditionally; `_request` handles auth/logging/errors; return Pydantic model, never raw dict. List endpoints return `list[Model]` with optional `limit`/`cursor`.

## Pydantic model pattern

All models use Pydantic v2 BaseModel with minimal configuration:

- `from __future__ import annotations` at top of every file
- Optional fields use `field: type | None = None`
- Money fields are `int` (cents), never `float`
- Timestamps are `str` (ISO 8601) — no datetime parsing at the model layer
- For API quirks (e.g. raw `[[int, int]]` arrays), use `@model_validator(mode="before")` — NOT `model_post_init`

## Test pattern

- One test file per source module: `tests/test_{module}.py`
- Mock HTTP with `AsyncMock(spec=httpx.AsyncClient)` replacing `client._http`
- Use `_mock_response(status, json_data)` helper for consistency
- Tests assert on model fields, not raw dicts
- Fixtures for `config`, `mock_auth`, `client` at file level
- For async orchestrators: `MagicMock(spec=Class)` + override async methods with `AsyncMock()` individually

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

Optional behavior is activated by injecting a dependency, not by setting a flag. If `self._dep is None`, the feature does not exist — no dead code paths, no untested branches. Applied in: `TalosApp` (conditional timers), `MarketFeed` (`on_book_update`), test mode (inject only `scanner`).

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
