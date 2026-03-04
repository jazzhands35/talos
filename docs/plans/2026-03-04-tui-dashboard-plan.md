# TUI Dashboard Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a real-time Textual terminal dashboard showing arbitrage opportunities, portfolio state, and order activity with modal dialogs for adding games and placing bids.

**Architecture:** Single-screen Textual `App` that orchestrates Layers 1-3. The app owns startup, wires callbacks, and runs two polling loops — 500ms for the opportunities table (reads scanner state) and 10s for REST data (balance, positions, orders). Modals handle user input. Catppuccin Mocha color theme via TCSS.

**Tech Stack:** Python 3.12+, Textual >=1.0, structlog, Pydantic v2

**Design doc:** `docs/plans/2026-03-04-tui-dashboard-design.md`

---

## Context for implementers

### How Textual apps work

Textual is an async TUI framework. Key concepts:

- **`App`** — the main application class. `compose()` yields widgets, `on_mount()` runs setup logic.
- **`Screen`** / **`ModalScreen`** — overlay screens. `push_screen()` shows them, `dismiss()` closes them.
- **Widgets** — `DataTable`, `Static`, `Button`, `Input`, `TextArea`, `Label`, `Header`, `Footer`.
- **CSS (TCSS)** — Textual uses a CSS-like language for styling. Set via `CSS` class attribute or `CSS_PATH`.
- **`set_interval(seconds, callback)`** — runs a callback periodically (async-safe).
- **`run_test()`** — headless testing. Returns a `Pilot` for simulating key presses and clicks.
- **`@work`** decorator — runs async tasks in a worker thread without blocking the UI.

### Key files to understand

- `src/talos/scanner.py` — `ArbitrageScanner.opportunities` returns `list[Opportunity]` sorted by edge desc
- `src/talos/game_manager.py` — `GameManager.add_game(url)` returns `ArbPair`, `add_games(urls)` returns `list[ArbPair]`
- `src/talos/market_feed.py:36` — `on_book_update` callback hook, `start()` at line 75
- `src/talos/rest_client.py` — `get_balance()` (line 210), `get_positions()` (line 214), `get_orders()` (line 186), `create_order()` (line 138)
- `src/talos/models/strategy.py` — `Opportunity` model fields: `event_ticker`, `ticker_a`, `ticker_b`, `no_a`, `no_b`, `qty_a`, `qty_b`, `raw_edge`, `tradeable_qty`, `timestamp`
- `src/talos/models/portfolio.py` — `Balance` (balance, portfolio_value), `Position` (ticker, position, market_exposure)
- `src/talos/models/order.py` — `Order` (order_id, ticker, side, price, count, remaining_count, status, created_time)
- `src/talos/config.py:42-78` — `KalshiConfig.from_env()` loads from env vars

### Running tests

```bash
.venv/Scripts/python -m pytest tests/test_ui.py -v          # UI tests
.venv/Scripts/python -m pytest -v                             # all tests
```

### Catppuccin Mocha palette (used throughout)

| Token | Hex | Use |
|-------|-----|-----|
| Base | `#1e1e2e` | App background |
| Surface | `#313244` | Panel backgrounds |
| Overlay | `#45475a` | Borders |
| Text | `#cdd6f4` | Primary text |
| Subtext | `#a6adc8` | Dimmed text |
| Blue | `#89b4fa` | Accents, headers |
| Green | `#a6e3a1` | Positive edge, connected, filled |
| Red | `#f38ba8` | Errors, disconnected, cancelled |
| Yellow | `#f9e2af` | Warnings, stale, pending |
| Mauve | `#cba6f7` | Buttons, interactive |

---

## Task 1: Theme module + entry point + package scaffold

Create the UI package structure, Catppuccin Mocha theme constants, and app entry point.

**Files:**
- Create: `src/talos/ui/__init__.py`
- Create: `src/talos/ui/theme.py`
- Create: `src/talos/__main__.py`

**Step 1: Create the UI package**

Create `src/talos/ui/__init__.py`:

```python
"""Talos TUI dashboard."""
```

**Step 2: Create the theme module**

Create `src/talos/ui/theme.py`:

