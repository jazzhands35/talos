# Phase 2 — Order Model Enrichment

Back to [[plans/03-api-integration/overview]]

## Goal

Capture `maker_fill_cost_dollars` and `taker_fill_cost_dollars` from order responses (fixing the known inaccurate fill cost calculation), and add `post_only` flag to order creation. These are fields already present in API responses that Talos currently drops.

## Changes

### src/talos/models/order.py
- Add `maker_fill_cost: int = 0` and `taker_fill_cost: int = 0` fields to `Order` model (cents)
- Add FP migration entries in `_migrate_fp` for `maker_fill_cost_dollars` and `taker_fill_cost_dollars`
- Add `fee_cost: int = 0` field to `Fill` model (cents) with migration for `fee_cost` (dollars string)
- Add `action: str = ""`, `is_taker: bool = False`, `purchased_side: str = ""` to `Fill` model

### src/talos/rest_client.py
- Add `post_only: bool = True` parameter to `create_order()`, include in request body
- No other REST changes needed — Order model automatically captures new fields from existing responses

### src/talos/position_ledger.py
- In `sync_from_orders()`, replace `order.no_price * order.fill_count` with `order.maker_fill_cost + order.taker_fill_cost` for fill cost calculation
- This fixes the known issue: "order.no_price * order.fill_count for fill cost may be inaccurate if order was amended at different prices"

## Data Structures

- `Order` gains: `maker_fill_cost: int`, `taker_fill_cost: int` (both cents, from `_dollars` strings)
- `Fill` gains: `fee_cost: int` (cents), `action: str`, `is_taker: bool`, `purchased_side: str`

## Verification

### Static
- `pyright` passes
- `ruff` passes

### Runtime
- Unit test: construct `Order` from dict with `maker_fill_cost_dollars: "4.4800"`, verify `maker_fill_cost == 448`
- Unit test: construct `Fill` from dict with `fee_cost: "0.0130"`, verify `fee_cost == 1` (rounds to nearest cent)
- Unit test: `sync_from_orders` uses `maker_fill_cost` instead of price × count — construct order with amended price, verify cost is exact
- Unit test: `create_order` includes `post_only: true` in request body
- Existing order/ledger tests still pass
- Invoke `position-scenarios` skill to verify fill cost accuracy scenarios
