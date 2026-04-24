# Phase 7 — Slim TalosApp

Back to [[plans/01-trading-engine-extraction/overview]]

## Goal

Rewrite `TalosApp` to delegate to `TradingEngine`. Remove all logic that was moved in phases 5-6. The app becomes a thin UI shell.

## Changes

### `src/talos/ui/app.py`

Rewrite constructor to accept `TradingEngine | None` (plus `scanner` for test mode):

```
def __init__(self, *, engine: TradingEngine | None = None, scanner: ArbitrageScanner | None = None)
```

- `compose()` — unchanged
- `on_mount()` — set up timers that call engine methods, wire engine callbacks to Textual notifications
- `refresh_opportunities()` — reads `engine.scanner` and `engine.tracker` for table refresh
- All polling methods (`refresh_account`, etc.) become one-line delegations to engine
- Action methods become one-line delegations to engine
- Remove: `_merge_queue`, `_queue_cache`, `_orders_cache`, `_cpm`, `_enrich_with_cpm`, `_active_market_tickers`, `_on_top_of_market_change`
- Remove imports no longer needed: `CPMTracker`, `MarketFeed`, `GameManager`, `PositionLedger`, `Side`, `compute_display_positions`, `TopOfMarketTracker`, `BidAdjuster`

Target: ~150 lines.

### `tests/test_ui.py`

Update test instantiations:
- Tests that only need scanner continue using `TalosApp(scanner=scanner)` — test mode preserved
- Tests that need full behavior construct a `TradingEngine` and pass it
- Remove tests that tested polling logic directly (now covered by `test_engine.py`)

## Verification

### Static
- `pyright` passes
- `ruff check` passes
- `TalosApp` is <=150 lines

### Runtime
- `pytest tests/test_ui.py` — all UI tests pass
- `pytest tests/test_engine.py` — engine tests still pass
- `pytest` — full suite passes
