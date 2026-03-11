# UI Readability Overhaul Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Transform the OpportunitiesTable from plain white text to Rich-styled cells with color, dimming, alignment, and status icons so the operator can scan delta neutrality, fill progress, and system behavior at a glance.

**Architecture:** All formatting changes live in `widgets.py` helper functions that return `rich.text.Text` objects instead of plain strings. The `refresh_from_scanner` method calls these helpers. Short event labels require storing `sub_title` from the API (new field on `Event` model, plumbed through `GameManager` → `scanner`). Status icons require adding "Jumped" detection to `_compute_event_status` in `engine.py`.

**Tech Stack:** Textual DataTable (accepts Rich `Text` as cell values), Rich `Text` with `style=` and `justify=`, Catppuccin Mocha palette from `theme.py`.

---

### Task 1: Rich Text Foundation — Dim Em-Dashes

The smallest possible change that proves Rich `Text` works in the DataTable cells.

**Files:**
- Modify: `src/talos/ui/widgets.py:1-14` (imports)
- Modify: `src/talos/ui/widgets.py:167-178` (no-position branch in `refresh_from_scanner`)
- Test: `tests/test_ui.py`

**Step 1: Write the failing test**

Add to `tests/test_ui.py`:

```python
from rich.text import Text as RichText

class TestRichTextCells:
    async def test_empty_cells_are_dim_rich_text(self) -> None:
        """Em-dash placeholders should be dim Rich Text, not plain strings."""
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            row = table.get_row_at(0)
            # Pos-A (index 4) should be a dim Rich Text em-dash
            pos_a = row[4]
            assert isinstance(pos_a, RichText)
            assert str(pos_a) == "—"
            assert "dim" in pos_a.style
```

**Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py::TestRichTextCells::test_empty_cells_are_dim_rich_text -v`
Expected: FAIL — `pos_a` is a plain string `"—"`, not `RichText`

**Step 3: Write minimal implementation**

In `src/talos/ui/widgets.py`, add import at top:

```python
from rich.text import Text as RichText
```

Add a helper function after the existing helpers (~line 14):

```python
def _dim(value: str) -> RichText:
    """Wrap a placeholder value in dim styling."""
    return RichText(value, style="dim")

DIM_DASH = _dim("—")
```

In `refresh_from_scanner`, replace the no-position branch (lines ~167-178):

```python
            else:
                pos_a = DIM_DASH
                pos_b = DIM_DASH
                q_a = DIM_DASH
                q_b = DIM_DASH
                cpm_a = DIM_DASH
                cpm_b = DIM_DASH
                eta_a = DIM_DASH
                eta_b = DIM_DASH
                pnl = DIM_DASH
                status = ""
                net_odds = _fmt_net_odds(opp.no_a, opp.no_b)
```

**Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py::TestRichTextCells -v`
Expected: PASS

**Step 5: Fix any broken existing tests**

Existing tests use `str(row)` or `str(row[N])` checks. `str(RichText("—", style="dim"))` returns `"—"` so most should pass. Run full suite:

Run: `.venv/Scripts/python -m pytest tests/test_ui.py -v`

Fix any assertion failures by wrapping with `str()` where needed.

**Step 6: Commit**

```
git add src/talos/ui/widgets.py tests/test_ui.py
git commit -m "feat(ui): dim em-dash placeholders with Rich Text"
```

---

### Task 2: Right-Align Numeric Columns

**Files:**
- Modify: `src/talos/ui/widgets.py` (formatting helpers)
- Test: `tests/test_ui.py`

**Step 1: Write the failing test**

```python
    async def test_numeric_cells_are_right_aligned(self) -> None:
        """Numeric columns should be right-justified Rich Text."""
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            row = table.get_row_at(0)
            # NO-A (index 1) should be right-aligned
            no_a = row[1]
            assert isinstance(no_a, RichText)
            assert no_a.justify == "right"
            assert "38¢" in str(no_a)
```

**Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py::TestRichTextCells::test_numeric_cells_are_right_aligned -v`
Expected: FAIL

**Step 3: Implement right-aligned helpers**

Replace `_fmt_cents` and update other formatting in `widgets.py`:

```python
def _fmt_cents(value: int) -> RichText:
    """Format an integer cents value as 'XX¢', right-aligned."""
    return RichText(f"{value}¢", justify="right")


def _fmt_edge(fee_edge: float) -> RichText:
    """Format fee-adjusted edge with color: green if positive, dim otherwise."""
    label = f"{fee_edge:.1f}¢"
    if fee_edge > 0:
        return RichText(label, style=GREEN, justify="right")
    return RichText(label, style="dim", justify="right")