```python
"""Catppuccin Mocha theme for Talos TUI."""

from __future__ import annotations

# Catppuccin Mocha palette
BASE = "#1e1e2e"
MANTLE = "#181825"
CRUST = "#11111b"
SURFACE0 = "#313244"
SURFACE1 = "#45475a"
SURFACE2 = "#585b70"
OVERLAY0 = "#6c7086"
TEXT = "#cdd6f4"
SUBTEXT0 = "#a6adc8"
SUBTEXT1 = "#bac2de"
BLUE = "#89b4fa"
GREEN = "#a6e3a1"
RED = "#f38ba8"
YELLOW = "#f9e2af"
MAUVE = "#cba6f7"
PEACH = "#fab387"
LAVENDER = "#b4befe"

APP_CSS = """
Screen {
    background: """ + BASE + """;
    color: """ + TEXT + """;
}

Header {
    background: """ + MANTLE + """;
    color: """ + BLUE + """;
    dock: top;
    height: 1;
}

Footer {
    background: """ + MANTLE + """;
    dock: bottom;
}

#opportunities-table {
    height: 1fr;
    min-height: 8;
    border: solid """ + SURFACE1 + """;
    background: """ + BASE + """;
}

#bottom-panels {
    height: auto;
    max-height: 12;
    layout: horizontal;
}

#account-panel {
    width: 2fr;
    border: solid """ + SURFACE1 + """;
    background: """ + SURFACE0 + """;
    padding: 0 1;
    height: auto;
    max-height: 12;
}

#order-log {
    width: 3fr;
    border: solid """ + SURFACE1 + """;
    background: """ + SURFACE0 + """;
    padding: 0 1;
    height: auto;
    max-height: 12;
}

.panel-title {
    color: """ + BLUE + """;
    text-style: bold;
}

.dim-row {
    color: """ + OVERLAY0 + """;
}

.edge-positive {
    color: """ + GREEN + """;
}

.status-connected {
    color: """ + GREEN + """;
}

.status-disconnected {
    color: """ + RED + """;
}

.order-filled {
    color: """ + GREEN + """;
}

.order-open {
    color: """ + YELLOW + """;
}

.order-cancelled {
    color: """ + RED + """;
}

/* Modal styling */
ModalScreen {
    align: center middle;
}

#modal-dialog {
    width: 60;
    height: auto;
    max-height: 80%;
    border: thick """ + SURFACE1 + """;
    background: """ + SURFACE0 + """;
    padding: 1 2;
}

#modal-dialog Label {
    width: 100%;
    margin: 0 0 1 0;
}

#modal-dialog TextArea {
    height: 8;
    margin: 0 0 1 0;
}

#modal-dialog Input {
    margin: 0 0 1 0;
}

#modal-buttons {
    layout: horizontal;
    height: auto;
    align: right middle;
}

#modal-buttons Button {
    margin: 0 0 0 1;
}

.modal-title {
    color: """ + BLUE + """;
    text-style: bold;
    margin: 0 0 1 0;
}

.modal-error {
    color: """ + RED + """;
    margin: 0 0 1 0;
}
"""
```

**Step 3: Create the entry point**

Create `src/talos/__main__.py`:

```python
"""Entry point: python -m talos."""

from talos.ui.app import TalosApp


def main() -> None:
    """Launch the Talos dashboard."""
    app = TalosApp()
    app.run()


if __name__ == "__main__":
    main()
```

**Step 4: Verify the package imports work**

Run: `.venv/Scripts/python -c "from talos.ui.theme import APP_CSS; print('OK')"`
Expected: `OK`

**Step 5: Commit**

```bash
git add src/talos/ui/ src/talos/__main__.py
git commit -m "feat: add UI package scaffold, Catppuccin Mocha theme, entry point"
```

---

## Task 2: App shell with opportunities DataTable

Build the minimal Textual App with the main layout — header, opportunities table (empty), bottom panels (placeholder), footer. Verify it mounts.

**Files:**
- Create: `src/talos/ui/app.py`
- Create: `src/talos/ui/widgets.py`
- Create: `tests/test_ui.py`

**Step 1: Write the test**

Create `tests/test_ui.py`:

```python
"""Tests for Talos TUI dashboard."""

from __future__ import annotations

import pytest

from talos.ui.app import TalosApp


class TestAppMount:
    async def test_app_mounts_without_error(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            assert app.query_one("#opportunities-table") is not None

    async def test_app_has_header_and_footer(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            from textual.widgets import Footer, Header

            assert len(app.query(Header)) == 1
            assert len(app.query(Footer)) == 1

    async def test_app_has_bottom_panels(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            assert app.query_one("#account-panel") is not None
            assert app.query_one("#order-log") is not None
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py -v`
Expected: FAIL (ImportError — `talos.ui.app` has no `TalosApp`)

**Step 3: Create the widgets module**

Create `src/talos/ui/widgets.py`:

```python
"""Dashboard widgets for Talos TUI."""

from __future__ import annotations

from textual.widgets import DataTable, Static


class OpportunitiesTable(DataTable):
    """Live-updating arbitrage opportunities table."""

    DEFAULT_CSS = """
    OpportunitiesTable {
        height: 1fr;
    }
    """

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_columns(
            "Event", "NO-A", "NO-B", "Cost", "Edge", "Qty", "Profit", ""
        )


class AccountPanel(Static):
    """Displays balance and open positions."""

    def on_mount(self) -> None:
        self.update("ACCOUNT\n\nCash: —\nPortfolio: —")


class OrderLog(Static):
    """Scrollable log of recent orders."""

    def on_mount(self) -> None:
        self.update("ORDERS\n\nNo orders yet")
```

**Step 4: Create the app module**

Create `src/talos/ui/app.py`:

