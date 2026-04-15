# Portfolio Panel Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the confusing portfolio panel (broken P&L, overlapping terms) with a clear two-section layout: Account (Cash/Matched/Partial/Locked/Exposure) and Coverage (Events/Positions/Bidding/Unentered).

**Architecture:** All data already exists in `EventPositionSummary`. The widget gets new fields and render layout; `app.py:refresh_opportunities` gets a new aggregation loop; `position.py` gets a `unit_size` field. No engine changes.

**Tech Stack:** Python, Textual (Static widget), Pydantic v2, pytest

**Spec:** `docs/superpowers/specs/2026-03-27-portfolio-panel-redesign.md`

---

### Task 1: Add `unit_size` to `EventPositionSummary`

**Files:**
- Modify: `src/talos/models/position.py:24-37`
- Modify: `src/talos/position_ledger.py:655-689` (constructor call)
- Test: `tests/test_position_ledger.py`

- [ ] **Step 1: Add field to model**

In `src/talos/models/position.py`, add `unit_size` to `EventPositionSummary`:

```python
class EventPositionSummary(BaseModel):
    """Matched-pair P&L summary for one event's arb position."""

    event_ticker: str
    leg_a: LegSummary
    leg_b: LegSummary
    matched_pairs: int
    locked_profit_cents: float
    unmatched_a: int
    unmatched_b: int
    exposure_cents: int
    unit_size: int = 10
    status: str = ""
    kalshi_pnl: int | None = None
```

- [ ] **Step 2: Pass `unit_size` from ledger in `compute_display_positions`**

In `src/talos/position_ledger.py`, in the `EventPositionSummary(...)` constructor call (~line 656), add:

```python
                unit_size=ledger.unit_size,
```

After the `exposure_cents=exposure,` line.

- [ ] **Step 3: Run tests to verify nothing breaks**

Run: `.venv/Scripts/python -m pytest tests/test_position_ledger.py -x -v`
Expected: All pass (new field has a default value, so existing tests are unaffected).

- [ ] **Step 4: Commit**

```bash
git add src/talos/models/position.py src/talos/position_ledger.py
git commit -m "feat: add unit_size to EventPositionSummary for matched-unit aggregation"
```

---

### Task 2: Rewrite `PortfolioPanel` widget

**Files:**
- Modify: `src/talos/ui/widgets.py:732-813`

- [ ] **Step 1: Replace the entire `PortfolioPanel` class**

Replace lines 732-813 of `src/talos/ui/widgets.py` with:

```python
class PortfolioPanel(Static):
    """Portfolio summary: account state and event coverage."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self._cash: int = 0
        self._portfolio: int = 0
        self._matched: int = 0
        self._partial: int = 0
        self._locked: float = 0.0
        self._exposure: int = 0
        self._events: int = 0
        self._with_positions: int = 0
        self._bidding: int = 0
        self._unentered: int = 0

    def on_mount(self) -> None:
        self.border_title = "Portfolio"

    def render(self) -> str:
        """Compute content each frame — bypasses Static.update() entirely."""
        cash = f"${self._cash / 100:,.2f}"
        locked = f"${self._locked / 100:,.2f}"
        exposure = f"${self._exposure / 100:,.2f}"
        return (
            f"Cash:       {cash}\n"
            f"Matched:    {self._matched} units\n"
            f"Partial:    {self._partial} events\n"
            f"Locked In:  {locked}\n"
            f"Exposure:   {exposure}\n"
            f"───────────────────\n"
            f"Events:       {self._events}\n"
            f"w/ Positions: {self._with_positions}\n"
            f"Bidding:      {self._bidding}\n"
            f"Unentered:    {self._unentered}"
        )

    def update_balance(self, balance_cents: int, portfolio_cents: int) -> None:
        self._cash = balance_cents
        self._portfolio = portfolio_cents
        self.refresh()

    def update_account(
        self,
        matched: int,
        partial: int,
        locked: float,
        exposure: int,
    ) -> None:
        self._matched = matched
        self._partial = partial
        self._locked = locked
        self._exposure = exposure
        self.refresh()

    def update_coverage(
        self,
        events: int,
        with_positions: int,
        bidding: int,
        unentered: int,
    ) -> None:
        self._events = events
        self._with_positions = with_positions
        self._bidding = bidding
        self._unentered = unentered
        self.refresh()
```

