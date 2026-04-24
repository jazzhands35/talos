# Phase 9 — Dynamic Fee Rates

Back to [[plans/03-api-integration/overview]]

## Goal

Replace the hardcoded `MAKER_FEE_RATE = 0.0175` with dynamically fetched fee rates from the Series model and the `GET /series/fee_changes` endpoint. Different series may have different fee types and rates.

## Changes

### src/talos/models/market.py
- Enrich `Series` model with currently-dropped fields:
  - `fee_type: str = "quadratic_with_maker_fees"` — enum: `quadratic`, `quadratic_with_maker_fees`, `flat`, `fee_free`
  - `fee_multiplier: float = 0.0175`
  - `frequency: str = ""`
  - `volume: int | None = None` (from `volume_fp`)
  - `settlement_sources: list[dict] = []`

### src/talos/rest_client.py
- Add `get_fee_schedule(series_ticker: str, *, show_historical: bool = False) -> list[dict]` method
  - Calls `GET /series/fee_changes?series_ticker={ticker}`
  - Returns raw fee change list (effective_ts, fee_type, maker_fee_rate, taker_fee_rate)
  - Used for detecting upcoming fee changes; current rate comes from Series model

### src/talos/fees.py
- Change all functions that use `MAKER_FEE_RATE` to accept an optional `rate: float = 0.0175` parameter:
  - `quadratic_fee(no_price, *, rate=0.0175)`
  - `fee_adjusted_cost(no_price, *, rate=0.0175)`
  - `fee_adjusted_edge(no_a, no_b, *, rate=0.0175)`
  - `american_odds(no_price, *, rate=0.0175)`
- Add `flat_fee(no_price, *, rate)` for flat fee type
- Add `compute_fee(no_price, *, fee_type, rate)` dispatch function that selects formula by type
- Handle `fee_free` → returns 0

### src/talos/scanner.py
- Store fee rate per pair (from series)
- Pass rate to `fee_adjusted_edge()` when computing edge

### src/talos/game_manager.py
- When adding a game, fetch the series (already called for some games) and extract `fee_type` + `fee_multiplier`
- Store on the `ArbPair` or pass through to scanner

### src/talos/bid_adjuster.py
- Use pair's fee rate in profitability gate (P18) instead of hardcoded constant

## Data Structures

- `Series` gains: `fee_type: str`, `fee_multiplier: float`, `frequency: str`, `volume: int | None`, `settlement_sources: list`
- Fee functions gain `rate` kwarg (backward compatible default)

## Verification

### Static
- `pyright` passes
- `ruff` passes
- All existing callers of fee functions still work (default parameter)

### Runtime
- Unit test: `Series` model parses `fee_type` and `fee_multiplier` from API response
- Unit test: `quadratic_fee(45, rate=0.02)` vs `quadratic_fee(45)` — different results
- Unit test: `compute_fee` dispatches correctly for quadratic, flat, fee_free
- Unit test: scanner uses series-specific rate when computing edge
- Invoke `strategy-verify` skill — verify edge calculations with dynamic rates
- Manual test: add game from a series, verify fee rate is fetched and used (check structlog output)
