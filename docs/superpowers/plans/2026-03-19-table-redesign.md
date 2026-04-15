# Table Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the main OpportunitiesTable from 20 columns / 1 row per event to 14 columns / 2 rows per event (one per team), with freshness indicators, Locked/Exposure columns, a portfolio summary panel, and settlement P&L tracking.

**Architecture:** Six tasks build incrementally. Each produces working, testable code. Tasks 1-2 add data infrastructure (per-leg labels, freshness exposure). Task 3 is the core table rewrite. Task 4 replaces the account panel. Task 5 adds settlement P&L. Task 6 adds internal reconciliation. The table rewrite (Task 3) is the biggest change — everything else is additive.

**Tech Stack:** Python 3.12+, Textual (terminal UI), Pydantic v2, pytest

**Spec:** `docs/superpowers/specs/2026-03-19-table-redesign-design.md`

---

## File Structure

| File | Responsibility | Task |
|------|---------------|------|
| `src/talos/game_manager.py` | Add `leg_labels` dict storing per-event `(team_a, team_b)` tuples | 1 |
| `src/talos/ui/widgets.py` | Rewrite `OpportunitiesTable` for two-row layout, freshness dots, new columns. Rewrite `AccountPanel` → `PortfolioPanel`. | 3, 4 |
| `src/talos/ui/app.py` | Wire new panel data, update row selection for two-row keys, pass orderbook to table | 3, 4, 5 |
| `src/talos/settlement_tracker.py` | New: fetch and cache settlement P&L by time window | 5 |
| `tests/test_table_redesign.py` | New: tests for two-row rendering, freshness, labels | 1, 2, 3 |
| `tests/test_settlement_tracker.py` | New: tests for settlement P&L aggregation | 5 |

---

### Task 1: Per-Leg Team Name Labels

**Files:**
- Modify: `src/talos/game_manager.py:93-197`
- Create: `tests/test_table_redesign.py`

- [ ] **Step 1: Write failing test for leg label extraction**

```python
# tests/test_table_redesign.py
"""Tests for table redesign features."""

from talos.game_manager import extract_leg_labels


def test_extract_leg_labels_from_subtitle():
    """sub_title like 'Boston Bruins vs Washington Capitals (Mar 19)' → tuple."""
    result = extract_leg_labels("Boston Bruins vs Washington Capitals (Mar 19)")
    assert result == ("Boston Bruins", "Washington Capitals")


def test_extract_leg_labels_no_date_suffix():
    result = extract_leg_labels("LA Lakers vs NY Knicks")
    assert result == ("LA Lakers", "NY Knicks")


def test_extract_leg_labels_at_separator():
    """'Wake Forest at Virginia Tech (Mar 10)' → tuple."""
    result = extract_leg_labels("Wake Forest at Virginia Tech (Mar 10)")
    assert result == ("Wake Forest", "Virginia Tech")


def test_extract_leg_labels_unparseable():
    """Fallback to full string for both if no separator found."""
    result = extract_leg_labels("Some Weird Title")
    assert result == ("Some Weird Title", "Some Weird Title")


def test_extract_leg_labels_empty():
    result = extract_leg_labels("")
    assert result == ("", "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_table_redesign.py::test_extract_leg_labels_from_subtitle -v`
Expected: FAIL — `cannot import name 'extract_leg_labels'`

- [ ] **Step 3: Implement `extract_leg_labels` in game_manager.py**

Add this function near the top of `game_manager.py` (after imports):

```python
def extract_leg_labels(sub_title: str) -> tuple[str, str]:
    """Extract per-leg team names from event sub_title.

    Handles formats like:
    - "Boston Bruins vs Washington Capitals (Mar 19)"
    - "Wake Forest at Virginia Tech (Mar 10)"

    Returns (team_a, team_b) tuple. Falls back to (full, full) if unparseable.
    """
    if not sub_title:
        return ("", "")
    # Strip date suffix in parens
    label = sub_title
    if "(" in label:
        label = label[: label.rfind("(")].strip()
    # Try separators
    for sep in (" vs ", " vs. ", " at "):
        if sep in label:
            parts = label.split(sep, 1)
            return (parts[0].strip(), parts[1].strip())
    return (label, label)
```

- [ ] **Step 4: Add `_leg_labels` dict to GameManager and populate in `add_game`**

In `GameManager.__init__` (around line 93), add:
```python
self._leg_labels: dict[str, tuple[str, str]] = {}
```

In `add_game`, after line 197 (`self._labels[event.event_ticker] = label`), add:
```python
self._leg_labels[event.event_ticker] = extract_leg_labels(
    event.sub_title or event.title
)
```