- [ ] **Step 2: Run lint**

Run: `.venv/Scripts/python -m ruff check src/talos/ui/widgets.py`
Expected: Clean.

- [ ] **Step 3: Commit**

```bash
git add src/talos/ui/widgets.py
git commit -m "feat: rewrite PortfolioPanel with Account + Coverage sections"
```

---

### Task 3: Update `app.py` — wire new panel methods

**Files:**
- Modify: `src/talos/ui/app.py:580-593` (`refresh_opportunities`)
- Modify: `src/talos/ui/app.py:137` (remove `_poll_settlements` immediate call)
- Modify: `src/talos/ui/app.py:456-468` (remove P&L aggregation from `_poll_settlements`)

- [ ] **Step 1: Replace the summation block in `refresh_opportunities`**

In `src/talos/ui/app.py`, replace lines 580-593 (the `# Push portfolio summaries` block) with:

```python
            # Push portfolio summaries
            panel = self.query_one(PortfolioPanel)
            summaries = self._engine.position_summaries
            total_matched_units = 0
            total_partial_events = 0
            total_locked = 0
            total_exposure = 0
            with_positions = 0
            bidding = 0

            for s in summaries:
                filled = s.leg_a.filled_count + s.leg_b.filled_count
                resting = s.leg_a.resting_count + s.leg_b.resting_count
                matched = s.matched_pairs

                total_matched_units += matched // s.unit_size if s.unit_size > 0 else 0
                total_locked += s.locked_profit_cents
                total_exposure += s.exposure_cents

                if filled > 0:
                    with_positions += 1
                    if not (
                        matched > 0
                        and matched % s.unit_size == 0
                        and s.leg_a.filled_count == s.leg_b.filled_count
                    ):
                        total_partial_events += 1
                elif resting > 0:
                    bidding += 1

            total_events = len(self._scanner.pairs) if self._scanner else 0
            unentered = total_events - with_positions - bidding

            panel.update_account(total_matched_units, total_partial_events, total_locked, total_exposure)
            panel.update_coverage(total_events, with_positions, bidding, unentered)
```

- [ ] **Step 2: Remove P&L aggregation from `_poll_settlements`**

In `_poll_settlements` (~line 456-468), remove only the P&L aggregation block. Keep the settlement cache population. The block to remove starts at `# Aggregate from cache` and ends at the `panel.update_pnl(...)` call. Specifically, delete:

```python
            # Aggregate from cache (complete history) — REST limit=200
            # returns a sliding window that drops older settlements as new
            # ones arrive, causing yesterday/7d totals to fluctuate.
            if cache is not None:
                agg = aggregate_settlements(cache.all_settlements())
            else:
                agg = aggregate_settlements([s.model_dump() for s in settlements])
            panel = self.query_one(PortfolioPanel)
            panel.update_pnl(
                today=agg["today_pnl"],
                yesterday=agg["yesterday_pnl"],
                last_7d=agg["week_pnl"],
                invested_today=agg["today_invested"],
                invested_yesterday=agg["yesterday_invested"],
                invested_7d=agg["week_invested"],
            )
```

Also remove the `aggregate_settlements` import from the `from talos.settlement_tracker import` line if it's now unused (keep `reconcile_event`).

- [ ] **Step 3: Remove immediate `_poll_settlements` call on mount**

In `on_mount` (~line 137), remove:
```python
            self._poll_settlements()  # fetch settlement P&L immediately
```

The settlement cache will still be populated by the 5-minute timer for the History screen.

- [ ] **Step 4: Run lint**

Run: `.venv/Scripts/python -m ruff check src/talos/ui/app.py`
Expected: Clean (or fix any unused import warnings).

- [ ] **Step 5: Commit**

```bash
git add src/talos/ui/app.py
git commit -m "feat: wire new portfolio panel methods, remove P&L aggregation"
```

---

### Task 4: Rewrite `test_portfolio_render.py`

**Files:**
- Rewrite: `tests/test_portfolio_render.py`

- [ ] **Step 1: Replace the entire test file**

