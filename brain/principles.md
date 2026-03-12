# Principles

Core values that govern every decision in Talos — from architecture to code review to runtime behavior.

## 1. Safety Above All

Capital preservation is the top priority. When in doubt, **don't trade**.

- Code touching real money requires explicit confirmation paths
- Failures halt trading immediately — resume only with manual intervention
- Demo environment by default; production must be explicitly opted into

## 2. Human in the Loop

The system advises; the human decides. Automation earns trust incrementally.

- Start with full manual control, automate only what's proven
- When data is uncertain or conditions are unusual, **flag and wait** — never guess
- Progressive automation: manual → assisted → supervised → autonomous (we're at supervised — ProposalQueue with operator approve/reject)

## 3. Prove It Works

If it's not tested, it doesn't work. Every behavior must have a test.

- Tests assert outcomes, not implementation details
- New behavior requires new tests before it merges
- If something can't be tested, redesign it until it can

## 4. Subtract Before You Add

Simpler is better. Try removing complexity before introducing it.

- No abstractions for one-time operations; three similar lines > a premature abstraction
- No speculative design for hypothetical futures
- **Exception:** Architectural splits that enable mock-free testing (see Principle 13) are not premature abstraction — they are structural correctness
- **Exception:** Observability mechanisms required by Principle 20 (hold proposals, visible inaction) are not unnecessary complexity — operator trust requires visible decision outcomes

## 5. Plan Then Build

Money-touching and decision-making code gets a written plan before implementation. Spike freely for UI and read-only utilities; any code that triggers I/O with side effects (order placement, subscription management) requires a plan even if initiated from the UI layer.

## 6. Boring and Proven

Pick the well-known, battle-tested approach. Standard patterns over novel ones. If two approaches both work, pick the one a new contributor would understand fastest.

## 7. Kalshi Is the Source of Truth — Always

Kalshi's API is the single, unconditional source of truth for all position and order state. Talos never computes, predicts, or caches position state as a substitute for asking Kalshi. Every trade decision and system action must leave a trail.

- **Positions and resting orders come from Kalshi, period.** No local computation may override, substitute, or bypass what Kalshi reports. See Principle 15
- All order actions logged with full context (why, what, when, outcome)
- Structured logging — every line machine-parseable
- Centralize logging in I/O boundaries (`_request`, callback dispatch) — full coverage, minimal per-site code
- API responses are trusted beyond basic type/model validation, but logged with full payload for post-hoc analysis
- "Trust" means not re-verifying Kalshi's matching engine logic. Structural validation (field names, types, response shape) is always enforced at the Pydantic boundary per Principle 14
- Domain-level sanity checks (price range 1-99, qty > 0) are always applied at the Pydantic model layer

## 8. Layered Observability

Default view: concise summary of system state. Drill-down: full detail on any component. Don't clutter the UI with noise, but never hide information the operator might need.

## 9. Idempotency and Resilience

Operations should be safe to retry. Failures should be contained.

- Order operations are idempotent where possible
- On failure: stop, protect capital, alert the operator
- No automatic recovery for money-touching operations — human confirms the restart
- Reconnection and data recovery can be automatic for read-only paths

## 10. Correctness Over Speed

A missed opportunity is acceptable. A bad trade is not. Never skip validation or safety checks to reduce latency.

## 11. Enforced Limits, Manual Override

- Automated actions are bound by code-enforced risk limits (max position size, max daily loss, etc.)
- Manual trading surfaces warnings at the same thresholds, but allows override
- Limit changes require a config change, not a runtime toggle

## 12. Single Strategy, Done Well

Talos does one thing: cross-event NO+NO arbitrage. Build for this specific strategy, not a generic framework. If scope expands later, refactor then.

## 13. Test Purity Drives Architecture

When designing a module, split it until the core logic can be tested with zero mocks.

- Separate pure state machines from async orchestrators at the I/O boundary
- The pure side receives data, updates state, answers queries — no imports of httpx, websockets, or asyncio
- The orchestrator side owns I/O lifecycle and routes data to the pure side
- See [[patterns#Pure state + async orchestrator split]] for implementation details

## 14. Parse at the Boundary

Raw data is converted to domain types at the system boundary (API response parsing, WS message dispatch). Interior code never handles raw formats.

- Model validators convert API quirks (raw arrays, string enums) at parse time
- REST methods return Pydantic models, never raw dicts
- If business logic needs to interpret a raw format, the boundary is in the wrong place
- Verify model schemas against actual API responses, not documentation alone — mock-based tests can't catch schema drift

## 15. Position Accuracy Is Non-Negotiable

**Talos must have a 100% accurate picture of positions and resting orders at all times. Without this, every suggestion, safety gate, and action is based on a lie.**

Kalshi is the single source of truth for position state — always, unconditionally, without exception. Talos does not compute, predict, or assume what positions exist. It asks Kalshi, and it trusts the answer. Every code path that reads or acts on position data must ultimately trace back to data fetched from Kalshi's API.

- No automated or semi-automated action may be taken without a complete, verified picture of current and projected positions
- The system must always be able to answer: "What do I hold right now? What will I hold if these resting orders fill?"
- Position means: filled contracts, resting contracts, and the sum of both, tracked per side per event
- When data sources disagree (e.g., fill count from polling vs. expected from placement), halt and flag — never guess
- Before any money-touching action (placement, amend, cancel, rebalance), re-fetch from Kalshi and re-verify. Stale data has caused runaway exposure in production
- If Talos cannot fetch fresh position data, it must not act. A missed opportunity is acceptable; an action based on stale state is not

**Failure mode this prevents:** A previous system lost track of positions and placed bids without knowing what it already held, causing runaway exposure. A later bug trusted stale ledger data for catch-up rebalances, escalating corrections on each cycle (A went from 20 to 50 contracts). Both failures had the same root cause: acting on data that didn't reflect Kalshi's actual state.

## 16. Delta Neutral by Construction

Talos must maintain balanced exposure across both sides of every event. This is enforced structurally, not by hope.

**Why delta neutral:** In NO+NO arbitrage, profit comes from `price_A + price_B < 100`. If one side fills and the other doesn't, you're holding a naked position that can go to zero. The arb is only an arb when both sides are filled equally.

- **Unit-based bidding:** Orders are placed in atomic units (currently 10 contracts). A "pair" is one unit on side A and one unit on side B
- **One pair at a time:** A new pair cannot deploy until the previous pair is completely filled on both sides. "Completely filled" means the full unit — 9 out of 10 is not complete
- **Resting + filled ≤ 1 unit per side:** At no point should a side have more than one unit's worth of contracts either resting or in the process of being placed. This is a hard gate, not a soft check
- **Fractional completion is allowed:** If a unit partially fills and the price jumps, a fractional bid may be placed to complete the unit — never to exceed it

**Why these rules exist:** The previous system's cascade failure happened because it could place new bids faster than it could verify position state. Each check cycle saw "I don't have enough on this side" and placed more, not realizing previous placements hadn't been accounted for yet. Unit-based gating with hard limits makes this structurally impossible.

## 17. Amend, Don't Cancel-and-Replace

When adjusting a resting bid (e.g., following a price jump), use the Kalshi amend API (`POST /portfolio/orders/{id}/amend`) to change the price in a single atomic operation. Never cancel and re-place as separate steps.

**Why amend is safer:** Amend is a single API call that changes the price on an existing order. There is never a moment where two orders exist on the same side, and never a moment where zero orders exist. The previous system used cancel-then-place, which created timing windows that caused cascade failures.

**Partial fill behavior:** For a partially filled order (e.g., 6 filled, 4 remaining at 48c), amend moves only the unfilled portion (4 contracts) to the new price queue. Filled contracts are unaffected. Pass `count = fill_count + remaining_count` (the original total) to keep the same quantity.

**Fallback:** If amend fails (API error, network issue), halt and flag the operator. Do not fall back to cancel-then-place — the risk of doubles is not worth the recovery attempt.

## 18. Profitable Arb Gate

No bid may be placed or amended unless the fee-adjusted arb remains profitable. Specifically: `avg_price_A + proposed_price_B < 100 cents (after fees)`.

**Why:** When a price jumps, the natural instinct is to follow it to maintain queue position. But following a jump into unprofitable territory locks in a guaranteed loss. If the market moves to a price where no profitable arb exists, the correct action is to wait — not to chase.

**Why fee-adjusted:** At small spreads or with fractional-cent averages from partial fills at different prices, fees can turn an apparently profitable arb into a loss. Computing fees is cheap; eating a loss is not.

## 19. Most-Behind-First on Dual Jumps

When both sides of a pair have partial fills and both get jumped, the side with **more remaining contracts** adjusts first. The other side waits until the first side's unit is complete.

**Why:** This minimizes worst-case imbalance. Example: Side A needs 7 more, Side B needs 4 more. If B adjusts first and fills, you're 10 contracts on B vs. 3 on A — an imbalance of 7. If A adjusts first and fills, you're 10 on A vs. 6 on B — an imbalance of 4. Letting the further-behind side catch up first keeps the delta tighter in every scenario.

**Why only one at a time:** If both sides adjust simultaneously and both fill at the new (worse) prices, you may end up with a pair where `avg_A + avg_B ≥ 100` — an unprofitable arb created by racing adjustments. Sequential adjustment lets each step re-verify profitability before the next.

## 20. Inaction Is a Decision — Make It Visible

When the system evaluates a market change and decides not to act, that decision must be surfaced to the operator with a reason. Silent non-action is indistinguishable from a broken system.

- Every evaluation path that could result in action must produce a visible outcome — either "do X" or "hold because Y"
- The operator should never have to wonder whether the system saw a change, failed to process it, or deliberately chose inaction
- This applies to jump evaluations, opportunity scanning, and any future automated decision point

**Why:** A jumped order with no proposal looks identical to a system that crashed, lost its WebSocket, or has a bug. The operator can't trust the system unless non-action is as transparent as action. Evidence: the `sync_from_orders` discrepancy bug silently blocked all proposals — indistinguishable from "working correctly, nothing to do."

## 21. Authoritative Data Over Computed Data

When an authoritative source provides exact values (e.g., Kalshi's `maker_fees` on orders, `fee_cost` on fills), prefer those over computing them from formulas — even if the formula is correct.

- Computed values are susceptible to rounding, formula drift, and parameter staleness
- API-provided actuals reflect edge cases the formula may not model (rounding modes, fee rebates, accumulator adjustments)
- Use formulas for projections and estimates; use actuals for P&L and settled amounts
- Applied in: `sync_from_orders` uses `order.maker_fees` for fee tracking, not `quadratic_fee(price)`. See [[decisions#2026-03-09 — Quadratic fee model and fill-time charging]]

## 22. End-to-End Before Done

A feature is not complete until the operator can trigger it, see it, and act on it — from input to execution. Every feature must ship with its full interaction chain: activation path → visible output → actionable response → execution.

**Why:** Three separate features shipped incomplete because only the "detection" or "logic" layer was built without wiring the operator's ability to use it:
1. **Suggestion mode** — proposer logic existed, but no keybinding to enable it
2. **Proposal approval** — proposals appeared in the UI, but approving a bid proposal had no execution path
3. **Rebalance detection** — imbalance proposals appeared, but approving them only dismissed the notification without acting

Each required a follow-up fix to wire the missing segment. The pattern is always the same: the interesting logic gets built, but the boring plumbing that connects it to the operator gets deferred or forgotten.

**Checklist for every feature touching operator interaction:**
- [ ] Can the operator activate/trigger this? (keybinding, toggle, config)
- [ ] Does the operator see the result? (toast, panel, table column, log)
- [ ] Can the operator respond to it? (approve, reject, dismiss, override)
- [ ] Does the response execute? (API call, state change, order mutation)

If any box is unchecked, the feature is not done.