Add a property to expose it:
```python
@property
def leg_labels(self) -> dict[str, tuple[str, str]]:
    return self._leg_labels
```

In `remove_game` (around line 274), add cleanup alongside existing `self._labels.pop(...)`:
```python
self._leg_labels.pop(event_ticker, None)
```

- [ ] **Step 5: Run all tests to verify**

Run: `.venv/Scripts/python -m pytest tests/test_table_redesign.py -v`
Expected: All 5 tests PASS

- [ ] **Step 6: Commit**

```bash
git add tests/test_table_redesign.py src/talos/game_manager.py
git commit -m "feat: add per-leg team name extraction for two-row table layout"
```

---

### Task 2: Expose Orderbook Freshness to Table

**Files:**
- Modify: `src/talos/ui/widgets.py:225-237` (add `_orderbook_ages` storage)
- Modify: `src/talos/ui/app.py:85-107` (pass freshness data on each refresh)
- Modify: `tests/test_table_redesign.py`

The orderbook already has `last_update: float` per `LocalOrderBook` (orderbook.py:36). We need to expose the age (seconds since last update) to the table widget so it can render freshness dots.

- [ ] **Step 1: Write failing test for freshness dot formatting**

Append to `tests/test_table_redesign.py`:

```python
from talos.ui.widgets import _fmt_freshness


def test_freshness_dot_fresh():
    """< 5 seconds → green dot."""
    result = _fmt_freshness(2.0)
    assert "●" in str(result)
    # Green color check via style


def test_freshness_dot_warming():
    """5-30 seconds → yellow dot."""
    result = _fmt_freshness(15.0)
    assert "●" in str(result)


def test_freshness_dot_stale():
    """30+ seconds → red dot."""
    result = _fmt_freshness(45.0)
    assert "●" in str(result)


def test_freshness_dot_never_connected():
    """No data yet (age=None) → dim dot."""
    result = _fmt_freshness(None)
    assert "○" in str(result)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_table_redesign.py::test_freshness_dot_fresh -v`
Expected: FAIL — `cannot import name '_fmt_freshness'`

- [ ] **Step 3: Implement `_fmt_freshness` in widgets.py**

Add near the other `_fmt_*` helpers in `widgets.py`:

```python
def _fmt_freshness(age_seconds: float | None) -> RichText:
    """Format freshness dot based on seconds since last WS update."""
    if age_seconds is None:
        return RichText("○", style="dim", justify="center")
    if age_seconds < 5.0:
        return RichText("●", style=GREEN, justify="center")
    if age_seconds < 30.0:
        return RichText("●", style=YELLOW, justify="center")
    return RichText("●", style=RED, justify="center")
```

- [ ] **Step 4: Add `update_freshness` method to OpportunitiesTable**

In `OpportunitiesTable.__init__`:
```python
self._freshness: dict[str, float | None] = {}  # market_ticker -> age in seconds
```

Add method:
```python
def update_freshness(self, ages: dict[str, float | None]) -> None:
    """Store per-market freshness ages for next render."""
    self._freshness = ages
```

- [ ] **Step 5: Wire freshness data from app.py**

In `TalosApp`, add a method to compute freshness from the orderbook manager and pass to the table. This gets called during `refresh_opportunities`:

```python
def _update_freshness(self) -> None:
    """Compute orderbook age per market and push to table."""
    if self._engine is None:
        return
    table = self.query_one(OpportunitiesTable)
    now = time.time()
    ages: dict[str, float | None] = {}
    for ticker, book in self._engine.orderbooks.books.items():
        if book.last_update <= 0.0:
            ages[ticker] = None
        else:
            ages[ticker] = now - book.last_update
    table.update_freshness(ages)
```

Call `self._update_freshness()` at the end of `refresh_opportunities()`.

- [ ] **Step 6: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_table_redesign.py -v`
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add src/talos/ui/widgets.py src/talos/ui/app.py tests/test_table_redesign.py
git commit -m "feat: add orderbook freshness indicator infrastructure"
```

---

### Task 3: Two-Row Table Rewrite

**Files:**
- Modify: `src/talos/ui/widgets.py:202-489` (OpportunitiesTable — major rewrite)
- Modify: `src/talos/ui/app.py` (row selection handling, label wiring)
- Modify: `tests/test_table_redesign.py`

This is the core change. The table goes from 20 columns / 1 row per event to 14 columns / 2 rows per event.

- [ ] **Step 1: Write failing test for two-row build**

Append to `tests/test_table_redesign.py`:

```python
from unittest.mock import MagicMock
from talos.ui.widgets import OpportunitiesTable


def test_build_two_rows_returns_pair():
    """_build_row_pair returns two tuples (row1, row2)."""
    table = OpportunitiesTable()
    table._leg_labels = {"EVT-TEST": ("Boston Bruins", "Washington Capitals")}
    table._freshness = {"MKT-A": 1.0, "MKT-B": 2.0}

    opp = MagicMock()
    opp.event_ticker = "EVT-TEST"
    opp.ticker_a = "MKT-A"
    opp.ticker_b = "MKT-B"
    opp.no_a = 42
    opp.no_b = 44
    opp.fee_edge = 3.2

    row1, row2 = table._build_row_pair(opp, tracker=None)
    # Row 1 should have team name "Boston Bruins"
    assert "Boston Bruins" in str(row1[1])
    # Row 2 should have team name "Washington Capitals"
    assert "Washington Capitals" in str(row2[1])
    # Both rows have 14 cells
    assert len(row1) == 14
    assert len(row2) == 14
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_table_redesign.py::test_build_two_rows_returns_pair -v`
Expected: FAIL — `_build_row_pair` not found

- [ ] **Step 3: Rewrite `on_mount` columns**

Replace the existing `on_mount` column definitions (widgets.py:256-279) with the new 14-column layout:

```python
def on_mount(self) -> None:
    self.cursor_type = "row"
    self.zebra_stripes = False  # We handle pair striping ourselves
    r = "right"
    c = "center"
    self.add_column(RichText("", justify=c), width=2)       # Freshness dot
    self.add_column("Team")                                   # Team name
    self.add_column("Lg", width=5)                           # League
    self.add_column(RichText("Game", justify=r), width=9)    # Game status
    self.add_column(RichText("NO", justify=r), width=5)      # NO price
    self.add_column(RichText("Vol", justify=r), width=6)     # 24h volume
    self.add_column(RichText("Pos", justify=r), width=14)    # Position
    self.add_column(RichText("Queue", justify=r), width=6)   # Queue position
    self.add_column(RichText("CPM", justify=r), width=8)     # Contracts/min
    self.add_column(RichText("ETA", justify=r), width=7)     # Est. time to fill
    self.add_column(RichText("Edge", justify=r), width=6)    # Fee-adjusted edge
    self.add_column("Status", width=16)                       # Event status
    self.add_column(RichText("Locked", justify=r), width=10) # Locked profit
    self.add_column(RichText("Expos", justify=r), width=10)  # Exposure
```

- [ ] **Step 4: Add `_leg_labels` storage and `update_leg_labels` method**

In `OpportunitiesTable.__init__`:
```python
self._leg_labels: dict[str, tuple[str, str]] = {}
```

Add method:
```python
def update_leg_labels(self, labels: dict[str, tuple[str, str]]) -> None:
    """Store per-event (team_a, team_b) labels."""
    self._leg_labels = labels
```

- [ ] **Step 5: Implement `_build_row_pair`**

Replace `_build_row` with `_build_row_pair` that returns two tuples:

```python
def _build_row_pair(
    self, opp: Any, tracker: TopOfMarketTracker | None
) -> tuple[tuple, tuple]:
    """Build two row tuples (row1=team_a, row2=team_b) for one event."""
    # Team names
    team_a, team_b = self._leg_labels.get(
        opp.event_ticker, (opp.ticker_a, opp.ticker_b)
    )

    # Freshness dots
    dot_a = _fmt_freshness(self._freshness.get(opp.ticker_a))
    dot_b = _fmt_freshness(self._freshness.get(opp.ticker_b))

    # Edge
    edge_str = _fmt_edge(opp.fee_edge)

    # Per-leg price and volume
    no_a = _fmt_cents(opp.no_a)
    no_b = _fmt_cents(opp.no_b)
    vol_a = _fmt_vol(self._volumes_24h.get(opp.ticker_a, 0))
    vol_b = _fmt_vol(self._volumes_24h.get(opp.ticker_b, 0))

    # Game status (row 1 only)
    prefix = opp.event_ticker.split("-")[0]
    _, league = _SPORT_LEAGUE.get(prefix, ("—", "—"))
    game_status = self._resolver.get(opp.event_ticker) if self._resolver else None
    game_col = _fmt_game_status(game_status)

    # Position data
    pos = self._positions.get(opp.event_ticker)
    if pos is not None:
        total_a = pos.leg_a.filled_count + pos.leg_a.resting_count
        total_b = pos.leg_b.filled_count + pos.leg_b.resting_count
        pos_a = _fmt_pos(pos.leg_a.filled_count, total_a, pos.leg_a.no_price)
        pos_b = _fmt_pos(pos.leg_b.filled_count, total_b, pos.leg_b.no_price)

        # Highlight imbalanced legs
        fa, fb = pos.leg_a.filled_count, pos.leg_b.filled_count
        ra, rb = pos.leg_a.resting_count, pos.leg_b.resting_count
        ta, tb = fa + ra, fb + rb
        if fa != fb:
            if fa < fb:
                pos_a = RichText(str(pos_a), style=YELLOW, justify="right")
            else:
                pos_b = RichText(str(pos_b), style=YELLOW, justify="right")
        elif ta != tb:
            if ta < tb:
                pos_a = RichText(str(pos_a), style=YELLOW, justify="right")
            else:
                pos_b = RichText(str(pos_b), style=YELLOW, justify="right")

        q_a = (
            RichText(str(pos.leg_a.queue_position), justify="right")
            if pos.leg_a.queue_position is not None
            else DIM_DASH
        )
        q_b = (
            RichText(str(pos.leg_b.queue_position), justify="right")
            if pos.leg_b.queue_position is not None
            else DIM_DASH
        )
        cpm_a = RichText(format_cpm(pos.leg_a.cpm, pos.leg_a.cpm_partial), justify="right")
        cpm_b = RichText(format_cpm(pos.leg_b.cpm, pos.leg_b.cpm_partial), justify="right")
        eta_a = RichText(
            format_eta(pos.leg_a.eta_minutes, pos.leg_a.cpm_partial), justify="right"
        )
        eta_b = RichText(
            format_eta(pos.leg_b.eta_minutes, pos.leg_b.cpm_partial), justify="right"
        )

        # Locked and Exposure
        locked = pos.locked_profit_cents
        if locked > 0:
            locked_str = RichText(f"${locked / 100:.2f}", style=GREEN, justify="right")
        elif locked == 0:
            locked_str = DIM_DASH
        else:
            locked_str = RichText(f"-${abs(locked) / 100:.2f}", style=RED, justify="right")

        exposure = pos.exposure_cents
        if exposure > 0:
            exposure_str = RichText(f"${exposure / 100:.2f}", style=RED, justify="right")
        else:
            exposure_str = DIM_DASH

        status = _fmt_status(pos.status)
    else:
        pos_a = pos_b = q_a = q_b = DIM_DASH
        cpm_a = cpm_b = eta_a = eta_b = DIM_DASH
        locked_str = exposure_str = DIM_DASH
        status = _fmt_status(self._event_statuses.get(opp.event_ticker, ""))

    if tracker is not None:
        if tracker.is_at_top(opp.ticker_a) is False:
            q_a = RichText(f"!! {q_a}", style=YELLOW, justify="right")
        if tracker.is_at_top(opp.ticker_b) is False:
            q_b = RichText(f"!! {q_b}", style=YELLOW, justify="right")

    # Row 1: team A + shared event-level info
    row1 = (
        dot_a, team_a, league, game_col,
        no_a, vol_a, pos_a, q_a, cpm_a, eta_a,
        edge_str, status, locked_str, exposure_str,
    )

    # Row 2: team B only — shared columns blank
    row2 = (
        dot_b, team_b, "", "",
        no_b, vol_b, pos_b, q_b, cpm_b, eta_b,
        "", "", "", "",
    )

    return row1, row2
```

- [ ] **Step 6: Rewrite `refresh_from_scanner` for two-row layout**

Replace the existing `refresh_from_scanner` method. Key changes:
- Row keys become `{event_ticker}:a` and `{event_ticker}:b`
- Add/remove in pairs
- Pair striping via alternating backgrounds (handled via `_render_line_in_row` override)

