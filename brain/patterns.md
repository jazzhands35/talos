# Patterns

Recurring patterns and conventions in this codebase.

## REST client method pattern

Every REST endpoint method follows the same shape:

```python
async def get_thing(self, id: str, *, optional_param: str | None = None) -> Thing:
    params: dict[str, Any] = {}
    if optional_param:
        params["optional_param"] = optional_param
    data = await self._request("GET", f"/things/{id}", params=params)
    return Thing.model_validate(data["thing"])
```

- Positional args for required identifiers, keyword-only for optional filters
- Build params dict conditionally (don't send None values)
- `_request` handles auth, logging, error mapping
- Return Pydantic model, never raw dict
- List endpoints return `list[Model]` with optional `limit`/`cursor` params

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

## Pure state + async orchestrator split

Separate I/O orchestration from state management:

- **Pure state machine** (`OrderBookManager`): No async, no I/O. Receives data, updates state, answers queries. Trivially testable — no mocks needed for its own logic.
- **Async orchestrator** (`MarketFeed`): Owns WS subscription lifecycle. Routes messages to the state machine. Tests mock the WS and state boundaries with `MagicMock(spec=...)` + `AsyncMock`.

This split makes the hot path (delta application) zero-cost to test and keeps the async surface area minimal.

## Sorted level insertion with bisect

For maintaining sorted orderbook levels on the hot path:

```python
bisect.insort(side_levels, new_level, key=lambda lvl: -lvl.price)
```

O(log n) insertion instead of append + sort (O(n log n)). Mutate existing levels in place rather than creating new Pydantic objects.

## Literal types for enum-like fields

Use `Literal["yes", "no"]` instead of bare `str` for fields with known values:

```python
side: Literal["yes", "no"]
```

Pydantic rejects invalid values at parse time, preventing silent misrouting downstream.

## Module-scoped constants for channel names

Extract repeated string literals into module constants:

```python
_ORDERBOOK_CHANNEL = "orderbook_delta"
```

Prevents typo-based bugs when the same channel name is used in subscribe, unsubscribe, and callback registration.

## Layer 2 test pattern

For async orchestrator tests (e.g., MarketFeed):

```python
ws = MagicMock(spec=KalshiWSClient)
ws.subscribe = AsyncMock()
ws.listen = AsyncMock()
```

- `MagicMock(spec=...)` for the class (catches typos in method names)
- Override async methods with `AsyncMock()` individually
- Assert on `.assert_called_once_with(...)` for exact argument verification

## Windows development

- Run pytest via `.venv/Scripts/python -m pytest` (not bare `pytest`, not on PATH in Git Bash)
- Compare `Path` objects directly, not `str(path)` (Windows uses backslashes)