```python
"""Main Talos TUI application."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header

from talos.ui.theme import APP_CSS
from talos.ui.widgets import AccountPanel, OpportunitiesTable, OrderLog


class TalosApp(App):
    """Talos arbitrage trading dashboard."""

    CSS = APP_CSS
    TITLE = "TALOS"
    BINDINGS = [
        ("a", "add_games", "Add Games"),
        ("d", "remove_game", "Remove Game"),
        ("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield OpportunitiesTable(id="opportunities-table")
        with Horizontal(id="bottom-panels"):
            yield AccountPanel(id="account-panel")
            yield OrderLog(id="order-log")
        yield Footer()

    def action_add_games(self) -> None:
        """Placeholder — will open Add Games modal."""

    def action_remove_game(self) -> None:
        """Placeholder — will remove selected game."""
```

**Step 5: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py -v`
Expected: All PASS

**Step 6: Run full suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: All PASS (138 existing + new UI tests)

**Step 7: Commit**

```bash
git add src/talos/ui/app.py src/talos/ui/widgets.py tests/test_ui.py
git commit -m "feat: add TalosApp shell with opportunities table and bottom panels"
```

---

## Task 3: Opportunities table refresh logic

Wire the table to read from `ArbitrageScanner.opportunities` and display formatted rows. The app creates a scanner in test-friendly mode (injectable).

**Files:**
- Modify: `src/talos/ui/app.py`
- Modify: `src/talos/ui/widgets.py`
- Modify: `tests/test_ui.py`

**Step 1: Write the tests**

Add to `tests/test_ui.py`:

```python
from talos.models.strategy import Opportunity
from talos.orderbook import OrderBookManager
from talos.scanner import ArbitrageScanner
from talos.models.ws import OrderBookSnapshot


def _make_scanner_with_opportunity() -> ArbitrageScanner:
    """Create a scanner with one detected opportunity."""
    mgr = OrderBookManager()
    scanner = ArbitrageScanner(mgr)
    scanner.add_pair("EVT-STANMIA", "GAME-STAN", "GAME-MIA")
    mgr.apply_snapshot(
        "GAME-STAN",
        OrderBookSnapshot(market_ticker="GAME-STAN", market_id="u1", yes=[[62, 100]], no=[]),
    )
    mgr.apply_snapshot(
        "GAME-MIA",
        OrderBookSnapshot(market_ticker="GAME-MIA", market_id="u2", yes=[[45, 200]], no=[]),
    )
    scanner.scan("GAME-STAN")
    return scanner


