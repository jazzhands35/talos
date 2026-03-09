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

## 5. Plan Then Build

Money-touching and decision-making code gets a written plan before implementation. Spike freely for UI and read-only utilities; any code that triggers I/O with side effects (order placement, subscription management) requires a plan even if initiated from the UI layer.

## 6. Boring and Proven

Pick the well-known, battle-tested approach. Standard patterns over novel ones. If two approaches both work, pick the one a new contributor would understand fastest.

## 7. Audit Everything, Trust Kalshi

Every trade decision and system action must leave a trail. Kalshi is the source of truth, but we keep receipts.

- All order actions logged with full context (why, what, when, outcome)
- Structured logging — every line machine-parseable
- Centralize logging in I/O boundaries (`_request`, callback dispatch) — full coverage, minimal per-site code
- API responses are trusted beyond basic type/model validation, but logged with full payload for post-hoc analysis
- "Trust" means not re-verifying Kalshi's matching engine logic. Domain-level sanity checks (price range 1-99, qty > 0) are always applied at the Pydantic model layer

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

## 15. Position Awareness Before Action

No automated or semi-automated action may be taken without a complete, verified picture of current and projected positions. The system must always be able to answer: "What do I hold right now? What will I hold if these resting orders fill?"

A previous system failed catastrophically because it lost track of filled orders and resting order counts simultaneously. It placed new bids without knowing what it already had, leading to runaway exposure on one side. **This is the failure mode we are engineering against.**

- The position model is the single source of truth — no component may place, amend, or cancel orders without consulting it
- Position means: filled contracts, resting contracts, and the sum of both, tracked per side per event
- The model must project future states: "if resting batch X fills, my position becomes P2; if Y also fills, it becomes P3"
- When data sources disagree (e.g., fill count from polling vs. expected from placement), halt and flag — never guess

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
