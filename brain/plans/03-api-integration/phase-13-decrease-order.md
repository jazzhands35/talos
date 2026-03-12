# Phase 13 — Decrease Order Endpoint

Back to [[plans/03-api-integration/overview]]

## Goal

Add `POST /portfolio/orders/{id}/decrease` as a cleaner alternative to amend for quantity-only reductions. Preserves queue position and has simpler semantics than amend.

## Changes

### src/talos/rest_client.py
- Add `decrease_order(order_id: str, *, reduce_by: int | None = None, reduce_to: int | None = None) -> Order`:
  - Exactly one of `reduce_by` or `reduce_to` must be provided
  - Send as `reduce_by_fp` / `reduce_to_fp` (string format)
  - Returns updated Order

### src/talos/engine.py
- In `_execute_rebalance()`: when reducing resting count without changing price, use `decrease_order` instead of `amend_order`
  - If `target_resting == 0`: still use `cancel_order` (cleaner intent)
  - If `target_resting > 0 and target_resting < current_resting`: use `decrease_order(reduce_to=target_resting)`
  - Remove the amend path for quantity-only reductions
- Benefit: no need to pass ticker, side, action, price just to change quantity

## Data Structures

No new types. `decrease_order` returns existing `Order` model.

## Verification

### Static
- `pyright` passes
- `ruff` passes

### Runtime
- Unit test: `decrease_order(order_id, reduce_to=5)` sends correct request body with `reduce_to_fp: "5"`
- Unit test: `decrease_order(order_id, reduce_by=3)` sends `reduce_by_fp: "3"`
- Unit test: rebalance uses `decrease_order` for quantity reduction, `cancel_order` for full cancel
- Existing rebalance tests still pass
- Invoke `safety-audit` skill — this modifies an order mutation path