class TestOpportunitiesTable:
    async def test_table_shows_opportunity_row(self) -> None:
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            table = app.query_one(OpportunitiesTable)
            app.refresh_opportunities()
            await pilot.pause()
            assert table.row_count == 1

    async def test_table_formats_prices_as_cents(self) -> None:
        scanner = _make_scanner_with_opportunity()
        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            # Row 0 should have formatted price data
            row_data = table.get_row_at(0)
            # NO-A=38¢, NO-B=55¢, Cost=93¢, Edge=7¢, Qty=100, Profit=$7.00
            assert "38¢" in str(row_data[1])
            assert "55¢" in str(row_data[2])
            assert "7¢" in str(row_data[4])

    async def test_table_clears_vanished_opportunities(self) -> None:
        mgr = OrderBookManager()
        scanner = ArbitrageScanner(mgr)
        scanner.add_pair("EVT-1", "GAME-A", "GAME-B")
        mgr.apply_snapshot("GAME-A", OrderBookSnapshot(market_ticker="GAME-A", market_id="u1", yes=[[62, 100]], no=[]))
        mgr.apply_snapshot("GAME-B", OrderBookSnapshot(market_ticker="GAME-B", market_id="u2", yes=[[45, 100]], no=[]))
        scanner.scan("GAME-A")

        app = TalosApp(scanner=scanner)
        async with app.run_test() as pilot:
            app.refresh_opportunities()
            await pilot.pause()
            table = app.query_one(OpportunitiesTable)
            assert table.row_count == 1

            # Remove the opportunity
            mgr.apply_snapshot("GAME-A", OrderBookSnapshot(market_ticker="GAME-A", market_id="u1", yes=[[40, 100]], no=[]))
            scanner.scan("GAME-A")
            app.refresh_opportunities()
            await pilot.pause()
            assert table.row_count == 0
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py::TestOpportunitiesTable -v`
Expected: FAIL (TalosApp doesn't accept `scanner=` parameter)

**Step 3: Update the app to accept injectable dependencies**

Modify `src/talos/ui/app.py` — update `TalosApp`:

```python
"""Main Talos TUI application."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header

from talos.scanner import ArbitrageScanner
from talos.ui.theme import APP_CSS
from talos.ui.widgets import AccountPanel, OpportunitiesTable, OrderLog


class TalosApp(App):
    """Talos arbitrage trading dashboard."""

    CSS = APP_CSS
    TITLE = "TALOS"
    BINDINGS = [
        ("a", "add_games", "Add Games"),
        ("d", "remove_game", "Remove Game"),
        ("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        scanner: ArbitrageScanner | None = None,
    ) -> None:
        super().__init__()
        self._scanner = scanner

    def compose(self) -> ComposeResult:
        yield Header()
        yield OpportunitiesTable(id="opportunities-table")
        with Horizontal(id="bottom-panels"):
            yield AccountPanel(id="account-panel")
            yield OrderLog(id="order-log")
        yield Footer()

    def refresh_opportunities(self) -> None:
        """Update the opportunities table from scanner state."""
        table = self.query_one(OpportunitiesTable)
        table.refresh_from_scanner(self._scanner)

    def action_add_games(self) -> None:
        """Placeholder — will open Add Games modal."""

    def action_remove_game(self) -> None:
        """Placeholder — will remove selected game."""
```

**Step 4: Update OpportunitiesTable with refresh logic**

Modify `src/talos/ui/widgets.py` — update `OpportunitiesTable`:

```python
"""Dashboard widgets for Talos TUI."""

from __future__ import annotations

from textual.widgets import DataTable, Static

from talos.scanner import ArbitrageScanner


def _fmt_cents(value: int) -> str:
    """Format an integer cents value as 'XX¢'."""
    return f"{value}¢"


def _fmt_dollars(cents: int) -> str:
    """Format cents as dollar string."""
    return f"${cents / 100:.2f}"


class OpportunitiesTable(DataTable):
    """Live-updating arbitrage opportunities table."""

    DEFAULT_CSS = """
    OpportunitiesTable {
        height: 1fr;
    }
    """

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_columns(
            "Event", "NO-A", "NO-B", "Cost", "Edge", "Qty", "Profit", ""
        )

    def refresh_from_scanner(self, scanner: ArbitrageScanner | None) -> None:
        """Rebuild table rows from current scanner opportunities."""
        if scanner is None:
            return

        opps = scanner.opportunities
        current_keys = {row_key.value for row_key in self.rows}
        new_keys = {opp.event_ticker for opp in opps}

        # Remove vanished rows
        for key in current_keys - new_keys:
            self.remove_row(key)

        # Add or update rows
        for opp in opps:
            cost = opp.no_a + opp.no_b
            profit_cents = opp.raw_edge * opp.tradeable_qty
            row_data = (
                opp.event_ticker,
                _fmt_cents(opp.no_a),
                _fmt_cents(opp.no_b),
                _fmt_cents(cost),
                _fmt_cents(opp.raw_edge),
                str(opp.tradeable_qty),
                _fmt_dollars(profit_cents),
                "▸",
            )
            if opp.event_ticker in current_keys:
                # Update existing row cells
                for col_idx, value in enumerate(row_data):
                    col_key = self.ordered_columns[col_idx].key
                    self.update_cell(opp.event_ticker, col_key, value)
            else:
                self.add_row(*row_data, key=opp.event_ticker)


class AccountPanel(Static):
    """Displays balance and open positions."""

    def on_mount(self) -> None:
        self.update("ACCOUNT\n\nCash: —\nPortfolio: —")


class OrderLog(Static):
    """Scrollable log of recent orders."""

    def on_mount(self) -> None:
        self.update("ORDERS\n\nNo orders yet")
```

**Step 5: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py -v`
Expected: All PASS

**Step 6: Run full suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: All PASS

**Step 7: Commit**

```bash
git add src/talos/ui/app.py src/talos/ui/widgets.py tests/test_ui.py
git commit -m "feat: add opportunities table with scanner refresh logic"
```

---

## Task 4: Account panel and order log widgets

Wire the bottom panels to display balance, positions, and orders from formatted data.

**Files:**
- Modify: `src/talos/ui/widgets.py`
- Modify: `src/talos/ui/app.py`
- Modify: `tests/test_ui.py`

**Step 1: Write the tests**

Add to `tests/test_ui.py`:

```python
from talos.ui.widgets import AccountPanel, OrderLog


class TestAccountPanel:
    async def test_renders_balance(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            panel = app.query_one(AccountPanel)
            panel.update_balance(balance_cents=125000, portfolio_cents=210050)
            await pilot.pause()
            content = str(panel.renderable)
            assert "$1,250.00" in content
            assert "$2,100.50" in content

    async def test_renders_positions(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            panel = app.query_one(AccountPanel)
            panel.update_positions([
                {"ticker": "GAME-STAN", "qty": 100, "price": 38},
                {"ticker": "GAME-MIA", "qty": 100, "price": 55},
            ])
            await pilot.pause()
            content = str(panel.renderable)
            assert "GAME-STAN" in content
            assert "100" in content


class TestOrderLog:
    async def test_renders_orders(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            log = app.query_one(OrderLog)
            log.update_orders([
                {"ticker": "GAME-STAN", "side": "no", "price": 38, "count": 100, "status": "resting", "time": "12:33"},
                {"ticker": "GAME-MIA", "side": "no", "price": 55, "count": 100, "status": "executed", "time": "12:33"},
            ])
            await pilot.pause()
            content = str(log.renderable)
            assert "GAME-STAN" in content
            assert "GAME-MIA" in content

    async def test_empty_orders(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            log = app.query_one(OrderLog)
            log.update_orders([])
            await pilot.pause()
            content = str(log.renderable)
            assert "No orders" in content
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py::TestAccountPanel -v`
Expected: FAIL (`AccountPanel` has no `update_balance` method)

**Step 3: Update the panel widgets**

Modify `AccountPanel` in `src/talos/ui/widgets.py`:

```python
class AccountPanel(Static):
    """Displays balance and open positions."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._balance_text = "Cash: —\nPortfolio: —"
        self._positions_text = ""

    def on_mount(self) -> None:
        self._render_content()

    def update_balance(self, balance_cents: int, portfolio_cents: int) -> None:
        """Update the balance display."""
        self._balance_text = (
            f"Cash:      ${balance_cents / 100:,.2f}\n"
            f"Portfolio: ${portfolio_cents / 100:,.2f}"
        )
        self._render_content()

    def update_positions(self, positions: list[dict[str, object]]) -> None:
        """Update the positions display.

        Each dict has: ticker, qty, price (cents).
        """
        if not positions:
            self._positions_text = ""
            self._render_content()
            return
        lines = []
        for pos in positions:
            ticker = pos["ticker"]
            qty = pos["qty"]
            price = pos["price"]
            lines.append(f"  {ticker}  {qty} @ {price}¢")
        self._positions_text = "\nPOSITIONS\n" + "\n".join(lines)
        self._render_content()

    def _render_content(self) -> None:
        self.update(f"ACCOUNT\n\n{self._balance_text}{self._positions_text}")
