# Phase 2: Portfolio Models

Back to [[plans/02-kalshi-fp-migration/overview]]

## Goal

Update `Order`, `Fill`, and `Position` models to parse the new `_dollars`/`_fp` field names. These are the most critical models — they feed the position ledger, safety gates, and P&L calculations.

## Changes

### `src/talos/models/order.py`

**Order** — Add `model_validator(mode="before")`:
- Dollars → cents: `yes_price_dollars`, `no_price_dollars`, `taker_fees_dollars`, `maker_fees_dollars`
- FP → int: `fill_count_fp`, `remaining_count_fp`, `initial_count_fp`

**Fill** — Add `model_validator(mode="before")`:
- Dollars → cents: `yes_price_dollars`, `no_price_dollars`
- FP → int: `count_fp`

### `src/talos/models/portfolio.py`

**Position** — Add `model_validator(mode="before")`:
- FP → int: `position_fp`
- Dollars → cents: `total_traded_dollars`, `market_exposure_dollars`

Note: `resting_orders_count` may also change to `resting_orders_count_fp`. Handle if present.

**Balance** — Verify whether `balance` and `portfolio_value` have `_dollars` equivalents. If so, add validator. The migration docs didn't explicitly list Balance, but check the actual API response.

### `tests/`

Test each model with new-format payloads. The Order model is the highest-priority — it's used in `sync_from_orders` which drives the entire position tracking pipeline.

## Verification

### Static
- `ruff check src/talos/models/order.py src/talos/models/portfolio.py`
- Type check passes

### Runtime
- `pytest tests/test_engine.py -x` — engine tests use Order mocks extensively
- `pytest tests/test_position_ledger.py -x` — ledger sync tests
- New tests: parse Order from `{"fill_count_fp": "10.00", "no_price_dollars": "0.48", ...}` → verify `order.fill_count == 10`, `order.no_price == 48`
- New tests: parse Position from `{"position_fp": "-10.00", "total_traded_dollars": "4.80"}` → verify `position.position == -10`, `position.total_traded == 480`