```python
"""Tests for PortfolioPanel rendering.

Verifies the panel renders the Account + Coverage layout correctly
with various data states.
"""

from __future__ import annotations

from talos.ui.app import TalosApp
from talos.ui.widgets import ActivityLog, OrderLog, PortfolioPanel


class TestPortfolioPanelRendering:
    async def test_initial_content_visible(self) -> None:
        """Panel should show $0.00 and zero counts on mount."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            await pilot.pause()
            text = str(panel.render())
            assert "Cash:" in text
            assert "$0.00" in text
            assert "Matched:" in text
            assert "Events:" in text

    async def test_has_nonzero_dimensions(self) -> None:
        """Panel must have real width and height, not zero."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            await pilot.pause()
            assert panel.size.width > 0
            assert panel.size.height > 0

    async def test_content_size_nonzero(self) -> None:
        """Content size must be nonzero (content actually renders)."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            await pilot.pause()
            assert panel.content_size.width > 0
            assert panel.content_size.height > 0

    async def test_update_balance_reflects_in_render(self) -> None:
        """After update_balance, render() should show new cash value."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            panel.update_balance(125000, 210050)
            await pilot.pause()
            text = str(panel.render())
            assert "$1,250.00" in text

    async def test_update_account_reflects(self) -> None:
        """Matched/partial/locked/exposure should update."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            panel.update_account(matched=5, partial=3, locked=1500.0, exposure=800)
            await pilot.pause()
            text = str(panel.render())
            assert "5 units" in text
            assert "3 events" in text
            assert "$15.00" in text
            assert "$8.00" in text

    async def test_update_coverage_reflects(self) -> None:
        """Events/positions/bidding/unentered should update."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            panel.update_coverage(events=100, with_positions=20, bidding=30, unentered=50)
            await pilot.pause()
            text = str(panel.render())
            assert "100" in text
            assert "20" in text
            assert "30" in text
            assert "50" in text

    async def test_no_pnl_section(self) -> None:
        """P&L section (Today/Yesterday/7d) must be gone."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            await pilot.pause()
            text = str(panel.render())
            assert "Today:" not in text
            assert "Yesterday:" not in text
            assert "Last 7d:" not in text

    async def test_all_three_panels_have_regions(self) -> None:
        """All bottom panels must have nonzero regions."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            for widget_class in [PortfolioPanel, ActivityLog, OrderLog]:
                w = app.query_one(widget_class)
                assert w.region.width > 0
                assert w.region.height > 0

    async def test_render_lines_contain_text(self) -> None:
        """Actual rendered strips must contain panel text."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            panel.update_balance(500000, 0)
            await pilot.pause()
            from textual.geometry import Region

            lines = panel.render_lines(Region(0, 0, panel.size.width, panel.size.height))
            all_text = ""
            for strip in lines:
                for seg in strip._segments:
                    all_text += seg.text
            assert "$5,000.00" in all_text

    async def test_panel_visible_at_small_terminal(self) -> None:
        """Panel visible at 80x24."""
        app = TalosApp()
        async with app.run_test(size=(80, 24)) as pilot:
            panel = app.query_one(PortfolioPanel)
            await pilot.pause()
            assert panel.size.width > 0
            text = str(panel.render())
            assert "Cash:" in text

    async def test_panel_visible_at_large_terminal(self) -> None:
        """Panel visible at 200x50."""
        app = TalosApp()
        async with app.run_test(size=(200, 50)) as pilot:
            panel = app.query_one(PortfolioPanel)
            await pilot.pause()
            assert panel.size.width > 0
            text = str(panel.render())
            assert "Cash:" in text
```

- [ ] **Step 2: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_portfolio_render.py -x -v`
Expected: All pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_portfolio_render.py
git commit -m "test: rewrite portfolio panel tests for new Account + Coverage layout"
```

---

### Task 5: Fix remaining test references

**Files:**
- Modify: `tests/test_ui.py` (if any `update_portfolio_summary` or `update_pnl` calls exist)
- Modify: `tests/test_proposal_panel.py` (if any panel method calls exist)

- [ ] **Step 1: Grep for old method names in tests**

Run: `grep -rn "update_portfolio_summary\|update_pnl\|update_tracked_counts" tests/`

Fix any remaining references to use the new `update_account` / `update_coverage` methods.

- [ ] **Step 2: Run full test suite**

Run: `.venv/Scripts/python -m pytest -x`
Expected: All 1030+ tests pass.

- [ ] **Step 3: Run lint + format**

Run: `.venv/Scripts/python -m ruff check src/ tests/ && .venv/Scripts/python -m ruff format src/ tests/`
Expected: Clean.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "fix: update remaining test references for portfolio panel redesign"
```