```

Modify `OrderLog` in `src/talos/ui/widgets.py`:

```python
class OrderLog(Static):
    """Scrollable log of recent orders."""

    STATUS_ICONS = {
        "executed": "✓",
        "resting": "◷",
        "cancelled": "✗",
    }

    def on_mount(self) -> None:
        self.update("ORDERS\n\nNo orders yet")

    def update_orders(self, orders: list[dict[str, object]]) -> None:
        """Update the order log display.

        Each dict has: ticker, side, price, count, status, time.
        """
        if not orders:
            self.update("ORDERS\n\nNo orders yet")
            return
        lines = []
        for order in orders:
            icon = self.STATUS_ICONS.get(str(order["status"]), "?")
            side = str(order["side"]).upper()
            lines.append(
                f"  {order['time']}  BUY {side} {order['ticker']}  "
                f"{order['price']}¢ x{order['count']}  {icon}"
            )
        self.update("ORDERS\n\n" + "\n".join(lines))
```

**Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/talos/ui/widgets.py tests/test_ui.py
git commit -m "feat: add balance, positions, and order log panel rendering"
```

---

## Task 5: Add Games modal

Create the `ModalScreen` for pasting Kalshi URLs. Wires to `GameManager.add_games()`.

**Files:**
- Create: `src/talos/ui/screens.py`
- Modify: `src/talos/ui/app.py`
- Modify: `tests/test_ui.py`

**Step 1: Write the tests**

Add to `tests/test_ui.py`:

```python
class TestAddGamesModal:
    async def test_modal_opens_on_a_key(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            await pilot.press("a")
            await pilot.pause()
            from talos.ui.screens import AddGamesScreen
            assert len(app.query(AddGamesScreen)) == 1

    async def test_modal_closes_on_cancel(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            await pilot.press("a")
            await pilot.pause()
            await pilot.click("#cancel-btn")
            await pilot.pause()
            from talos.ui.screens import AddGamesScreen
            assert len(app.query(AddGamesScreen)) == 0
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py::TestAddGamesModal -v`
Expected: FAIL (no `AddGamesScreen`, `action_add_games` is a stub)

**Step 3: Create the screens module**

Create `src/talos/ui/screens.py`:

```python
"""Modal screens for Talos TUI."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, TextArea

from talos.ui.theme import BLUE


class AddGamesScreen(ModalScreen[list[str] | None]):
    """Modal for adding games by URL or ticker."""

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label("Add Games", classes="modal-title")
            yield Label("Paste Kalshi game URLs or event tickers, one per line:")
            yield TextArea(id="url-input")
            yield Label("", id="modal-error", classes="modal-error")
            with Vertical(id="modal-buttons"):
                yield Button("Cancel", id="cancel-btn", variant="default")
                yield Button("Add", id="add-btn", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.dismiss(None)
        elif event.button.id == "add-btn":
            text_area = self.query_one("#url-input", TextArea)
            raw = text_area.text.strip()
            if not raw:
                self.query_one("#modal-error", Label).update("Enter at least one URL or ticker")
                return
            urls = [line.strip() for line in raw.splitlines() if line.strip()]
            self.dismiss(urls)
```

**Step 4: Wire the modal in app.py**

Modify `action_add_games` in `src/talos/ui/app.py`:

```python
    def action_add_games(self) -> None:
        """Open the Add Games modal."""
        self.push_screen(AddGamesScreen())
```

Add the import at top of `app.py`:
```python
from talos.ui.screens import AddGamesScreen
```

**Step 5: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add src/talos/ui/screens.py src/talos/ui/app.py tests/test_ui.py
git commit -m "feat: add Add Games modal screen"
```

---

## Task 6: Bid confirmation modal

Create the modal for placing NO bids on both legs of an opportunity.

**Files:**
- Modify: `src/talos/ui/screens.py`
- Modify: `src/talos/ui/app.py`
- Modify: `tests/test_ui.py`

**Step 1: Write the tests**

Add to `tests/test_ui.py`:

```python
from talos.ui.screens import AddGamesScreen, BidScreen
from talos.models.strategy import Opportunity