def _fmt_pnl(net_cents: float) -> RichText:
    """Format P&L in dollars: green positive, red negative."""
    dollars = net_cents / 100
    if dollars >= 0:
        label = f"${dollars:.2f}"
        return RichText(label, style=GREEN, justify="right")
    label = f"-${abs(dollars):.2f}"
    return RichText(label, style=RED, justify="right")
```

Import `GREEN`, `RED` from `theme.py` at the top:

```python
from talos.ui.theme import GREEN, OVERLAY0, PEACH, RED, YELLOW, BLUE
```

Update `_fmt_pos` to return `RichText`:

```python
def _fmt_pos(filled: int, total: int, avg_no_price: int) -> RichText:
    """Format position as 'filled/total avg¢' with fee-adjusted cost."""
    if total == 0:
        return DIM_DASH
    if filled == 0:
        return RichText(f"0/{total}", justify="right")
    fee_avg = fee_adjusted_cost(avg_no_price)
    return RichText(f"{filled}/{total} {fee_avg:.1f}¢", justify="right")
```

Update `_fmt_net_odds` return type to `RichText`:

```python
def _fmt_net_odds(...) -> RichText:
    # ... existing logic, but wrap returns:
    # GTD → green
    if worse > 0:
        return RichText(f"GTD ${worse / 100:.2f}", style=GREEN, justify="right")
    # Underwater → red
    if better <= 0:
        return RichText(f"-${abs(better) / 100:.2f}", style=RED, justify="right")
    # Mixed → default
    # ... existing formatting ...
    return RichText(f"${base / 100:.0f} {side} {eff_str}", justify="right")
    # No-fill case:
    return RichText(odds_str, justify="right")
```

In `refresh_from_scanner`, update the with-position branch to use the new helpers:

```python
            edge_str = _fmt_edge(opp.fee_edge)
            # ... position columns already return RichText from updated helpers ...
            pnl = _fmt_pnl(net)
```

Also right-align queue/CPM/ETA values when they are plain strings (the with-position branch):

```python
                q_a = RichText(str(pos.leg_a.queue_position), justify="right") if pos.leg_a.queue_position else DIM_DASH
                q_b = RichText(str(pos.leg_b.queue_position), justify="right") if pos.leg_b.queue_position else DIM_DASH
                cpm_a = RichText(format_cpm(pos.leg_a.cpm, pos.leg_a.cpm_partial), justify="right")
                cpm_b = RichText(format_cpm(pos.leg_b.cpm, pos.leg_b.cpm_partial), justify="right")
                eta_a = RichText(format_eta(pos.leg_a.eta_minutes, pos.leg_a.cpm_partial), justify="right")
                eta_b = RichText(format_eta(pos.leg_b.eta_minutes, pos.leg_b.cpm_partial), justify="right")
```

**Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py -v`

Fix existing tests that compare with `str()` — they should still pass because `str(RichText("38¢"))` == `"38¢"`.

**Step 5: Commit**

```
git add src/talos/ui/widgets.py tests/test_ui.py
git commit -m "feat(ui): right-align numerics and color-code Edge/P&L/Net-Odds"
```

---

### Task 3: Queue Position Warning Styling

**Files:**
- Modify: `src/talos/ui/widgets.py` (queue formatting in `refresh_from_scanner`)
- Test: `tests/test_ui.py`

**Step 1: Write the failing test**

```python
    async def test_jumped_queue_is_yellow(self) -> None:
        """Queue position with !! prefix should be styled yellow."""
        from talos.top_of_market import TopOfMarketTracker

        scanner = _make_scanner_with_opportunity()
        tracker = TopOfMarketTracker()
        # Set up tracker with resting order NOT at top
        tracker.update("GAME-STAN", resting_price=38, best_no_ask=35)

        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            table = app.query_one(OpportunitiesTable)
            table.refresh_from_scanner(scanner, tracker)
            await pilot.pause()
            row = table.get_row_at(0)
            q_a = row[6]  # Q-A column
            assert isinstance(q_a, RichText)
            assert "!!" in str(q_a)
            assert YELLOW in q_a.style
```

**Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py::TestRichTextCells::test_jumped_queue_is_yellow -v`

**Step 3: Implement yellow queue warnings**

In `refresh_from_scanner`, replace the top-of-market warning block:

```python
            # Top-of-market warning (applies regardless of position data)
            if tracker is not None:
                if tracker.is_at_top(opp.ticker_a) is False:
                    q_a = RichText(f"!! {str(q_a)}", style=YELLOW, justify="right")
                if tracker.is_at_top(opp.ticker_b) is False:
                    q_b = RichText(f"!! {str(q_b)}", style=YELLOW, justify="right")
