# Kalshi Fixed-Point Migration — SUPERSEDED

> **⚠️ This plan is superseded. Do not use it as an implementation guide.**
>
> The March 2026 first-pass migration described here shipped successfully for the *wire-compat* goal it aimed at, but the boundary-only strategy it prescribed — specifically `_fp → int(float(val))` and `_dollars → int cents` — is the direct source of two later-discovered money-path bugs:
>
> 1. **Fractional inventory loss** on `fractional_trading_enabled` markets (observed live 2026-04-21 on `KXTRUMPSAYNICKNAME-26JUL01-MARJ`: a 1.89-contract partial maker fill was truncated to 1 contract, inflating cost-basis avg from ~52¢ to 60¢).
> 2. **Silent sub-cent market drops** (e.g., DJT at 3.8¢/96.1¢ — the scanner's integer-cent edge gate discards these markets after rounding collapses their inside prices).
>
> The boundary-only strategy — keep cents/contracts internally, convert at the parser — is the wrong answer for these market shapes. The full-unit migration to basis points (`_bps`) for prices and fp100 for counts is specified in:
>
> **→ [docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md](../../../docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md)**
>
> That spec replaces this document as the active plan. The sections below are kept only as historical record of what shipped in March 2026.

---

## Historical record (what shipped in March 2026)

Back to [[plans/index]]

### What this migration did

On March 12, 2026, Kalshi removed all legacy integer fields from REST and WebSocket response payloads. Integer cents price fields (e.g., `yes_bid`, `no_price`) and integer count fields (e.g., `fill_count`, `remaining_count`) were replaced by:

- **`_dollars` fields** — string, dollar-denominated (e.g., `"0.52"` = 52 cents)
- **`_fp` fields** — string, fixed-point contracts (e.g., `"10.00"` = 10 contracts)

This broke every Pydantic model in Talos that parsed API responses. The migration described below added `model_validator(mode="before")` hooks to each affected model so the new wire fields populated the existing integer fields. It restored connectivity with Kalshi without changing Talos's internal representation.

### What shipped

- **Model validators** added to `Order`, `Fill`, `BatchOrderResult`, `Position`, `Market`, `Trade`, `OrderBookSnapshot`, `OrderBookDelta`, `TickerMessage`, `TradeMessage`, `FillMessage`, `UserOrderMessage`.
- **REST client request payloads** updated: `create_order` / `amend_order` send `_dollars` / `_fp` strings via `f"{x / 100:.2f}"` formatting.
- **REST orderbook response key** handling updated from `data["orderbook"]` to accept both `data["orderbook"]` and `data["orderbook_fp"]`.
- **Test fixtures** regenerated in the new `_dollars` / `_fp` format across `tests/fixtures/`.

### Phase files (historical)

- [[plans/02-kalshi-fp-migration/phase-1-market-models]] — Market, Trade, OrderBook validators
- [[plans/02-kalshi-fp-migration/phase-2-portfolio-models]] — Order, Fill, Position validators
- [[plans/02-kalshi-fp-migration/phase-3-ws-models]] — OrderBookSnapshot, OrderBookDelta, TickerMessage, TradeMessage
- [[plans/02-kalshi-fp-migration/phase-4-rest-requests]] — Request payloads + response key handling
- [[plans/02-kalshi-fp-migration/phase-5-test-migration]] — Update test fixtures, verify full suite

### Field mapping reference (for reading this era's code)

**Dollars → Cents (prices, fees, costs):** `yes_bid`, `yes_ask`, `no_bid`, `no_ask`, `last_price`, `yes_price`, `no_price`, `taker_fees`, `maker_fees`, `total_traded`, `market_exposure` — each gained a `_dollars` wire field, parsed to the existing integer cents field.

**FP → Int (counts, quantities, positions):** `fill_count`, `remaining_count`, `initial_count`, `volume`, `open_interest`, `count`, `position` — each gained a `_fp` wire field, parsed via `int(float(val))` to the existing integer contracts field.

**⚠️ The `int(float(val))` conversion is the truncation bug the later migration reverses.** It is load-bearing in this historical plan but must not be preserved going forward.

### What was explicitly declared out of scope at the time (both reversed 2026-04-21)

- ~~Fractional trading support (Talos uses whole contracts only)~~ — reversed. See supersession spec.
- ~~Subpenny pricing support (not relevant to current strategy)~~ — reversed. See supersession spec.
