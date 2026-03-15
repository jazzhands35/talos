# Scanner Integration Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Press `c` to discover all open arb-eligible events from Kalshi, view them in a modal with sport/league/date/time/volume, and select which to add.

**Architecture:** `GameManager.scan_events()` fetches events from all known series concurrently (semaphore-limited), filters to 2-market pairs not already monitored. `ScanScreen` modal displays results in a DataTable with Space-to-toggle selection. App wires the keybinding, resolves game status for dates, and adds selected events.

**Tech Stack:** Python 3.12+, Textual (ModalScreen, DataTable), httpx (async), asyncio.Semaphore

**Spec:** `docs/plans/2026-03-14-scanner-integration-design.md`

---

## Task 1: `GameManager.scan_events()`

**Files:**
- Modify: `src/talos/game_manager.py`
- Modify: `tests/test_game_status.py` (or create `tests/test_scanner_integration.py`)

- [ ] **Step 1: Write failing test for scan_events**

```python
# tests/test_scanner_integration.py
"""Tests for the integrated event scanner."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from talos.game_manager import GameManager, SCAN_SERIES


class TestScanSeries:
    def test_scan_series_contains_known_series(self) -> None:
        assert "KXNHLGAME" in SCAN_SERIES
        assert "KXAHLGAME" in SCAN_SERIES
        assert "KXCS2GAME" in SCAN_SERIES
        assert "KXATPMATCH" in SCAN_SERIES

    def test_scan_series_no_duplicates(self) -> None:
        assert len(SCAN_SERIES) == len(set(SCAN_SERIES))


class TestScanEvents:
    @pytest.mark.asyncio
    async def test_scan_returns_two_market_events(self) -> None:
        rest = AsyncMock()
        feed = AsyncMock()
        scanner_mock = AsyncMock()
        scanner_mock.pairs = []

        from talos.models.market import Event, Market

        event_2m = Event(
            event_ticker="KXNHLGAME-26MAR16-TEST",
            series_ticker="KXNHLGAME",
            title="Test Game",
            sub_title="TST at OPP (Mar 16)",
            category="Sports",
            mutually_exclusive=True,
            markets=[
                Market(ticker="KXNHLGAME-26MAR16-TEST-TST", event_ticker="KXNHLGAME-26MAR16-TEST", title="TST", status="active"),
                Market(ticker="KXNHLGAME-26MAR16-TEST-OPP", event_ticker="KXNHLGAME-26MAR16-TEST", title="OPP", status="active"),
            ],
        )
        event_3m = Event(
            event_ticker="KXNHLGAME-26MAR16-BAD",
            series_ticker="KXNHLGAME",
            title="Bad Game",
            category="Sports",
            markets=[
                Market(ticker="t1", event_ticker="e1", title="A", status="active"),
                Market(ticker="t2", event_ticker="e1", title="B", status="active"),
                Market(ticker="t3", event_ticker="e1", title="C", status="active"),
            ],
        )
        rest.get_events.return_value = [event_2m, event_3m]

        gm = GameManager(rest, feed, scanner_mock)
        results = await gm.scan_events()
        assert len(results) == 1
        assert results[0].event_ticker == "KXNHLGAME-26MAR16-TEST"

    @pytest.mark.asyncio
    async def test_scan_excludes_already_monitored(self) -> None:
        rest = AsyncMock()
        feed = AsyncMock()
        scanner_mock = AsyncMock()
        scanner_mock.pairs = []

        from talos.models.market import Event, Market
        from talos.models.strategy import ArbPair

        event = Event(
            event_ticker="KXNHLGAME-26MAR16-ALREADY",
            series_ticker="KXNHLGAME",
            title="Already Monitored",
            category="Sports",
            mutually_exclusive=True,
            markets=[
                Market(ticker="t1", event_ticker="e1", title="A", status="active"),
                Market(ticker="t2", event_ticker="e1", title="B", status="active"),
            ],
        )
        rest.get_events.return_value = [event]

        gm = GameManager(rest, feed, scanner_mock)
        # Simulate this event already being monitored
        gm._games["KXNHLGAME-26MAR16-ALREADY"] = ArbPair(
            event_ticker="KXNHLGAME-26MAR16-ALREADY",
            ticker_a="t1", ticker_b="t2",
        )
        results = await gm.scan_events()
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_scan_handles_api_failure(self) -> None:
        rest = AsyncMock()
        feed = AsyncMock()
        scanner_mock = AsyncMock()
        scanner_mock.pairs = []

        rest.get_events.side_effect = RuntimeError("API down")

        gm = GameManager(rest, feed, scanner_mock)
        results = await gm.scan_events()
        assert results == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_scanner_integration.py -v`