```

**Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py -v`

**Step 5: Commit**

```
git add src/talos/ui/widgets.py tests/test_ui.py
git commit -m "feat(ui): yellow styling for jumped queue positions"
```

---

### Task 4: Delta Neutrality Highlighting

**Files:**
- Modify: `src/talos/ui/widgets.py` (position comparison logic in `refresh_from_scanner`)
- Test: `tests/test_ui.py`

**Step 1: Write the failing test**

```python
    async def test_imbalanced_position_highlighted_yellow(self) -> None:
        """When fills are imbalanced, the behind side should be yellow."""
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            table = app.query_one(OpportunitiesTable)
            table.update_positions(
                [
                    EventPositionSummary(
                        event_ticker="EVT-STANMIA",
                        leg_a=LegSummary(
                            ticker="GAME-STAN", no_price=31,
                            filled_count=3, resting_count=7, total_fill_cost=93,
                        ),
                        leg_b=LegSummary(
                            ticker="GAME-MIA", no_price=67,
                            filled_count=5, resting_count=5, total_fill_cost=335,
                        ),
                        matched_pairs=3, locked_profit_cents=0,
                        unmatched_a=0, unmatched_b=2, exposure_cents=0,
                    )
                ]
            )
            app.refresh_opportunities()
            await pilot.pause()
            row = table.get_row_at(0)
            pos_a = row[4]  # Pos-A: 3 filled (behind)
            pos_b = row[5]  # Pos-B: 5 filled (ahead)
            assert isinstance(pos_a, RichText)
            # Behind side (A has fewer fills) should be yellow
            assert YELLOW in pos_a.style
            # Ahead side should NOT be yellow
            assert YELLOW not in str(pos_b.style)
```

**Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py::TestRichTextCells::test_imbalanced_position_highlighted_yellow -v`

**Step 3: Implement delta comparison**

In `refresh_from_scanner`, after computing `pos_a` and `pos_b` (with-position branch), add:

```python
                # Delta neutrality highlighting
                if pos is not None:
                    fa, fb = pos.leg_a.filled_count, pos.leg_b.filled_count
                    ra, rb = pos.leg_a.resting_count, pos.leg_b.resting_count
                    ta, tb = fa + ra, fb + rb
                    # Highlight behind side yellow for fill imbalance
                    if fa != fb:
                        if fa < fb:
                            pos_a = RichText(str(pos_a), style=YELLOW, justify="right")
                        else:
                            pos_b = RichText(str(pos_b), style=YELLOW, justify="right")
                    # Highlight behind side for resting imbalance (when fills are equal)
                    elif ta != tb:
                        if ta < tb:
                            pos_a = RichText(str(pos_a), style=YELLOW, justify="right")
                        else:
                            pos_b = RichText(str(pos_b), style=YELLOW, justify="right")
```

**Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py -v`

**Step 5: Commit**

```
git add src/talos/ui/widgets.py tests/test_ui.py
git commit -m "feat(ui): yellow highlight on behind side for delta imbalance"
```

---

### Task 5: Status Icons with Color

**Files:**
- Modify: `src/talos/ui/widgets.py` (new `_fmt_status` helper)
- Modify: `src/talos/engine.py` (`_compute_event_status` — add "Jumped" state)
- Test: `tests/test_ui.py`
- Test: `tests/test_engine.py`

**Step 5a: Add "Jumped" status to engine**

In `src/talos/engine.py`, in `_compute_event_status`, add a check after the "Bidding" returns (around line 1022) and before the fill imbalance check. The tracker knows which tickers are jumped:

```python
        # Check if any side is jumped (not at top of market)
        pair = self._find_pair(event_ticker)
        if pair is not None:
            jumped_a = self._tracker.is_at_top(pair.ticker_a) is False
            jumped_b = self._tracker.is_at_top(pair.ticker_b) is False
            if jumped_a or jumped_b:
                sides = ""
                if jumped_a:
                    sides += "A"
                if jumped_b:
                    sides += "B"
                return f"Jumped {sides}"
```

Insert this block in two locations:
1. After line 977 (`return "Bidding"`) — when both sides complete and re-bidding
2. After line 1022 (`return "Bidding"`) — when fills equal with resting on both sides

Actually, a cleaner approach: add the jumped check right after the discrepancy check (line 963) and before any position-based logic — but only when there are resting orders. The jumped check applies to any state with resting orders:

