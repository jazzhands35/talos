# Phase 12 — Order Groups (Server-Side Unit Enforcement)

Back to [[plans/03-api-integration/overview]]

## Goal

Implement Order Groups as a server-side belt-and-suspenders safety layer for unit-based bidding (P16). Kalshi auto-cancels orders when a group's fill count hits the limit, preventing overextension even if client-side `is_placement_safe()` somehow fails.

## Changes

### src/talos/rest_client.py
- Add Order Group methods:
  - `create_order_group(name: str, contracts_limit: int) -> str` — returns `order_group_id`
  - `delete_order_group(order_group_id: str) -> None`
  - `reset_order_group(order_group_id: str) -> None` — reset matched contracts counter
  - `trigger_order_group(order_group_id: str) -> None` — cancel all orders in group
  - `get_order_groups() -> list[dict]` — list active groups

### src/talos/models/order.py
- Add `order_group_id: str | None = None` to `Order` model (already returned by API, currently dropped)

### src/talos/engine.py
- When placing bids (`place_bids`), create an order group per side per unit:
  - Group name: `"{event_ticker}-{side}-unit-{N}"` (e.g., `"KXMLB-NYY-26-A-unit-1"`)
  - Contracts limit: `unit_size` (typically 10)
  - Pass `order_group_id` in `create_order()` call
- Track active groups: `_order_groups: dict[str, str]` — `"{event_ticker}-{side}" → order_group_id`
- On unit completion (both sides filled), groups are naturally exhausted
- On re-entry (next unit), create new groups with incremented unit number
- On rebalance cancel: if cancelling all resting on a side, consider triggering the group

### Rollout strategy
- Start with groups created but `is_placement_safe()` still active (belt AND suspenders)
- Log when a group limit prevents a placement (should never happen if `is_placement_safe()` works correctly)
- Any group-triggered cancellation is a sign of a client-side safety gap — alert operator loudly

## Data Structures

- `Order` gains: `order_group_id: str | None`
- Engine gains: `_order_groups: dict[str, str]`
- REST client gains: order group CRUD methods

## Verification

### Static
- `pyright` passes
- `ruff` passes

### Runtime
- Unit test: `create_order_group` sends correct request, returns group ID
- Unit test: `create_order` includes `order_group_id` in request body when provided
- Unit test: group is created per side per unit during `place_bids`
- Unit test: group ID tracked in `_order_groups` mapping
- Invoke `safety-audit` skill — this adds a safety layer to order placement
- Manual test: place bids with order group in demo, verify Kalshi dashboard shows group association
