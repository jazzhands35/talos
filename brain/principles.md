# Principles

Core values that govern every decision in Talos — from architecture to code review to runtime behavior.

## 1. Safety Above All

Capital preservation is the top priority. When in doubt, **don't trade**.

- Code touching real money requires explicit confirmation paths
- Failures halt trading immediately — resume only with manual intervention
- Demo environment by default; production must be explicitly opted into
- No "clever" optimizations that sacrifice safety for speed

## 2. Human in the Loop

The system advises; the human decides. Automation earns trust incrementally.

- Start with full manual control, automate only what's proven
- When data is uncertain or conditions are unusual, **flag and wait** — never guess
- Every automated action must have a manual override
- Progressive automation: manual → assisted → supervised → autonomous (we're at manual)

## 3. Prove It Works

If it's not tested, it doesn't work. Compiling is not proof.

- Every behavior must have a test — no exceptions
- If something can't be tested, redesign it until it can
- Tests assert outcomes, not implementation details
- New behavior requires new tests before it merges

## 4. Subtract Before You Add

Simpler is better. Complexity is a cost, not a feature.

- Try removing complexity before introducing it
- No abstractions for one-time operations
- No speculative design for hypothetical futures
- Three similar lines > a premature abstraction
- When refactoring, ask: "if we built this from scratch, what would we build?"

## 5. Plan Then Build

Think before typing. Design up front, especially for anything risky.

- Money-touching and decision-making code gets a written plan before implementation
- Think through edge cases in the brain vault, not in the debugger
- Spike freely for UI and utilities, but redesign properly before merging

## 6. Boring and Proven

Clever code is a liability. Pick the well-known, battle-tested approach.

- Standard patterns over novel ones
- Well-maintained libraries over hand-rolled solutions
- If two approaches both work, pick the one a new contributor would understand fastest
- Surprises belong in the market, not in the code

## 7. Audit Everything

Every trade decision and system action must leave a trail.

- All order actions logged with full context (why, what, when, outcome)
- Structured logging — every line machine-parseable
- Decisions include the data that drove them, not just the result
- If something went wrong, the logs alone should explain why

## 8. Layered Observability

Clean surface, full depth available on demand.

- Default view: concise summary of system state and actionable items
- Drill-down: full detail on any component — connections, data, decisions, skipped opportunities
- Don't clutter the UI with noise, but never hide information the operator might need

## 9. Idempotency and Resilience

Operations should be safe to retry. Failures should be contained.

- Order operations are idempotent where possible
- On failure: stop, protect capital, alert the operator
- No automatic recovery for money-touching operations — human confirms the restart
- Reconnection and data recovery can be automatic for read-only paths

## 10. Correctness Over Speed

A missed opportunity is acceptable. A bad trade is not.

- Never skip validation or safety checks to reduce latency
- Optimize non-safety paths freely, but correctness is never the tradeoff
- If an arbitrage window closes because we were being careful, that's fine

## 11. Enforced Limits, Manual Override

Automation gets hard guardrails. The operator gets judgment calls.

- Automated actions are bound by code-enforced risk limits (max position size, max daily loss, etc.)
- Manual trading surfaces warnings at the same thresholds, but allows override
- Limit changes require a config change, not a runtime toggle — deliberate, not impulsive

## 12. Single Strategy, Done Well

Talos does one thing: cross-event NO+NO arbitrage. No plugin architecture, no strategy abstraction.

- Build for this specific strategy, not a generic framework
- Every design decision can assume the arbitrage context
- If scope expands later, refactor then — don't pay the abstraction tax now

## 13. Trust But Log

Kalshi is the source of truth, but we keep receipts.

- API responses are trusted beyond basic type/model validation
- Every response is logged with full payload for post-hoc analysis
- If something anomalous surfaces later, the logs should have the raw data to investigate

## 14. Clean Slate (Autopilot)

Build what's right, not what's familiar.

- No code or patterns carried from Autopilot — fresh architecture only
- Learn from past mistakes, but don't inherit past code
- Every module earns its place; nothing gets grandfathered in