In `_compute_event_status`, after the discrepancy check and before the pending proposal check (~line 965):

```python
        # Jumped — resting orders exist but not at top of market
        if resting_a > 0 or resting_b > 0:
            pair = self._find_pair(event_ticker)
            if pair is not None:
                jumped_a = resting_a > 0 and self._tracker.is_at_top(pair.ticker_a) is False
                jumped_b = resting_b > 0 and self._tracker.is_at_top(pair.ticker_b) is False
                if jumped_a or jumped_b:
                    sides = ""
                    if jumped_a:
                        sides += "A"
                    if jumped_b:
                        sides += "B"
                    return f"Jumped {sides}"
```

**Step 5b: Write the status formatting helper**

In `widgets.py`, add:

```python
def _fmt_status(status: str) -> RichText:
    """Format status with icon and color."""
    if not status:
        return DIM_DASH

    # Map status prefixes to (icon, color)
    STATUS_STYLES: dict[str, tuple[str, str]] = {
        "Low edge": ("○", "dim"),
        "Unstable": ("○", "dim"),
        "Sug. off": ("○", "dim"),
        "Ready": ("○", "dim"),
        "Stable": ("○", "dim"),
        "Cooldown": ("○", "dim"),
        "Proposed": ("◎", BLUE),
        "Resting": ("◷", YELLOW),
        "Bidding": ("◷", YELLOW),
        "Jumped": ("◷", PEACH),
        "Filling": ("◐", BLUE),
        "Waiting": ("◐", BLUE),
        "Need bid": ("◐", BLUE),
        "Locked": ("✓", GREEN),
        "Imbalanced": ("⚠", YELLOW),
        "Discrepancy": ("⚠", RED),
    }

    for prefix, (icon, color) in STATUS_STYLES.items():
        if status.startswith(prefix):
            return RichText(f"{icon} {status}", style=color)

    # Fallback: unknown status, no icon
    return RichText(status)
```

**Step 5c: Wire into refresh_from_scanner**

Replace `status` assignment in the row_data tuple. In the with-position branch:

```python
                status = _fmt_status(pos.status)
```

In the no-position branch:

```python
                status = DIM_DASH
```

**Step 5d: Write tests**

For the helper (pure function, no async needed):

```python
class TestStatusFormatting:
    def test_low_edge_is_dim(self) -> None:
        result = _fmt_status("Low edge")
        assert "○" in str(result)
        assert "dim" in result.style

    def test_jumped_is_peach(self) -> None:
        result = _fmt_status("Jumped A")
        assert "◷" in str(result)
        assert PEACH in result.style

    def test_filling_is_blue(self) -> None:
        result = _fmt_status("Filling (B -3)")
        assert "◐" in str(result)
        assert BLUE in result.style

    def test_empty_is_dim_dash(self) -> None:
        result = _fmt_status("")
        assert str(result) == "—"
```

For the engine "Jumped" status, add to `tests/test_engine.py`:

```python
    async def test_status_jumped_when_not_at_top(self) -> None:
        # Setup: engine with a pair that has resting orders and is jumped
        # ... (use existing test patterns for engine setup)
        # Assert status starts with "Jumped"
```

**Step 5e: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py tests/test_engine.py -v`

**Step 5f: Commit**

```
git add src/talos/ui/widgets.py src/talos/engine.py tests/test_ui.py tests/test_engine.py
git commit -m "feat(ui): status icons with color, add Jumped state"
```

---

### Task 6: Short Event Labels

This requires plumbing `sub_title` from the API through `GameManager` to the scanner/UI.

**Files:**
- Modify: `src/talos/models/market.py` (add `sub_title` to `Event`)
- Modify: `src/talos/game_manager.py` (store `sub_title` in a label map)
- Modify: `src/talos/ui/widgets.py` (use label map in `refresh_from_scanner`)
- Modify: `src/talos/ui/app.py` (pass label map to table refresh)
- Test: `tests/test_ui.py`

**Step 6a: Add `sub_title` to Event model**

In `src/talos/models/market.py`, add to `Event`:

```python
class Event(BaseModel):
    event_ticker: str
    series_ticker: str
    title: str
    sub_title: str = ""
    category: str
    status: str | None = None
    mutually_exclusive: bool | None = None
    markets: list[Market] = []