class TestBidModal:
    async def test_bid_modal_shows_opportunity_data(self) -> None:
        opp = Opportunity(
            event_ticker="EVT-STANMIA",
            ticker_a="GAME-STAN",
            ticker_b="GAME-MIA",
            no_a=38,
            no_b=55,
            qty_a=100,
            qty_b=200,
            raw_edge=7,
            tradeable_qty=100,
            timestamp="2026-03-04T12:00:00Z",
        )
        app = TalosApp()
        async with app.run_test() as pilot:
            app.push_screen(BidScreen(opp))
            await pilot.pause()
            # Verify the modal is showing
            assert len(app.query(BidScreen)) == 1

    async def test_bid_modal_cancel(self) -> None:
        opp = Opportunity(
            event_ticker="EVT-STANMIA",
            ticker_a="GAME-STAN",
            ticker_b="GAME-MIA",
            no_a=38,
            no_b=55,
            qty_a=100,
            qty_b=200,
            raw_edge=7,
            tradeable_qty=100,
            timestamp="2026-03-04T12:00:00Z",
        )
        app = TalosApp()
        async with app.run_test() as pilot:
            app.push_screen(BidScreen(opp))
            await pilot.pause()
            await pilot.click("#cancel-btn")
            await pilot.pause()
            assert len(app.query(BidScreen)) == 0
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py::TestBidModal -v`
Expected: FAIL (no `BidScreen`)

**Step 3: Add BidScreen to screens.py**

Add to `src/talos/ui/screens.py`:

```python
from textual.widgets import Button, Input, Label, TextArea

from talos.models.strategy import Opportunity


