# Supervised Automation Design

**Date:** 2026-03-09
**Status:** Approved
**Goal:** Move Talos from "assisted" to "supervised" automation — the system proposes decisions, the human approves/rejects before execution.

## Architecture Overview

### New Components

1. **`ProposalQueue`** (pure state machine, no I/O) — Holds pending proposals, handles add/supersede/expire/approve/reject. Single source of truth for what's awaiting decision. Lives in `src/talos/proposal_queue.py`.

2. **`ProposalPanel`** (Textual widget) — Collapsible right sidebar that renders the queue. Shows each proposal as a compact row with details + approve/reject keybindings. Slides in when proposals exist, collapses when empty. Lives in `src/talos/ui/`.

3. **`OpportunityProposer`** (pure decision logic, no I/O) — Watches scanner output + ledger state, applies edge threshold + stability filter, emits bid proposals. Analogous to how BidAdjuster emits adjustment proposals. Lives in `src/talos/opportunity_proposer.py`.

### Data Flow

```
Jump detected → BidAdjuster.evaluate_jump() → ProposedAdjustment → ProposalQueue → ProposalPanel
                                                                                     ↓
Scanner edge  → OpportunityProposer.evaluate() → ProposedBid → ProposalQueue →  [approve] → Engine.place_bids()
                                                                                     ↓
                                                                       [approve] → Engine.approve_adjustment()
```

**Key principle:** ProposalQueue is the single choke point. Nothing executes without passing through it. The engine never auto-executes — it only acts on proposals that the queue marks as approved.

## ProposalQueue (Pure State Machine)

### Responsibilities

- Store pending proposals (adjustments and bids) keyed by `(event_ticker, side, proposal_type)`
- Supersede: new proposal on same key replaces the old one
- Staleness: on each `tick()`, check if proposals are still valid (order still exists, price hasn't moved again). Mark stale proposals, auto-remove after configurable grace period (default 5s)
- Expose ordered list for UI rendering

### Interface

```python
class ProposalQueue:
    def add(self, proposal: Proposal) -> None          # add or supersede
    def approve(self, key: ProposalKey) -> Proposal     # pop and return for execution
    def reject(self, key: ProposalKey) -> None          # remove
    def tick(self, current_orders: list[Order]) -> None  # staleness sweep
    def pending(self) -> list[Proposal]                  # ordered list for UI
```

### Unified Proposal Model

```python
class Proposal(BaseModel):
    key: ProposalKey                              # (event_ticker, side, type)
    kind: Literal["adjustment", "bid"]
    summary: str                                  # one-line human-readable
    detail: str                                   # full context (before/after/safety)
    created_at: datetime
    stale: bool = False                           # set by tick()
    stale_since: datetime | None = None
    # Payload — one of:
    adjustment: ProposedAdjustment | None = None
    bid: ProposedBid | None = None
```

Wraps the existing `ProposedAdjustment` rather than replacing it — BidAdjuster keeps generating them, ProposalQueue wraps them in a `Proposal` envelope.

## ProposalPanel (Collapsible UI)

### Behavior

- Hidden when `proposal_queue.pending()` is empty
- Slides in from the right when proposals arrive, collapses when queue empties
- Each proposal renders as a compact row:
  ```
  [1] ADJ  EVTA-123  A  47→48c  "arb 99.2c"     [Y]approve [N]reject
  [2] BID  EVTB-456     edge 2.1c (5s stable)    [Y]approve [N]reject
  ```
- Stale proposals show dimmed/strikethrough before auto-removal
- Keyboard: number key selects proposal, `y` approves selected, `n` rejects selected. `Y` approves all, `N` rejects all.

### Layout

- Textual widget docked right
- Width: ~40-50 chars
- Stacks vertically if multiple proposals pending
- Visual indicator on new proposal arrival

### Integration with TalosApp

- App mounts `ProposalPanel` at startup (hidden)
- Engine's polling cycle calls `proposal_queue.tick()` to sweep staleness
- On approve: App calls `engine.approve_adjustment()` or `engine.place_bids()` depending on `kind`
- On reject: App calls `proposal_queue.reject()`

## OpportunityProposer (Initial Bid Automation)

### Decision Logic (all must pass)

1. **Edge threshold** — fee-adjusted edge > configurable min (default 1.5c)
2. **Position gate** — event doesn't already have a full unit resting on both sides (via PositionLedger)
3. **Stability filter** — edge continuously above threshold for N seconds (default 5s). Tracked as `{event_ticker: first_seen_at}`. Timer resets if edge drops.
4. **No pending proposal** — don't propose if queue already has unapproved bid for this event
5. **Cooldown** — don't re-propose within N seconds of a rejection (default 30s)

### Output

```python
class ProposedBid(BaseModel):
    event_ticker: str
    ticker_a: str
    ticker_b: str
    no_a: int
    no_b: int
    qty: int
    edge_cents: float
    stable_for_seconds: float
    reason: str
```

Pure state machine — receives scanner snapshots and ledger state, returns proposals or None. No I/O, no async.

## Configuration

```python
@dataclass
class AutomationConfig:
    edge_threshold_cents: float = 1.5
    stability_seconds: float = 5.0
    staleness_grace_seconds: float = 5.0
    rejection_cooldown_seconds: float = 30.0
    unit_size: int = 10
    enabled: bool = False  # opt-in, off by default
```

## Safety Invariants

- Nothing executes without human approval (Principle 2)
- All existing safety gates remain — ProposalQueue is an additional layer, not a replacement
- BidAdjuster still checks profitability (P18), most-behind-first (P19), unit limits (P16)
- OpportunityProposer checks edge + position + stability
- Execution still goes through engine methods that consult PositionLedger (P15)
- Automation off by default, explicit opt-in required
- Demo environment only until manually switched to production

### Failure Modes

| Failure | Response |
|---------|----------|
| Approve stale proposal | Execution detects staleness, returns error notification, proposal removed |
| Queue fills faster than human can review | Superseding handles same-event duplicates; oldest auto-expire via staleness |
| Network error during execution | Existing engine error handling — halt, notify, don't retry (P9) |
| Proposer bugs | All proposals show full arithmetic in `detail` — human verifies before approving |

## Testing Strategy

### Pure state machines (zero mocks)

- `ProposalQueue` — add/supersede/expire/approve/reject lifecycle, staleness sweep, key collision
- `OpportunityProposer` — edge threshold, stability filter, position gate, cooldown, no-duplicate

### Integration (minimal mocks)

- Engine + ProposalQueue wiring — BidAdjuster proposal flows into queue, approve triggers engine action
- Staleness sweep during `refresh_account` cycle

### UI (Textual `run_test()`)

- ProposalPanel renders/hides based on queue state
- Keyboard bindings dispatch correct actions
- Stale visual treatment

## Implementation Phases

- **Phase 1:** ProposalQueue + ProposalPanel + wire BidAdjuster adjustments through queue
- **Phase 2:** OpportunityProposer + ProposedBid model + wire into same queue
