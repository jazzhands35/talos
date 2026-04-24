# Phase 4: REST Client Requests

Back to [[plans/02-kalshi-fp-migration/overview]]

## Goal

Update REST client methods that send data to Kalshi to use the new field names. Also handle any response structure changes (orderbook key).

## Changes

### `src/talos/rest_client.py`

**`create_order()`** — Update request body:
- `count` (int) → `count_fp` (str): `str(count)`
- `no_price` (int cents) → `no_price_dollars` (str): `f"{no_price / 100:.2f}"`
- `yes_price` (int cents) → `yes_price_dollars` (str): `f"{yes_price / 100:.2f}"`
- Keep method signature as int (internal convention). Convert at the serialization boundary.

**`amend_order()`** — Update request body:
- `no_price` → `no_price_dollars`
- `count` → `count_fp`
- Same conversion as create_order

**`get_orderbook()`** — Handle response key change:
- Try `data["orderbook"]` first, fall back to `data.get("orderbook_fp", data.get("orderbook", {}))`
- The OrderBook model validator (from Phase 1) handles the inner format

**`get_queue_positions()`** — Already handles `queue_position_fp`. Verify `queue_position` (int) fallback still works or is no longer needed.

### Method signature stability

The public API of `KalshiRESTClient` stays the same:
- `create_order(..., no_price: int, count: int)` — callers pass int cents
- `amend_order(..., no_price: int, count: int)` — callers pass int cents
- `get_orderbook(...)` → `OrderBook` — returns model with int fields

All conversion happens inside the REST client methods. Engine, adjuster, proposer — none change.

## Safety Audit

This phase modifies order placement code (money-touching). Invoke `safety-audit` skill after implementation to verify:
- Price conversion is correct (no off-by-one in cents↔dollars)
- Count conversion preserves exact values
- Amend still sends correct total count (fills + remaining)

## Verification

### Static
- `ruff check src/talos/rest_client.py`
- Type check passes

### Runtime
- `pytest tests/test_rest_client.py -x`
- New tests: verify `create_order(no_price=48, count=10)` sends `{"no_price_dollars": "0.48", "count_fp": "10"}`
- New tests: verify `amend_order(no_price=52, count=10)` sends `{"no_price_dollars": "0.52", "count_fp": "10"}`
- Manual: place a test order on demo environment, verify it appears correctly