class BidScreen(ModalScreen[dict[str, object] | None]):
    """Confirmation modal for placing NO bids on both legs."""

    def __init__(self, opportunity: Opportunity) -> None:
        super().__init__()
        self._opp = opportunity

    def compose(self) -> ComposeResult:
        opp = self._opp
        cost = opp.no_a + opp.no_b
        max_profit_cents = opp.raw_edge * opp.tradeable_qty

        with Vertical(id="modal-dialog"):
            yield Label("Place NO Bids", classes="modal-title")
            yield Label(f"{opp.event_ticker} — Edge: {opp.raw_edge}¢")
            yield Label(f"Leg A: BUY NO {opp.ticker_a} @ {opp.no_a}¢")
            yield Label(f"Leg B: BUY NO {opp.ticker_b} @ {opp.no_b}¢")
            yield Label(f"Qty (max {opp.tradeable_qty}):")
            yield Input(
                value=str(opp.tradeable_qty),
                id="qty-input",
                type="integer",
            )
            yield Label(
                f"Total: ${cost * opp.tradeable_qty / 100:.2f} → "
                f"Profit: ${max_profit_cents / 100:.2f}",
                id="cost-label",
            )
            yield Label("", id="modal-error", classes="modal-error")
            with Vertical(id="modal-buttons"):
                yield Button("Cancel", id="cancel-btn", variant="default")
                yield Button("Confirm", id="confirm-btn", variant="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.dismiss(None)
        elif event.button.id == "confirm-btn":
            qty_input = self.query_one("#qty-input", Input)
            try:
                qty = int(qty_input.value)
            except ValueError:
                self.query_one("#modal-error", Label).update("Invalid quantity")
                return
            if qty <= 0 or qty > self._opp.tradeable_qty:
                self.query_one("#modal-error", Label).update(
                    f"Quantity must be 1-{self._opp.tradeable_qty}"
                )
                return
            self.dismiss({
                "ticker_a": self._opp.ticker_a,
                "ticker_b": self._opp.ticker_b,
                "no_a": self._opp.no_a,
                "no_b": self._opp.no_b,
                "qty": qty,
            })
```

**Step 4: Wire bid action in app.py**

Add to `TalosApp` in `src/talos/ui/app.py`:

```python
    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open bid modal when a row is selected."""
        if self._scanner is None:
            return
        event_ticker = str(event.row_key.value)
        opp = next(
            (o for o in self._scanner.opportunities if o.event_ticker == event_ticker),
            None,
        )
        if opp and opp.raw_edge > 0:
            self.push_screen(BidScreen(opp))
```

Add import:
```python
from talos.ui.screens import AddGamesScreen, BidScreen
from textual.widgets import DataTable, Footer, Header
```

**Step 5: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add src/talos/ui/screens.py src/talos/ui/app.py tests/test_ui.py
git commit -m "feat: add bid confirmation modal with quantity validation"
```

---

## Task 7: Full app orchestration — startup wiring + timers

Wire the full startup sequence: config, REST, WS, books, feed, scanner, game manager. Add polling timers for table refresh and REST data. This makes the app functional end-to-end.

**Files:**
- Modify: `src/talos/ui/app.py`
- Modify: `src/talos/__main__.py`

**Step 1: Update app.py with full orchestration**

Rewrite `src/talos/ui/app.py` to support both test mode (injected scanner) and production mode (full wiring):

```python
"""Main Talos TUI application."""

from __future__ import annotations

import asyncio

import structlog
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header
from textual.work import work

from talos.auth import KalshiAuth
from talos.config import KalshiConfig
from talos.game_manager import GameManager
from talos.market_feed import MarketFeed
from talos.orderbook import OrderBookManager
from talos.rest_client import KalshiRESTClient
from talos.scanner import ArbitrageScanner
from talos.ui.screens import AddGamesScreen, BidScreen
from talos.ui.theme import APP_CSS
from talos.ui.widgets import AccountPanel, OpportunitiesTable, OrderLog
from talos.ws_client import KalshiWSClient

logger = structlog.get_logger()


class TalosApp(App):
    """Talos arbitrage trading dashboard."""

    CSS = APP_CSS
    TITLE = "TALOS"
    BINDINGS = [
        ("a", "add_games", "Add Games"),
        ("d", "remove_game", "Remove Game"),
        ("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        scanner: ArbitrageScanner | None = None,
        game_manager: GameManager | None = None,
        rest_client: KalshiRESTClient | None = None,
    ) -> None:
        super().__init__()
        self._scanner = scanner
        self._game_manager = game_manager
        self._rest = rest_client

    def compose(self) -> ComposeResult:
        yield Header()
        yield OpportunitiesTable(id="opportunities-table")
        with Horizontal(id="bottom-panels"):
            yield AccountPanel(id="account-panel")
            yield OrderLog(id="order-log")
        yield Footer()

    def on_mount(self) -> None:
        """Start polling timers."""
        self.set_interval(0.5, self.refresh_opportunities)
        if self._rest is not None:
            self.set_interval(10.0, self.refresh_account)

    def refresh_opportunities(self) -> None:
        """Update the opportunities table from scanner state."""
        table = self.query_one(OpportunitiesTable)
        table.refresh_from_scanner(self._scanner)

    @work(thread=False)
    async def refresh_account(self) -> None:
        """Fetch balance, positions, and orders from REST."""
        if self._rest is None:
            return
        try:
            balance = await self._rest.get_balance()
            panel = self.query_one(AccountPanel)
            panel.update_balance(balance.balance, balance.portfolio_value)

            positions = await self._rest.get_positions()
            pos_data = [
                {"ticker": p.ticker, "qty": p.position, "price": p.market_exposure}
                for p in positions
                if p.position != 0
            ]
            panel.update_positions(pos_data)

            orders = await self._rest.get_orders(limit=20)
            order_data = [
                {
                    "ticker": o.ticker,
                    "side": o.side,
                    "price": o.price,
                    "count": o.count,
                    "status": o.status,
                    "time": o.created_time[11:16] if len(o.created_time) > 16 else o.created_time,
                }
                for o in orders
            ]
            log = self.query_one(OrderLog)
            log.update_orders(order_data)
        except Exception:
            logger.exception("refresh_account_error")

    def action_add_games(self) -> None:
        """Open the Add Games modal."""
        self.push_screen(AddGamesScreen(), callback=self._on_games_added)

    def _on_games_added(self, urls: list[str] | None) -> None:
        """Handle result from Add Games modal."""
        if urls is None or self._game_manager is None:
            return
        self._add_games_async(urls)

    @work(thread=False)
    async def _add_games_async(self, urls: list[str]) -> None:
        """Add games in background."""
        try:
            await self._game_manager.add_games(urls)
            self.notify(f"Added {len(urls)} game(s)", severity="information")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")
            logger.exception("add_games_error")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open bid modal when a row is selected."""
        if self._scanner is None:
            return
        event_ticker = str(event.row_key.value)
        opp = next(
            (o for o in self._scanner.opportunities if o.event_ticker == event_ticker),
            None,
        )
        if opp and opp.raw_edge > 0:
            self.push_screen(BidScreen(opp), callback=self._on_bid_confirmed)

    def _on_bid_confirmed(self, result: dict[str, object] | None) -> None:
        """Handle result from Bid modal."""
        if result is None or self._rest is None:
            return
        self._place_bids(result)

    @work(thread=False)
    async def _place_bids(self, bid: dict[str, object]) -> None:
        """Place NO orders on both legs."""
        try:
            qty = int(str(bid["qty"]))
            # Leg A
            await self._rest.create_order(
                ticker=str(bid["ticker_a"]),
                side="no",
                order_type="limit",
                price=int(str(bid["no_a"])),
                count=qty,
            )
            # Leg B
            await self._rest.create_order(
                ticker=str(bid["ticker_b"]),
                side="no",
                order_type="limit",
                price=int(str(bid["no_b"])),
                count=qty,
            )
            self.notify("Orders placed", severity="information")
        except Exception as e:
            self.notify(f"Order error: {e}", severity="error")
            logger.exception("place_bids_error")

    def action_remove_game(self) -> None:
        """Remove the currently selected game."""
        if self._game_manager is None or self._scanner is None:
            return
        table = self.query_one(OpportunitiesTable)
        if table.cursor_row is not None:
            try:
                row_key = table.get_row_at(table.cursor_row)
                event_ticker = str(row_key[0])  # first column is event_ticker
                self._remove_game_async(event_ticker)
            except Exception:
                pass

    @work(thread=False)
    async def _remove_game_async(self, event_ticker: str) -> None:
        """Remove a game in background."""
        if self._game_manager is None:
            return
        try:
            await self._game_manager.remove_game(event_ticker)
            self.notify(f"Removed {event_ticker}", severity="information")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")
```

**Step 2: Update __main__.py for production startup**

```python
"""Entry point: python -m talos."""

from __future__ import annotations

import structlog

from talos.auth import KalshiAuth
from talos.config import KalshiConfig
from talos.game_manager import GameManager
from talos.market_feed import MarketFeed
from talos.orderbook import OrderBookManager
from talos.rest_client import KalshiRESTClient
from talos.scanner import ArbitrageScanner
from talos.ui.app import TalosApp
from talos.ws_client import KalshiWSClient


def main() -> None:
    """Launch the Talos dashboard."""
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(structlog.get_config()["wrapper_class"]),
    )

    config = KalshiConfig.from_env()
    auth = KalshiAuth(config)
    rest = KalshiRESTClient(auth, config)
    ws = KalshiWSClient(auth, config)
    books = OrderBookManager()
    feed = MarketFeed(ws, books)
    scanner = ArbitrageScanner(books)
    game_mgr = GameManager(rest, feed, scanner)

    # Wire scanner to book updates
    feed.on_book_update = scanner.scan

    app = TalosApp(
        scanner=scanner,
        game_manager=game_mgr,
        rest_client=rest,
    )
    app.run()


