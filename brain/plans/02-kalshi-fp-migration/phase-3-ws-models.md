# Phase 3: WebSocket Models

Back to [[plans/02-kalshi-fp-migration/overview]]

## Goal

Update all WS message models to parse the new field names. This is critical for live data flow — orderbook snapshots and deltas drive the scanner and opportunity detection.

## Changes

### `src/talos/models/ws.py`

**OrderBookSnapshot** — Add `model_validator(mode="before")`:
- Read `yes_dollars_fp` / `no_dollars_fp` arrays (new field names)
- Convert each `["0.52", "10.00"]` pair to `[52, 10]` (cents int, qty int)
- Store in `yes` / `no` fields (existing format)
- `OrderBookManager.apply_snapshot()` and `_parse_levels_sorted()` continue working unchanged

**OrderBookDelta** — Update to prioritize new fields over old:
- If `price_dollars` present → `price = round(float(price_dollars) * 100)`
- If `delta_fp` present → `delta = int(float(delta_fp))`
- The model already has `price_dollars` and `delta_fp` as optional fields — just add the conversion logic
- `OrderBookManager.apply_delta()` reads `delta.price` and `delta.delta` — unchanged

**TickerMessage** — Add `model_validator(mode="before")`:
- Dollars → cents: `yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, `no_ask_dollars`, `last_price_dollars`
- FP → int: `volume_fp`

**TradeMessage** — Add `model_validator(mode="before")`:
- Dollars → cents: `price` from `yes_price_dollars`/`no_price_dollars`
- FP → int: `count_fp`

## Changes NOT needed

- `WSSubscribed`, `WSError` — no numeric fields, unaffected
- `ws_client.py` — dispatches by message type, unchanged
- `orderbook.py` — reads from model fields, unchanged (validators convert first)
- `market_feed.py` — passes parsed models to callbacks, unchanged

## Verification

### Static
- `ruff check src/talos/models/ws.py`
- Type check passes

### Runtime
- `pytest tests/test_orderbook.py -x` — orderbook manager tests
- `pytest tests/test_ws_client.py -x` if it exists
- New tests: parse OrderBookSnapshot with `{"yes_dollars_fp": [["0.52", "10.00"]], ...}` → verify `snapshot.yes == [[52, 10]]`
- New tests: parse OrderBookDelta with `{"price_dollars": "0.48", "delta_fp": "5.00", ...}` → verify `delta.price == 48`, `delta.delta == 5`