```python
def refresh_from_scanner(
    self,
    scanner: ArbitrageScanner | None,
    tracker: TopOfMarketTracker | None = None,
) -> None:
    """Rebuild table rows from current scanner state + position data."""
    if scanner is None:
        return

    all_snaps = scanner.all_snapshots
    current_events = {
        k.value.rsplit(":", 1)[0]
        for k in self.rows
        if k.value is not None and ":" in str(k.value)
    }
    new_events = set(all_snaps.keys())

    # On sort click: clear and re-add in sorted order
    if self._needs_resort:
        self._needs_resort = False
        sorted_opps = sorted(
            all_snaps.values(),
            key=self._sort_key,
            reverse=self._sort_reverse,
        )
        with self.app.batch_update():
            self.clear()
            for opp in sorted_opps:
                row1, row2 = self._build_row_pair(opp, tracker)
                self.add_row(*row1, key=f"{opp.event_ticker}:a")
                self.add_row(*row2, key=f"{opp.event_ticker}:b")
        return

    sorted_opps = sorted(all_snaps.values(), key=lambda o: o.raw_edge, reverse=True)

    dirty = self._dirty_events
    all_dirty = self._all_dirty
    self._dirty_events = set()
    self._all_dirty = False

    with self.app.batch_update():
        # Remove events no longer tracked
        for evt in current_events - new_events:
            self.remove_row(f"{evt}:a")
            self.remove_row(f"{evt}:b")

        for opp in sorted_opps:
            key_a = f"{opp.event_ticker}:a"
            key_b = f"{opp.event_ticker}:b"
            is_new = opp.event_ticker not in current_events

            if is_new:
                row1, row2 = self._build_row_pair(opp, tracker)
                self.add_row(*row1, key=key_a)
                self.add_row(*row2, key=key_b)
            elif all_dirty or opp.event_ticker in dirty:
                row1, row2 = self._build_row_pair(opp, tracker)
                # Update row A
                old_a = self.get_row(key_a)
                for col_idx, value in enumerate(row1):
                    if col_idx < len(old_a) and str(old_a[col_idx]) == str(value):
                        continue
                    col_key = self.ordered_columns[col_idx].key
                    self.update_cell(key_a, col_key, value)
                # Update row B
                old_b = self.get_row(key_b)
                for col_idx, value in enumerate(row2):
                    if col_idx < len(old_b) and str(old_b[col_idx]) == str(value):
                        continue
                    col_key = self.ordered_columns[col_idx].key
                    self.update_cell(key_b, col_key, value)
```

- [ ] **Step 7: Update `_SORT_KEYS` for new column indices**

Sorting operates at the event level (sorts by leg A data for per-leg columns). Column indices match the new 14-column layout:

```python
_SORT_KEYS: dict[int, str] = {
    1: "label",    # Team name (col 1) — sorts by event label
    2: "league",   # Lg (col 2)
    3: "state",    # Game (col 3)
    4: "no_a",     # NO (col 4) — sorts by leg A price
    5: "vol_a",    # Vol (col 5) — sorts by leg A volume
    10: "fee_edge", # Edge (col 10)
}
```

Update `_sort_key` to handle the "label" key using `_leg_labels` (first team name):
```python
if key_name == "label":
    labels = self._leg_labels.get(opp.event_ticker)
    if labels:
        return labels[0].lower()
    return opp.event_ticker.lower()
```

- [ ] **Step 8: Update row key handling in app.py**

The row keys now have `:a` / `:b` suffixes. Create a helper and update ALL handlers that use row keys:

Add helper at module level in app.py:
```python
def _event_ticker_from_row_key(raw_key: str) -> str:
    """Strip :a or :b suffix from two-row layout row keys."""
    return raw_key.rsplit(":", 1)[0] if ":" in raw_key else raw_key
```

Update `on_data_table_row_selected` (line 577):
```python
def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
    if self._scanner is None:
        return
    event_ticker = _event_ticker_from_row_key(str(event.row_key.value))
    opp = self._scanner.get_opportunity(event_ticker)
    if opp is None:
        opp = self._scanner.all_snapshots.get(event_ticker)
    if opp is not None:
        self.push_screen(BidScreen(opp), callback=self._on_bid_confirmed)
```

Update `action_open_in_browser` (line 552):
```python
event_ticker = _event_ticker_from_row_key(str(cell_key.row_key.value))
```

Update `action_remove_game` and any other handler that reads `row_key.value` — grep for `row_key.value` in app.py.

- [ ] **Step 9: Wire `leg_labels` from engine to table in `refresh_opportunities`**

In `refresh_opportunities` (app.py), after `table.update_labels(...)`, add:

```python
table.update_leg_labels(self._engine.game_manager.leg_labels)
```

- [ ] **Step 10: Run all tests**

Run: `.venv/Scripts/python -m pytest tests/test_table_redesign.py tests/test_scanner.py -v`
Expected: All PASS

- [ ] **Step 11: Commit**

```bash
git add src/talos/ui/widgets.py src/talos/ui/app.py tests/test_table_redesign.py
git commit -m "feat: two-row table layout with freshness dots and per-leg team names"
```

---

### Task 4: Portfolio Summary Panel

**Files:**
- Modify: `src/talos/ui/widgets.py` (rewrite `AccountPanel` → `PortfolioPanel`)
- Modify: `src/talos/ui/app.py` (wire new data to panel, update compose)

- [ ] **Step 1: Write failing test for portfolio panel formatting**

Append to `tests/test_table_redesign.py`:

