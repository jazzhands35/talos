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

## Windows development

- Run pytest via `.venv/Scripts/python -m pytest` (not bare `pytest`, not on PATH in Git Bash)
- Compare `Path` objects directly, not `str(path)` (Windows uses backslashes)
