# Phase 1: Market Data Models

Back to [[plans/02-kalshi-fp-migration/overview]]

## Goal

Update `Market`, `Trade`, and `OrderBook` models to parse the new `_dollars`/`_fp` field names from Kalshi API responses. Internal field types stay as int cents / int contracts.

## Changes

### `src/talos/models/market.py`

**Market** — Add `model_validator(mode="before")` that converts:
- `yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, `no_ask_dollars`, `last_price_dollars` → cents int
- `volume_fp`, `open_interest_fp` → int

Backward-compatible: if old int fields are present (test fixtures), they work as before.

**Trade** — Extend existing `_normalize` validator to also handle:
- `yes_price_dollars`, `no_price_dollars` → cents int
- `count_fp` → int

The validator already handles `taker_side` → `side` and float `price` → cents. Add the new conversions alongside.

**OrderBook** — Extend existing `_coerce_levels` validator to handle the new level format:
- Old: `[[52, 10], [48, 5]]` (int cents, int qty)
- New: `[["0.52", "10.00"], ["0.48", "5.00"]]` (dollars str, fp str)

Detection: if array elements are strings or floats, convert to `[cents_int, qty_int]` before existing parsing. Also handle `orderbook_fp` wrapper key if the REST response nests it differently.

### `tests/`

Add test cases for each model with new-format API payloads. Keep existing tests (they verify backward compat with old format).

## Verification

### Static
- `ruff check src/talos/models/market.py`
- `pyright` on models/market.py (ignore known import false positives)

### Runtime
- `pytest tests/test_rest_client.py -x` (uses Market/Trade/OrderBook models)
- New tests: parse a Market from `{"yes_bid_dollars": "0.52", "no_ask_dollars": "0.48", ...}` → verify `market.yes_bid == 52`
- New tests: parse an OrderBook with string-format levels → verify `book.yes[0].price == 52`