```python
from talos.ui.widgets import _fmt_pnl_with_roi


def test_pnl_with_roi_positive():
    result = _fmt_pnl_with_roi(640, 15600)  # $6.40 P&L on $156 invested
    text = str(result)
    assert "$6.40" in text
    assert "4.1%" in text


def test_pnl_with_roi_zero_invested():
    """Zero invested → no ROI shown."""
    result = _fmt_pnl_with_roi(0, 0)
    text = str(result)
    assert "%" not in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_table_redesign.py::test_pnl_with_roi_positive -v`
Expected: FAIL

- [ ] **Step 3: Implement `_fmt_pnl_with_roi`**

```python
def _fmt_pnl_with_roi(pnl_cents: int, invested_cents: int) -> RichText:
    """Format P&L with ROI percentage: '$6.40 (4.1%)'."""
    dollars = pnl_cents / 100
    if dollars >= 0:
        label = f"${dollars:.2f}"
        style = GREEN
    else:
        label = f"-${abs(dollars):.2f}"
        style = RED
    if invested_cents > 0:
        roi = (pnl_cents / invested_cents) * 100
        label += f" ({roi:.1f}%)"
    return RichText(label, style=style)
```

- [ ] **Step 4: Rewrite `AccountPanel` → `PortfolioPanel`**

Replace `AccountPanel` class with:

```python
class PortfolioPanel(Static):
    """Portfolio summary: cash, locked, exposure, invested, historical P&L."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._cash: int = 0
        self._portfolio: int = 0
        self._locked: float = 0.0
        self._exposure: int = 0
        self._invested: int = 0
        self._pnl_today: int = 0
        self._pnl_yesterday: int = 0
        self._pnl_7d: int = 0
        self._invested_today: int = 0
        self._invested_yesterday: int = 0
        self._invested_7d: int = 0

    def on_mount(self) -> None:
        self._render_content()

    def update_balance(self, balance_cents: int, portfolio_cents: int) -> None:
        self._cash = balance_cents
        self._portfolio = portfolio_cents
        self._render_content()

    def update_portfolio_summary(
        self,
        locked: float,
        exposure: int,
        invested: int,
    ) -> None:
        self._locked = locked
        self._exposure = exposure
        self._invested = invested
        self._render_content()

    def update_pnl(
        self,
        today: int,
        yesterday: int,
        last_7d: int,
        invested_today: int = 0,
        invested_yesterday: int = 0,
        invested_7d: int = 0,
    ) -> None:
        self._pnl_today = today
        self._pnl_yesterday = yesterday
        self._pnl_7d = last_7d
        self._invested_today = invested_today
        self._invested_yesterday = invested_yesterday
        self._invested_7d = invested_7d
        self._render_content()

    def _render_content(self) -> None:
        cash = f"${self._cash / 100:,.2f}"
        locked = f"${self._locked / 100:,.2f}"
        exposure = f"${self._exposure / 100:,.2f}"
        invested = f"${self._invested / 100:,.2f}"

        today = _fmt_pnl_with_roi(self._pnl_today, self._invested_today)
        yesterday = _fmt_pnl_with_roi(self._pnl_yesterday, self._invested_yesterday)
        last_7d = _fmt_pnl_with_roi(self._pnl_7d, self._invested_7d)

        self.update(
            f"PORTFOLIO\n\n"
            f"Cash:      {cash}\n"
            f"Locked In: {locked}\n"
            f"Exposure:  {exposure}\n"
            f"Invested:  {invested}\n"
            f"───────────────────\n"
            f"Today:     {today}\n"
            f"Yesterday: {yesterday}\n"
            f"Last 7d:   {last_7d}"
        )
```

- [ ] **Step 5: Update `compose()` and all references in app.py**

Migration checklist:
1. Change import: `from talos.ui.widgets import AccountPanel` → `PortfolioPanel`
2. In `compose()` (app.py:80): `yield AccountPanel(id="account-panel")` → `yield PortfolioPanel(id="account-panel")`
3. Grep for `query_one(AccountPanel)` and `AccountPanel` in app.py — update ALL references to `PortfolioPanel`
4. The `update_balance` method signature is unchanged, so existing balance polling continues to work.

- [ ] **Step 6: Wire locked/exposure/invested summaries from engine**

In `_recompute_and_push_positions()` (or wherever positions are pushed to the table), after pushing positions, compute sums:

```python
panel = self.query_one(PortfolioPanel)
summaries = self._engine.position_summaries
total_locked = sum(s.locked_profit_cents for s in summaries)
total_exposure = sum(s.exposure_cents for s in summaries)
total_invested = sum(
    s.leg_a.total_fill_cost + s.leg_b.total_fill_cost for s in summaries
)
panel.update_portfolio_summary(total_locked, total_exposure, total_invested)
```