Expected: FAIL — `cannot import name 'SCAN_SERIES'`

- [ ] **Step 3: Implement scan_events and SCAN_SERIES**

Add to `src/talos/game_manager.py`:

```python
import asyncio

SCAN_SERIES = [
    # Major US leagues (ESPN)
    "KXNHLGAME", "KXNBAGAME", "KXMLBGAME", "KXNFLGAME", "KXWNBAGAME",
    "KXCFBGAME", "KXCBBGAME", "KXMLSGAME", "KXEPLGAME",
    # Minor leagues (Odds API)
    "KXAHLGAME",
    # Esports (PandaScore)
    "KXLOLGAME", "KXCS2GAME", "KXVALGAME", "KXDOTA2GAME", "KXCODGAME",
    # Tennis
    "KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH",
    "KXATPDOUBLES",
]
```

Add method to `GameManager`:

```python
async def scan_events(self) -> list[Event]:
    """Discover all open arb-eligible events not already monitored."""
    from talos.models.market import Event

    active_tickers = {p.event_ticker for p in self.active_games}
    sem = asyncio.Semaphore(4)

    async def fetch_series(series: str) -> list[Event]:
        async with sem:
            try:
                return await self._rest.get_events(
                    series_ticker=series,
                    status="open",
                    with_nested_markets=True,
                    limit=200,
                )
            except Exception:
                logger.warning("scan_series_failed", series=series, exc_info=True)
                return []

    all_results = await asyncio.gather(
        *(fetch_series(s) for s in SCAN_SERIES)
    )

    events: list[Event] = []
    for batch in all_results:
        for event in batch:
            if event.event_ticker in active_tickers:
                continue
            if len(event.markets) != 2:
                continue
            # Skip if both markets are settled/determined
            if all(m.status in ("settled", "determined") for m in event.markets):
                continue
            events.append(event)
    return events
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_scanner_integration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/talos/game_manager.py tests/test_scanner_integration.py
git commit -m "feat(scanner): add scan_events() to discover arb-eligible events"
```

---

## Task 2: Add `KXWTAMATCH` to `_SPORT_LEAGUE`

**Files:**
- Modify: `src/talos/ui/widgets.py`

- [ ] **Step 1: Add missing mapping**

In `_SPORT_LEAGUE` dict in `widgets.py`, add:

```python
"KXWTAMATCH": ("TEN", "WTA"),
```

- [ ] **Step 2: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add src/talos/ui/widgets.py
git commit -m "fix: add KXWTAMATCH to sport/league mapping"
```

---

## Task 3: `ScanScreen` Modal

**Files:**
- Modify: `src/talos/ui/screens.py`

- [ ] **Step 1: Implement ScanScreen**

Add to `src/talos/ui/screens.py`:

```python
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from textual.widgets import DataTable
from rich.text import Text as RichText

from talos.game_status import GameStatus, _extract_date_from_ticker

_PT = ZoneInfo("America/Los_Angeles")

# Duplicated from widgets.py to avoid circular import — just the sport/league map
_SPORT_LEAGUE: dict[str, tuple[str, str]] = {
    "KXNHLGAME": ("HOC", "NHL"),
    "KXNBAGAME": ("BKB", "NBA"),
    "KXMLBGAME": ("BSB", "MLB"),
    "KXNFLGAME": ("FTB", "NFL"),
    "KXWNBAGAME": ("BKB", "WNBA"),
    "KXCFBGAME": ("FTB", "NCAAF"),
    "KXCBBGAME": ("BKB", "NCAAB"),
    "KXMLSGAME": ("SOC", "MLS"),
    "KXEPLGAME": ("SOC", "EPL"),
    "KXAHLGAME": ("HOC", "AHL"),
    "KXLOLGAME": ("ESP", "LoL"),
    "KXCS2GAME": ("ESP", "CS2"),
    "KXVALGAME": ("ESP", "VAL"),
    "KXDOTA2GAME": ("ESP", "DOTA"),
    "KXCODGAME": ("ESP", "COD"),
    "KXATPMATCH": ("TEN", "ATP"),
    "KXATPDOUBLES": ("TEN", "ATP"),
    "KXATPCHALLENGERMATCH": ("TEN", "ATPC"),
    "KXWTACHALLENGERMATCH": ("TEN", "WTAC"),
    "KXWTAMATCH": ("TEN", "WTA"),
}


