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
- Progressive automation: manual → assisted → supervised → autonomous (we're at manual)

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