- [ ] **Step 7: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_table_redesign.py -v`
Expected: All PASS

- [ ] **Step 8: Commit**

```bash
git add src/talos/ui/widgets.py src/talos/ui/app.py tests/test_table_redesign.py
git commit -m "feat: portfolio summary panel with locked/exposure/invested"
```

---

### Task 5: Settlement P&L Tracker

**Files:**
- Create: `src/talos/settlement_tracker.py`
- Create: `tests/test_settlement_tracker.py`
- Modify: `src/talos/ui/app.py` (wire settlement polling)

- [ ] **Step 1: Write failing test for settlement aggregation**

```python
# tests/test_settlement_tracker.py
"""Tests for settlement P&L aggregation."""

from datetime import datetime
from zoneinfo import ZoneInfo

from talos.settlement_tracker import aggregate_settlements

PT = ZoneInfo("America/Los_Angeles")


def test_aggregate_today():
    """Settlements from today (PT) sum correctly."""
    now_pt = datetime.now(PT)
    today_str = now_pt.strftime("%Y-%m-%dT%H:%M:%SZ")

    settlements = [
        {"revenue": 640, "no_total_cost": 400, "settled_time": today_str, "event_ticker": "E1"},
        {"revenue": 320, "no_total_cost": 300, "settled_time": today_str, "event_ticker": "E2"},
    ]

    result = aggregate_settlements(settlements, now_pt)
    assert result["today_pnl"] == 960  # 640 + 320 revenue
    assert result["today_invested"] == 700  # 400 + 300 cost


