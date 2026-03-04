# Decisions

Record significant technical decisions here.

## 2026-03-03 — Fresh start, no Autopilot code

**Context:** Previous Kalshi bot (Autopilot) exists but has architectural issues.
**Decision:** Build Talos from scratch. No code sharing with Autopilot.
**Consequences:** Slower initial setup, but cleaner architecture without legacy debt.

## 2026-03-03 — Python + Textual TUI

**Context:** Needed to choose language and UI approach.
**Decision:** Python 3.12+ with Textual for terminal UI.
**Rationale:** Python has the best trading ecosystem. Textual provides a rich dashboard without browser complexity. Function over form.

## 2026-03-03 — Drop TCH ruff rules

**Context:** Ruff's TCH (type-checking) rules (TC001, TC003) flag imports used in `__init__` parameter types and function bodies, suggesting they move to `TYPE_CHECKING` blocks. But these imports are used at runtime, not just for annotations.
**Decision:** Removed `"TCH"` from ruff lint select in `pyproject.toml`.
**Rationale:** False positives outweigh the benefit. With `from __future__ import annotations` already used, the TCH rules aggressively flag runtime imports that would break if moved.

## 2026-03-03 — Full API client in one pass

**Context:** Could have built auth-only, then markets, then orders incrementally across sessions.
**Decision:** Built the entire REST + WebSocket client (Layer 1) in a single session using subagent-driven development.
**Rationale:** All endpoints are structurally similar, and the plan was clear. Batching reduces context-switching overhead. 12 tasks, 67 tests, ~30 minutes wall clock.

## 2026-03-03 — Pure state + async orchestrator split for Layer 2

**Context:** OrderBookManager needs to apply snapshots/deltas (pure logic), while MarketFeed needs to subscribe via WS and route messages (async I/O).
**Decision:** Split into two classes — `OrderBookManager` (pure state machine, no async) and `MarketFeed` (async orchestrator, no state logic).
**Rationale:** Pure state machine is trivially testable without mocks. Async surface area stays minimal. The hot path (delta application) has zero framework overhead. Tests for each side are simpler and more focused.

## 2026-03-04 — Fee-agnostic scanner for Layer 3

**Context:** The scanner detects NO+NO arbitrage opportunities. Kalshi charges fees that reduce the real edge.
**Decision:** Scanner reports raw edges only. Fee calculation and filtering is a separate downstream concern.
**Rationale:** Keeps the scanner simple and reusable. Fee structures may change or vary by market. The operator or a downstream policy module can filter opportunities by net-of-fee profitability.

## 2026-03-04 — Reused pure state + async orchestrator for Layer 3

**Context:** Layer 3 has the same split: pure logic (edge detection) vs async I/O (REST fetch, WS subscribe).
**Decision:** `ArbitrageScanner` = pure state machine, `GameManager` = async orchestrator. Identical to the Layer 2 `OrderBookManager`/`MarketFeed` split.
**Rationale:** The pattern proved effective in Layer 2. Consistent architecture across layers reduces cognitive load. Scanner's 17 tests need zero mocks.

## 2026-03-03 — Combine adjacent subagent tasks on same files

**Context:** Tasks 2-4 in the Layer 2 plan all modified `orderbook.py` and `test_orderbook.py`.
**Decision:** Dispatched a single implementer subagent for all three tasks instead of three separate agents.
**Rationale:** Multiple agents writing to the same files would cause conflicts. Combining avoids file contention while keeping the spec review meaningful (review covers all three tasks' requirements).
