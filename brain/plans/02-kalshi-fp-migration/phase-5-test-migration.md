# Phase 5: Test Migration & Verification

Back to [[plans/02-kalshi-fp-migration/overview]]

## Goal

Update all test fixtures and mock data to use the new API field names. Verify the full test suite passes. Confirm backward compatibility with old-format data (some tests may intentionally use old format to verify the validators handle both).

## Changes

### Test files to update

Grep all test files for old field names used in mock API responses:
- `yes_price`, `no_price` (in order/fill/trade mocks)
- `fill_count`, `remaining_count`, `initial_count` (in order mocks)
- `yes_bid`, `no_bid`, `yes_ask`, `no_ask` (in market mocks)
- `position`, `total_traded`, `market_exposure` (in position mocks)
- Orderbook level arrays with int values

For each mock, create the new-format version. Keep at least one test per model that verifies old-format backward compatibility.

### Test categories

1. **Model parsing tests** — verify each model correctly parses new-format data
2. **REST client tests** — verify request payloads use new field names
3. **Engine tests** — verify sync_from_orders/sync_from_positions work with new-format Order/Position objects
4. **Orderbook tests** — verify snapshot/delta parsing with new format
5. **Integration tests** — end-to-end from mock API response through model parsing to engine state

### What NOT to change

- Test logic, assertions, expected values — only the mock input format changes
- Any test that doesn't mock Kalshi API data — leave as-is
- Engine/scanner/ledger logic tests that work with already-parsed models — leave as-is

## Verification

### Static
- `ruff check tests/`
- Full test suite: `pytest tests/ -x` — zero failures

### Runtime
- Start Talos against demo: `python -m talos`
- Verify opportunities table populates with correct prices
- Verify position display matches Kalshi portfolio
- Place a test order, verify it appears in order log with correct price/count
- Verify WebSocket orderbook updates flow (prices change in real-time)