def test_aggregate_empty():
    now_pt = datetime.now(PT)
    result = aggregate_settlements([], now_pt)
    assert result["today_pnl"] == 0
    assert result["yesterday_pnl"] == 0
    assert result["week_pnl"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_settlement_tracker.py::test_aggregate_today -v`
Expected: FAIL

- [ ] **Step 3: Implement `settlement_tracker.py`**

```python
"""Settlement P&L tracker — aggregates Kalshi settlements by time window."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")


def aggregate_settlements(
    settlements: list[dict],
    now_pt: datetime | None = None,
) -> dict[str, int]:
    """Aggregate settlement revenue and cost by time window.

    Returns dict with keys: today_pnl, today_invested, yesterday_pnl,
    yesterday_invested, week_pnl, week_invested.
    """
    if now_pt is None:
        now_pt = datetime.now(PT)

    today_start = now_pt.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    week_start = today_start - timedelta(days=6)

    buckets: dict[str, int] = {
        "today_pnl": 0,
        "today_invested": 0,
        "yesterday_pnl": 0,
        "yesterday_invested": 0,
        "week_pnl": 0,
        "week_invested": 0,
    }

    for s in settlements:
        settled_str = s.get("settled_time", "")
        if not settled_str:
            continue
        try:
            settled_dt = datetime.fromisoformat(
                settled_str.replace("Z", "+00:00")
            ).astimezone(PT)
        except (ValueError, TypeError):
            continue

        revenue = s.get("revenue", 0)
        cost = s.get("no_total_cost", 0) + s.get("yes_total_cost", 0)

        if settled_dt >= today_start:
            buckets["today_pnl"] += revenue
            buckets["today_invested"] += cost
        if yesterday_start <= settled_dt < today_start:
            buckets["yesterday_pnl"] += revenue
            buckets["yesterday_invested"] += cost
        if settled_dt >= week_start:
            buckets["week_pnl"] += revenue
            buckets["week_invested"] += cost

    return buckets
```

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_settlement_tracker.py -v`
Expected: All PASS

- [ ] **Step 5: Wire settlement polling in app.py**

Add a polling interval that fetches settlements and pushes to the portfolio panel:

```python
# In on_mount, add:
self.set_interval(300.0, self._poll_settlements)  # every 5 minutes

# New method:
@work(thread=False, exclusive=True, group="settlements")
async def _poll_settlements(self) -> None:
    if self._engine is None:
        return
    try:
        settlements = await self._engine.rest.get_settlements(limit=200)
        from talos.settlement_tracker import aggregate_settlements
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
    except Exception:
        pass  # Non-critical — don't crash for P&L display
```

- [ ] **Step 6: Commit**

```bash
git add src/talos/settlement_tracker.py tests/test_settlement_tracker.py src/talos/ui/app.py
git commit -m "feat: settlement P&L tracker with today/yesterday/7d windows"
```

---

### Task 6: Internal P&L Reconciliation

**Files:**
- Modify: `src/talos/settlement_tracker.py` (add reconciliation logic)
- Modify: `tests/test_settlement_tracker.py`

- [ ] **Step 1: Write failing test for reconciliation**

```python
# Append to tests/test_settlement_tracker.py

from talos.settlement_tracker import reconcile_event


def test_reconcile_matching():
    """Our P&L matches Kalshi's — no discrepancy."""
    result = reconcile_event(
        our_revenue=640,
        kalshi_revenue=640,
        event_ticker="EVT-TEST",
    )
    assert result is None  # No discrepancy


def test_reconcile_mismatch():
    """Our P&L differs from Kalshi's — returns discrepancy."""
    result = reconcile_event(
        our_revenue=640,
        kalshi_revenue=600,
        event_ticker="EVT-TEST",
    )
    assert result is not None
    assert result["difference"] == 40
    assert result["event_ticker"] == "EVT-TEST"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_settlement_tracker.py::test_reconcile_matching -v`
Expected: FAIL

- [ ] **Step 3: Implement `reconcile_event`**

Add to `settlement_tracker.py`:

```python
def reconcile_event(
    our_revenue: int,
    kalshi_revenue: int,
    event_ticker: str,
) -> dict[str, Any] | None:
    """Compare our expected revenue vs Kalshi's actual.

    Returns None if they match, or a discrepancy dict if they differ.
    """
    if our_revenue == kalshi_revenue:
        return None
    return {
        "event_ticker": event_ticker,
        "our_revenue": our_revenue,
        "kalshi_revenue": kalshi_revenue,
        "difference": our_revenue - kalshi_revenue,
    }
```

- [ ] **Step 4: Wire reconciliation into settlement polling**

In `_poll_settlements` in `app.py`, after aggregating, compare each settled event against our cached position summaries. Use `self._engine.position_summaries` (which is `list[EventPositionSummary]`) — NOT `position_ledger`:

```python
# After aggregation, check for discrepancies
from talos.settlement_tracker import reconcile_event
summaries_by_event = {s.event_ticker: s for s in self._engine.position_summaries}
for s in settlements:
    pos = summaries_by_event.get(s.event_ticker)
    if pos is None:
        continue
    # Compare our locked profit (guaranteed arb profit) vs Kalshi's net revenue
    # Note: this comparison is only meaningful for fully-matched arb positions.
    # Partially-filled events will naturally differ — only flag large discrepancies.
    our_expected = int(pos.locked_profit_cents)
    disc = reconcile_event(our_expected, s.revenue, s.event_ticker)
    if disc is not None and abs(disc["difference"]) > 5:  # >5¢ threshold
        log = self.query_one(ActivityLog)
        log.log_activity(
            f"P&L DISCREPANCY {s.event_ticker}: "
            f"ours=${disc['our_revenue']/100:.2f} "
            f"kalshi=${disc['kalshi_revenue']/100:.2f} "
            f"diff=${disc['difference']/100:.2f}",
            severity="warning",
        )
```

- [ ] **Step 5: Run all tests**

Run: `.venv/Scripts/python -m pytest tests/test_settlement_tracker.py tests/test_table_redesign.py -v`
Expected: All PASS

- [ ] **Step 6: Run full test suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: All existing tests still pass

- [ ] **Step 7: Commit**

```bash
git add src/talos/settlement_tracker.py tests/test_settlement_tracker.py src/talos/ui/app.py
git commit -m "feat: internal P&L reconciliation — log discrepancies vs Kalshi"
```

---

## Execution Notes

- **Task 3 is the riskiest** — it rewrites the core table rendering. Run the app after this task to visually verify before proceeding.
- **The `_render_line_in_row` override** for vertical separators (existing code) should continue to work with the new column count. Verify during Task 3.
- **Pair striping** in the two-row layout: `zebra_stripes = False` is set in `on_mount`. To get per-pair alternating backgrounds, enhance `_render_line_in_row` to check if the row index divided by 2 is odd/even, and apply a subtle background tint to even pairs. The separator line on row 2 (`:b` rows) visually groups pairs.
- **The `open_in_browser` action** (app.py:552-565) uses `row_key.value` — already handled in Task 3 Step 8 via the `_event_ticker_from_row_key` helper.
- **Settlement endpoint** already exists: `rest_client.py:447-467` has `get_settlements()` returning `list[Settlement]` with `revenue`, `settled_time`, `event_ticker` fields. Verified in codebase.
- **`_render_content` uses f-string with RichText objects** — the `PortfolioPanel._render_content()` method passes `_fmt_pnl_with_roi()` results (RichText objects) into an f-string. This will call `__str__()` on them, losing color. For Task 4, either use plain string formatting in the panel or build the display using Rich's `Text` composition instead of f-strings.