if __name__ == "__main__":
    main()
```

**Step 3: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py -v`
Expected: All PASS (tests use injected scanner, no real connections)

**Step 4: Run full suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/talos/ui/app.py src/talos/__main__.py
git commit -m "feat: add full app orchestration with startup wiring and polling timers"
```

---

## Task 8: Lint + type check

**Step 1: Run ruff lint**

Run: `.venv/Scripts/python -m ruff check src/ tests/`

Fix any errors found.

**Step 2: Run ruff format**

Run: `.venv/Scripts/python -m ruff format src/ tests/`

**Step 3: Run pyright**

Run: `.venv/Scripts/python -m pyright`
Expected: 0 errors

**Step 4: Run full test suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: All PASS

**Step 5: Commit (if any changes)**

```bash
git add -u
git commit -m "style: lint and format TUI dashboard code"
```

---

## Task 9: Brain update

Update brain files to reflect Layer 5 completion.

**Files:**
- Modify: `brain/architecture.md`
- Modify: `brain/codebase/index.md`

**Step 1: Update architecture.md**

Change the Layer 5 line to:

```
5. **UI (Textual TUI)** (Layer 5) — **COMPLETE**
   - `ui/theme.py` — Catppuccin Mocha color palette and TCSS
   - `ui/widgets.py` — OpportunitiesTable (DataTable), AccountPanel, OrderLog
   - `ui/screens.py` — AddGamesScreen, BidScreen (ModalScreens)
   - `ui/app.py` — TalosApp orchestrator (startup, timers, event handling)
   - `__main__.py` — entry point: `python -m talos`
```

**Step 2: Update codebase/index.md**

Add to module map:
| `ui/theme.py` | Catppuccin Mocha colors + TCSS | Color constants, `APP_CSS` |
| `ui/widgets.py` | Dashboard widgets | `OpportunitiesTable`, `AccountPanel`, `OrderLog` |
| `ui/screens.py` | Modal dialogs | `AddGamesScreen`, `BidScreen` |
| `ui/app.py` | Main app orchestration | `TalosApp` |
| `__main__.py` | Entry point | `python -m talos` |

Add gotcha:
- **Textual table refresh:** Don't refresh the DataTable on every WS delta (10-50/sec). Poll `scanner.opportunities` every 500ms instead. Use `set_interval(0.5, callback)`.
- **Textual test mode:** Use `TalosApp(scanner=scanner)` with injected dependencies. `async with app.run_test() as pilot:` for headless UI testing.

**Step 3: Commit**

```bash
git add brain/
git commit -m "docs: update brain with Layer 5 (TUI Dashboard) completion"
```

---

## Summary

| Task | Creates/Modifies | Tests |
|------|-----------------|-------|
| 1. Theme + scaffold | `ui/__init__.py`, `ui/theme.py`, `__main__.py` | — |
| 2. App shell + table | `ui/app.py`, `ui/widgets.py`, `test_ui.py` | 3 tests (mount, header/footer, panels) |
| 3. Table refresh | `ui/widgets.py`, `ui/app.py`, `test_ui.py` | 3 tests (row content, formatting, removal) |
| 4. Account + orders | `ui/widgets.py`, `test_ui.py` | 4 tests (balance, positions, orders, empty) |
| 5. Add Games modal | `ui/screens.py`, `ui/app.py`, `test_ui.py` | 2 tests (open, cancel) |
| 6. Bid modal | `ui/screens.py`, `ui/app.py`, `test_ui.py` | 2 tests (shows data, cancel) |
| 7. Full orchestration | `ui/app.py`, `__main__.py` | — (integration via existing tests) |
| 8. Lint + types | All new files | — |
| 9. Brain update | `brain/` | — |