class ScanScreen(ModalScreen[list[str] | None]):
    """Modal for scanning and selecting events to add."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("a", "select_all", "Select All"),
        ("enter", "add_selected", "Add Selected"),
        ("space", "toggle_select", "Toggle"),
    ]

    DEFAULT_CSS = """
    ScanScreen {
        align: center middle;
    }
    #scan-dialog {
        width: 90%;
        height: 85%;
        border: thick $surface;
        background: $surface;
        padding: 1 2;
    }
    #scan-table {
        height: 1fr;
    }
    """

    def __init__(
        self,
        events: list,  # list[Event]
        statuses: dict[str, GameStatus] | None = None,
    ) -> None:
        super().__init__()
        self._events = events
        self._statuses = statuses or {}
        self._selected: set[str] = set()

    def compose(self) -> ComposeResult:
        with Vertical(id="scan-dialog"):
            yield Label(
                f"Scan Results — {len(self._events)} events found  "
                "[Space=Toggle  Enter=Add Selected  A=Select All  Esc=Cancel]",
                classes="modal-title",
            )
            yield DataTable(id="scan-table")

    def on_mount(self) -> None:
        table = self.query_one("#scan-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        r = "right"
        table.add_column("✓", width=2)
        table.add_column("Spt", width=4)
        table.add_column("Lg", width=5)
        table.add_column("Date", width=6)
        table.add_column("Time", width=8)
        table.add_column("Event")
        table.add_column(RichText("V-A", justify=r), width=7)
        table.add_column(RichText("V-B", justify=r), width=7)

        for event in self._sorted_events():
            prefix = event.series_ticker or event.event_ticker.split("-")[0]
            sport, league = _SPORT_LEAGUE.get(prefix, ("—", "—"))

            # Date/time from resolver or ticker fallback
            gs = self._statuses.get(event.event_ticker)
            if gs and gs.scheduled_start:
                pt = gs.scheduled_start.astimezone(_PT)
                date_str = pt.strftime("%m/%d")
                time_str = pt.strftime("%I:%M %p").lstrip("0")
            else:
                date_code = _extract_date_from_ticker(event.event_ticker)
                if date_code:
                    date_str = f"{date_code[4:6]}/{date_code[6:8]}"
                else:
                    date_str = "—"
                time_str = "—"

            # Label from sub_title
            label = event.sub_title or event.title
            if "(" in label:
                label = label[: label.rfind("(")].strip()

            # Volume from nested markets
            vol_a = event.markets[0].volume_24h or 0 if len(event.markets) > 0 else 0
            vol_b = event.markets[1].volume_24h or 0 if len(event.markets) > 1 else 0
            va_str = f"{vol_a / 1000:.1f}k" if vol_a >= 1000 else str(vol_a) if vol_a else "—"
            vb_str = f"{vol_b / 1000:.1f}k" if vol_b >= 1000 else str(vol_b) if vol_b else "—"

            table.add_row(
                " ",  # checkbox placeholder
                sport, league, date_str, time_str, label,
                RichText(va_str, justify="right"),
                RichText(vb_str, justify="right"),
                key=event.event_ticker,
            )

    def _sorted_events(self) -> list:
        """Sort events by scheduled start time (soonest first)."""
        def sort_key(e):
            gs = self._statuses.get(e.event_ticker)
            if gs and gs.scheduled_start:
                return gs.scheduled_start.timestamp()
            date_code = _extract_date_from_ticker(e.event_ticker)
            if date_code:
                return float(date_code)  # YYYYMMDD as number
            return float("inf")
        return sorted(self._events, key=sort_key)

    def _refresh_checkmarks(self) -> None:
        table = self.query_one("#scan-table", DataTable)
        check_col = table.ordered_columns[0].key
        for row_key in table.rows:
            ticker = str(row_key.value)
            mark = "✓" if ticker in self._selected else " "
            table.update_cell(row_key, check_col, mark)

    def action_toggle_select(self) -> None:
        table = self.query_one("#scan-table", DataTable)
        if table.row_count == 0:
            return
        cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        ticker = str(cell_key.row_key.value)
        if ticker in self._selected:
            self._selected.discard(ticker)
        else:
            self._selected.add(ticker)
        self._refresh_checkmarks()

    def action_select_all(self) -> None:
        table = self.query_one("#scan-table", DataTable)
        if len(self._selected) == table.row_count:
            self._selected.clear()  # toggle: deselect all if all selected
        else:
            for row_key in table.rows:
                self._selected.add(str(row_key.value))
        self._refresh_checkmarks()

    def action_add_selected(self) -> None:
        if self._selected:
            self.dismiss(list(self._selected))
        # If nothing selected, do nothing (don't close)

    def action_cancel(self) -> None:
        self.dismiss(None)
```

- [ ] **Step 2: Run existing tests to make sure nothing broke**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add src/talos/ui/screens.py
git commit -m "feat(scanner): add ScanScreen modal with toggle selection"
```

---

## Task 4: Wire into TalosApp

**Files:**
- Modify: `src/talos/ui/app.py`

- [ ] **Step 1: Add import and keybinding**

Add import:
```python
from talos.ui.screens import AddGamesScreen, AutoAcceptScreen, BidScreen, ScanScreen, UnitSizeScreen
```
(ScanScreen added to existing import line)

Add to BINDINGS:
```python
("c", "scan", "Scan"),
```

- [ ] **Step 2: Add action_scan method**

```python
def action_scan(self) -> None:
    """Scan for new arb-eligible events."""
    if self._engine is not None:
        self._run_scan()

@work(thread=False, exclusive=True, group="scan")
async def _run_scan(self) -> None:
    if self._engine is None:
        return
    self.notify("Scanning for events...")
    events = await self._engine.game_manager.scan_events()
    if not events:
        self.notify("No new events found", severity="information")
        return

    # Resolve game status for date/time columns
    statuses: dict[str, GameStatus] = {}
    resolver = self._engine.game_status_resolver
    if resolver is not None:
        batch = [
            (e.event_ticker, e.sub_title or "")
            for e in events
        ]
        await resolver.resolve_batch(batch)
        for e in events:
            gs = resolver.get(e.event_ticker)
            if gs is not None:
                statuses[e.event_ticker] = gs

    selected = await self.push_screen_wait(ScanScreen(events, statuses))
    if selected and self._engine is not None:
        await self._engine.add_games(selected)
        self.notify(f"Added {len(selected)} event(s)")
```

Add the `GameStatus` import at top:
```python
from talos.game_status import GameStatus
```

- [ ] **Step 3: Run full test suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/talos/ui/app.py
git commit -m "feat(scanner): wire scan keybinding and modal into TalosApp"
```

---

## Task 5: Smoke Test

- [ ] **Step 1: Test scan_events with real API**

```bash
.venv/Scripts/python -c "
import asyncio, os
from pathlib import Path
for line in Path('.env').read_text().splitlines():
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, _, v = line.partition('=')
    os.environ[k.strip()] = v.strip().strip('\"')

from talos.config import KalshiConfig
from talos.auth import KalshiAuth
from talos.rest_client import KalshiRESTClient
from talos.game_manager import GameManager
from unittest.mock import MagicMock

async def main():
    config = KalshiConfig.from_env()
    auth = KalshiAuth(config.key_id, config.private_key_path)
    rest = KalshiRESTClient(auth, config)
    gm = GameManager(rest, MagicMock(), MagicMock())
    gm._scanner = MagicMock()
    gm._scanner.pairs = []

    events = await gm.scan_events()
    print(f'Found {len(events)} events')
    for e in events[:10]:
        label = e.sub_title or e.title
        print(f'  {e.series_ticker:25s} {e.event_ticker:45s} {label}')
    await rest.close()

asyncio.run(main())
"
```

Expected: Lists 50+ events across NHL, AHL, tennis, esports, etc.

- [ ] **Step 2: Launch Talos and press `c`**

Run: `.venv/Scripts/python -m talos`

Press `c` — verify modal opens with scan results, Space toggles selection, Enter adds selected events, Escape closes.

- [ ] **Step 3: Full test suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: ALL tests pass

- [ ] **Step 4: Final commit if adjustments needed**

```bash
git add -u
git commit -m "fix(scanner): adjustments from smoke testing"
```