```

**Step 6b: Store labels in GameManager**

In `src/talos/game_manager.py`, add a label dict and populate it in `add_game`:

```python
    def __init__(self, ...):
        # ... existing init ...
        self._labels: dict[str, str] = {}  # event_ticker → short label

    @property
    def labels(self) -> dict[str, str]:
        """Event ticker → short display label."""
        return dict(self._labels)
```

In `add_game`, after creating the pair, extract a short label from `event.sub_title`:

```python
        # Store short label for UI
        label = event.sub_title or event.title
        # sub_title is like "ROMBRA vs BRADY (Mar 11)" — strip the date suffix
        if "(" in label:
            label = label[:label.rfind("(")].strip()
        # Replace " vs " / " at " with "-" for compactness
        for sep in (" vs ", " at ", " vs. "):
            label = label.replace(sep, "-")
        self._labels[event.event_ticker] = label
```

**Step 6c: Pass labels to table**

In `src/talos/ui/widgets.py`, add a `_labels` dict to `OpportunitiesTable`:

```python
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._positions: dict[str, EventPositionSummary] = {}
        self._labels: dict[str, str] = {}

    def update_labels(self, labels: dict[str, str]) -> None:
        """Store event ticker → short display label mapping."""
        self._labels = labels
```

In `refresh_from_scanner`, replace `opp.event_ticker` in the row_data tuple:

```python
            display_name = self._labels.get(opp.event_ticker, opp.event_ticker)
            row_data = (
                display_name,
                # ... rest unchanged
            )
```

**Important:** Keep `opp.event_ticker` as the row key (not display_name). The key is used for `update_cell` and row identity.

**Step 6d: Wire in app.py**

In `src/talos/ui/app.py`, wherever `refresh_opportunities` is called, also update labels from the engine's game manager:

```python
    def refresh_opportunities(self) -> None:
        table = self.query_one(OpportunitiesTable)
        if self._engine is not None:
            table.update_labels(self._engine.game_manager.labels)
        table.refresh_from_scanner(self._scanner, self._tracker)
```

The `game_manager` property needs to be exposed on `TradingEngine` if it isn't already. Check and add if needed.

**Step 6e: Write tests**

```python
    async def test_short_event_label_displayed(self) -> None:
        """Table should show short label, not full event ticker."""
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            table = app.query_one(OpportunitiesTable)
            table.update_labels({"EVT-STANMIA": "Stan-Mia"})
            app.refresh_opportunities()
            await pilot.pause()
            row = table.get_row_at(0)
            assert str(row[0]) == "Stan-Mia"
```

**Step 6f: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py -v`

**Step 6g: Commit**

```
git add src/talos/models/market.py src/talos/game_manager.py src/talos/ui/widgets.py src/talos/ui/app.py tests/test_ui.py
git commit -m "feat(ui): short event labels from sub_title"
```

---

### Task 7: Widen Status Column and Final Polish

The status column is currently `width=14`. With icons and "Jumped AB" text, it may need to be wider.

**Files:**
- Modify: `src/talos/ui/widgets.py` (column width)

**Step 1: Adjust column width**

In `on_mount`, change Status column width:

```python
        self.add_column("Status", width=16)
```

Also widen Event column now that labels are shorter — consider removing the fixed width (let it auto-size) or setting it to a reasonable value.

**Step 2: Visual smoke test**

Run: `set -a && source .env && set +a && .venv/Scripts/python -m talos`

Visually verify:
- Em-dashes are dimmed
- Numbers are right-aligned
- Edge is green when positive
- P&L is green/red
- GTD is green in Net/Odds
- Jumped queue shows yellow `!!`
- Imbalanced positions show yellow on behind side
- Status shows icons with correct colors
- Event column shows short labels

**Step 3: Commit**

```
git add src/talos/ui/widgets.py
git commit -m "feat(ui): polish column widths for readability overhaul"
```

---

## Summary

| Task | What | Files |
|------|------|-------|
| 1 | Dim em-dashes (Rich Text foundation) | `widgets.py` |
| 2 | Right-align numerics + color Edge/P&L/Net-Odds | `widgets.py` |
| 3 | Yellow queue warnings | `widgets.py` |
| 4 | Delta neutrality highlighting | `widgets.py` |
| 5 | Status icons + Jumped state | `widgets.py`, `engine.py` |
| 6 | Short event labels | `market.py`, `game_manager.py`, `widgets.py`, `app.py` |
| 7 | Column width polish | `widgets.py` |

Tasks 1-4 are pure `widgets.py` changes (low risk). Task 5 touches `engine.py` (medium risk — needs engine tests). Task 6 touches the data pipeline (medium risk — needs model + manager + app wiring). Task 7 is cosmetic.
