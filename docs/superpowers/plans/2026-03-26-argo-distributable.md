# Argo Distributable Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a single-file Windows executable (`Argo.exe`) that wraps the Talos trading engine for MLB player prop bets (KXMLBHRR, KXMLBTB series) with a first-run setup wizard and custom portfolio panel.

**Architecture:** Thin wrapper package `src/argo/` imports from `src/talos/` and overrides scan flow, portfolio panel, and branding. One small backward-compatible Talos change: configurable data directory in `persistence.py`. PyInstaller `--onefile` produces the exe.

**Tech Stack:** Python 3.12+, Textual (TUI), PyInstaller, Pydantic v2, httpx, websockets, cryptography

**Spec:** `docs/superpowers/specs/2026-03-26-argo-distributable-design.md`

---

## File Map

### Talos Changes (minimal, backward-compatible)

| File | Change | Purpose |
|------|--------|---------|
| `src/talos/persistence.py` | Add `set_data_dir()` / `get_data_dir()`, update module-level paths | Configurable data directory for PyInstaller |
| `src/talos/__main__.py` | Switch hardcoded `Path(__file__)` to `get_data_dir()` | Same |
| `src/talos/ui/app.py:737,1017` | Switch `Path(__file__).parents[3]` to `get_data_dir()` | Same |
| `src/talos/rest_client.py` | Add `get_markets(event_ticker=...)` public method | Argo scan needs to list markets per event |
| `src/talos/engine.py` | Add `rest_client` public property | ArgoApp.action_scan needs REST access |
| `src/talos/game_manager.py` | Add optional `fee_type_override`/`fee_rate_override` params | Argo hardcodes fee_free at pair construction |
| `pyproject.toml` | Add `src/argo` to packages, add `argo` script entry | Build config |

### New Argo Package

| File | Purpose |
|------|---------|
| `src/argo/__init__.py` | Package marker, version |
| `src/argo/config.py` | Series list, fee config, app name, `resolve_data_dir()` |
| `src/argo/__main__.py` | Entry point: first-run check → engine wiring → launch |
| `src/argo/first_run.py` | Textual SetupScreen for API credentials |
| `src/argo/widgets.py` | `ArgoPortfolioPanel` (completed/partial/open pairs) |
| `src/argo/screens.py` | `ArgoScanScreen` (prop event discovery) |
| `src/argo/app.py` | `ArgoApp(TalosApp)` subclass — title, scan override, portfolio swap |

### Build Artifacts

| File | Purpose |
|------|---------|
| `argo.spec` | PyInstaller spec for onefile build |
| `icons/argo/argo.ico` | Converted from SVG for exe icon |

### Tests

| File | Purpose |
|------|---------|
| `tests/test_persistence_data_dir.py` | `set_data_dir` / `get_data_dir` backward compatibility |
| `tests/test_argo_config.py` | Config constants, `resolve_data_dir()` |
| `tests/test_argo_portfolio_panel.py` | Pair counting math with worked examples |
| `tests/test_argo_first_run.py` | SetupScreen writes correct `.env` |
| `tests/test_argo_scan.py` | Scan screen event discovery + selection flow |

---

## Task 1: Configurable Data Directory in persistence.py

**Files:**
- Modify: `src/talos/persistence.py:1-14`
- Test: `tests/test_persistence_data_dir.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_persistence_data_dir.py
"""Test configurable data directory for persistence module."""
from pathlib import Path
from talos.persistence import get_data_dir, set_data_dir


def test_get_data_dir_returns_default_when_unset():
    """Default should be project root (parents[2] from persistence.py)."""
    result = get_data_dir()
    # Should be a real directory, not None
    assert isinstance(result, Path)


def test_set_data_dir_overrides_default(tmp_path: Path):
    """After set_data_dir, get_data_dir returns the override."""
    set_data_dir(tmp_path)
    assert get_data_dir() == tmp_path
    # Reset to not pollute other tests
    set_data_dir(None)  # type: ignore[arg-type]


def test_set_data_dir_none_restores_default():
    """Passing None restores default behavior."""
    original = get_data_dir()
    set_data_dir(Path("/fake"))
    set_data_dir(None)  # type: ignore[arg-type]
    assert get_data_dir() == original
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_persistence_data_dir.py -v`
Expected: ImportError — `get_data_dir` and `set_data_dir` don't exist yet

- [ ] **Step 3: Implement `set_data_dir` / `get_data_dir` in persistence.py**

Add at the top of `src/talos/persistence.py`, after imports (before line 12):

```python
_data_dir: Path | None = None


def set_data_dir(path: Path | None) -> None:
    """Override the base directory for all persistence files.

    Call with None to restore the default (project root).
    Used by Argo to point persistence at the exe's directory.
    """
    global _data_dir
    _data_dir = path


def get_data_dir() -> Path:
    """Return the base directory for persistence files."""
    return _data_dir or Path(__file__).resolve().parents[2]
```

Then update the three module-level path constants to use `get_data_dir()`:

Replace lines 12-13 and 39:
```python
# OLD:
_GAMES_FILE = Path(__file__).resolve().parents[2] / "games.json"
_SETTINGS_FILE = Path(__file__).resolve().parents[2] / "settings.json"
# ...
_GAMES_FULL_FILE = Path(__file__).resolve().parents[2] / "games_full.json"

# NEW — compute lazily via get_data_dir():
# Remove the module-level constants entirely.
# In each function, use get_data_dir() / "filename" as the default.
```

Update `load_saved_games`, `save_games`, `save_games_full`, `load_saved_games_full`, `load_settings`, `save_settings` — change each default from `_GAMES_FILE` / `_SETTINGS_FILE` / `_GAMES_FULL_FILE` to `get_data_dir() / "games.json"` etc.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_persistence_data_dir.py -v`
Expected: 3 PASSED

- [ ] **Step 5: Run full Talos test suite to verify backward compatibility**

Run: `.venv/Scripts/python -m pytest -x`
Expected: All existing tests pass (the default path is unchanged)

- [ ] **Step 6: Commit**

```bash
git add src/talos/persistence.py tests/test_persistence_data_dir.py
git commit -m "feat: configurable data directory in persistence.py for Argo"
```

---

## Task 2: Update Remaining Hardcoded Paths

**Files:**
- Modify: `src/talos/__main__.py:12,98,181`
- Modify: `src/talos/ui/app.py:737,1017`

- [ ] **Step 1: Update `__main__.py` to use `get_data_dir()`**

Replace `Path(__file__).resolve().parents[2]` at three locations:

```python
# Line 12 (in _load_dotenv):
# OLD: env_file = Path(__file__).resolve().parents[2] / ".env"
# NEW:
from talos.persistence import get_data_dir
env_file = get_data_dir() / ".env"

# Line 98 (db_dir):
# OLD: db_dir = Path(__file__).resolve().parents[2]
# NEW:
db_dir = get_data_dir()

# Line 181 (suggestions.log):
# OLD: log_path = Path(__file__).resolve().parents[2] / "suggestions.log"
# NEW:
log_path = get_data_dir() / "suggestions.log"
```

- [ ] **Step 2: Update `app.py` to use `get_data_dir()`**

```python
# Line 737 (action_review_event):
# OLD: base = Path(__file__).resolve().parents[3]
# NEW:
from talos.persistence import get_data_dir
base = get_data_dir()

# Line 1017 (auto-accept sessions):
# OLD: log_dir = Path(__file__).resolve().parents[3] / "auto_accept_sessions"
# NEW:
from talos.persistence import get_data_dir
log_dir = get_data_dir() / "auto_accept_sessions"
```

- [ ] **Step 3: Add `rest_client` property to TradingEngine**

Add at `src/talos/engine.py` after the existing `adjuster` property (around line 170):

```python
@property
def rest_client(self) -> KalshiRESTClient:
    return self._rest
```

- [ ] **Step 4: Add fee override params to GameManager**

At `src/talos/game_manager.py`, add `fee_type_override` and `fee_rate_override` to `__init__`:

```python
def __init__(
    self,
    rest: KalshiRESTClient,
    feed: MarketFeed,
    scanner: ArbitrageScanner,
    *,
    sports_enabled: bool = False,
    nonsports_categories: list[str] | None = None,
    nonsports_max_days: int = 7,
    ticker_blacklist: list[str] | None = None,
    fee_type_override: str | None = None,    # NEW
    fee_rate_override: float | None = None,  # NEW
) -> None:
```

Store as `self._fee_type_override` and `self._fee_rate_override`. In the `add_game` method at line 305-306, use the override if set:

```python
fee_type = self._fee_type_override or "quadratic_with_maker_fees"
fee_rate = self._fee_rate_override if self._fee_rate_override is not None else 0.0175
if self._fee_type_override is None:
    # Only fetch series fee info when not overridden
    try:
        series = await self._rest.get_series(event.series_ticker)
        fee_type = series.fee_type
        fee_rate = series.fee_multiplier
    except Exception:
        pass
```

- [ ] **Step 5: Run full test suite**

Run: `.venv/Scripts/python -m pytest -x`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add src/talos/__main__.py src/talos/ui/app.py src/talos/engine.py src/talos/game_manager.py
git commit -m "refactor: configurable data dir, rest_client property, fee override in GameManager"
```

---

## Task 3: Add `get_markets()` to REST Client

**Files:**
- Modify: `src/talos/rest_client.py` (after `get_market` at line 88)
- Test: `tests/test_rest_client.py` (add test)

- [ ] **Step 1: Write the failing test**

Add to the existing `tests/test_rest_client.py`:

```python
async def test_get_markets_by_event(rest_client, mock_http):
    """get_markets returns list of Market objects for an event."""
    mock_http.request.return_value = httpx.Response(
        200,
        json={
            "cursor": "",
            "markets": [
                _minimal_market("KXMLBHRR-T1-PLAYER1-3"),
                _minimal_market("KXMLBHRR-T1-PLAYER2-2"),
            ],
        },
    )
    result = await rest_client.get_markets(event_ticker="KXMLBHRR-T1")
    assert len(result) == 2
    assert result[0].ticker == "KXMLBHRR-T1-PLAYER1-3"
```

(Use whatever `_minimal_market` helper already exists in the test file, or create a minimal dict matching the `Market` model.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_rest_client.py::test_get_markets_by_event -v`
Expected: AttributeError — `get_markets` doesn't exist

- [ ] **Step 3: Implement `get_markets`**

Add after `get_market()` at `src/talos/rest_client.py:91`:

```python
async def get_markets(
    self,
    *,
    event_ticker: str | None = None,
    series_ticker: str | None = None,
    status: str | None = None,
    limit: int = 200,
    cursor: str | None = None,
) -> list[Market]:
    """Fetch markets, optionally filtered by event or series ticker."""
    params: dict[str, Any] = {"limit": limit}
    if event_ticker:
        params["event_ticker"] = event_ticker
    if series_ticker:
        params["series_ticker"] = series_ticker
    if status:
        params["status"] = status
    if cursor:
        params["cursor"] = cursor
    data = await self._request("GET", "/markets", params=params)
    return [Market.model_validate(m) for m in data.get("markets", [])]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_rest_client.py::test_get_markets_by_event -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/talos/rest_client.py tests/test_rest_client.py
git commit -m "feat: add get_markets() public method to REST client"
```

---

## Task 4: pyproject.toml + Argo Package Scaffold

**Files:**
- Modify: `pyproject.toml:25-27,33-34`
- Create: `src/argo/__init__.py`
- Create: `src/argo/config.py`

- [ ] **Step 1: Update pyproject.toml**

Add `argo` script entry and package:

```toml
# [project.scripts] — add:
argo = "argo.__main__:main"

# [tool.hatch.build.targets.wheel] — update:
packages = ["src/talos", "src/drip", "src/argo"]
```

- [ ] **Step 2: Create `src/argo/__init__.py`**

```python
"""Argo — MLB Props trading terminal, powered by Talos."""

__version__ = "0.1.0"
```

- [ ] **Step 3: Create `src/argo/config.py`**

```python
"""Argo configuration — series, fees, app identity, data directory."""

from __future__ import annotations

import sys
from pathlib import Path

APP_NAME = "Argo"
APP_SUBTITLE = "MLB Props"

# MLB player prop series — the only markets Argo scans
SERIES = ["KXMLBHRR", "KXMLBTB"]

SERIES_LABELS = {
    "KXMLBHRR": "Hits + Runs + RBIs",
    "KXMLBTB": "Total Bases",
}

DEFAULT_UNIT_SIZE = 10

# MLB props are currently fee-free on Kalshi.
# Hardcode so Argo ignores any future API fee_type changes.
FEE_TYPE = "fee_free"
FEE_RATE = 0.0


def resolve_data_dir() -> Path:
    """Return directory for runtime data files.

    PyInstaller --onefile extracts to a temp dir, but sys.executable
    points to the actual exe location. In dev mode, use cwd.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path.cwd()
```

- [ ] **Step 4: Write config test**

Create `tests/test_argo_config.py`:

```python
"""Test Argo configuration constants and resolve_data_dir."""
from pathlib import Path

from argo.config import (
    DEFAULT_UNIT_SIZE,
    FEE_RATE,
    FEE_TYPE,
    SERIES,
    SERIES_LABELS,
    resolve_data_dir,
)


def test_series_tickers():
    assert SERIES == ["KXMLBHRR", "KXMLBTB"]


def test_series_labels_match_series():
    assert set(SERIES_LABELS.keys()) == set(SERIES)


def test_fee_free():
    assert FEE_TYPE == "fee_free"
    assert FEE_RATE == 0.0


def test_default_unit_size():
    assert DEFAULT_UNIT_SIZE == 10


def test_resolve_data_dir_returns_cwd_in_dev():
    """In non-frozen mode, returns current working directory."""
    result = resolve_data_dir()
    assert result == Path.cwd()
```

- [ ] **Step 5: Run test**

Run: `.venv/Scripts/python -m pytest tests/test_argo_config.py -v`
Expected: All PASS

- [ ] **Step 6: Reinstall editable package (new package added)**

Run: `.venv/Scripts/pip install -e .`

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/argo/__init__.py src/argo/config.py tests/test_argo_config.py
git commit -m "feat: scaffold Argo package with config"
```

---

## Task 5: ArgoPortfolioPanel Widget

**Files:**
- Create: `src/argo/widgets.py`
- Test: `tests/test_argo_portfolio_panel.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_argo_portfolio_panel.py
"""Test ArgoPortfolioPanel pair counting math."""
from argo.widgets import compute_pair_stats, PairStats


def test_fully_matched_pair():
    """10 YES + 10 NO at unit_size=10 → 1 completed, 0 partial."""
    stats = compute_pair_stats(yes_filled=10, no_filled=10, has_resting=False, unit_size=10)
    assert stats.completed == 1
    assert stats.partial == 0
    assert stats.open == 0


def test_over_filled_one_side():
    """20 YES + 10 NO at unit_size=10 → 1 completed, 1 partial."""
    stats = compute_pair_stats(yes_filled=20, no_filled=10, has_resting=False, unit_size=10)
    assert stats.completed == 1
    assert stats.partial == 1
    assert stats.open == 0


def test_partial_fill_no_full_batch():
    """5 YES + 0 NO at unit_size=10 → 0 completed, 1 partial."""
    stats = compute_pair_stats(yes_filled=5, no_filled=0, has_resting=False, unit_size=10)
    assert stats.completed == 0
    assert stats.partial == 1
    assert stats.open == 0


def test_resting_only_no_fills():
    """0 YES + 0 NO with resting bid → 0 completed, 0 partial, 1 open."""
    stats = compute_pair_stats(yes_filled=0, no_filled=0, has_resting=True, unit_size=10)
    assert stats.completed == 0
    assert stats.partial == 0
    assert stats.open == 1


def test_no_activity():
    """0 YES + 0 NO, no resting → all zeros."""
    stats = compute_pair_stats(yes_filled=0, no_filled=0, has_resting=False, unit_size=10)
    assert stats.completed == 0
    assert stats.partial == 0
    assert stats.open == 0


def test_multiple_completed_batches():
    """30 YES + 30 NO at unit_size=10 → 3 completed."""
    stats = compute_pair_stats(yes_filled=30, no_filled=30, has_resting=False, unit_size=10)
    assert stats.completed == 3
    assert stats.partial == 0
    assert stats.open == 0


def test_spec_worked_example():
    """Verify the full worked example from the spec.

    HARPER-3: 20 YES, 10 NO → completed=1, partial=1
    SEAGER-2: 10 YES, 10 NO → completed=1, partial=0
    BOHM-1:    5 YES,  0 NO → completed=0, partial=1
    TURNER-4:  0 YES,  0 NO, resting → open=1
    Totals: completed=2, partial=2, open=1
    """
    tickers = [
        compute_pair_stats(20, 10, False, 10),
        compute_pair_stats(10, 10, False, 10),
        compute_pair_stats(5, 0, False, 10),
        compute_pair_stats(0, 0, True, 10),
    ]
    assert sum(t.completed for t in tickers) == 2
    assert sum(t.partial for t in tickers) == 2
    assert sum(t.open for t in tickers) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_argo_portfolio_panel.py -v`
Expected: ImportError — `argo.widgets` doesn't exist

- [ ] **Step 3: Implement the widget**

Create `src/argo/widgets.py`:

```python
"""Argo-specific widgets — portfolio panel with pair status summary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from textual.widgets import Static


@dataclass(frozen=True)
class PairStats:
    """Per-ticker pair counting result."""

    completed: int
    partial: int
    open: int


def compute_pair_stats(
    yes_filled: int,
    no_filled: int,
    has_resting: bool,
    unit_size: int,
) -> PairStats:
    """Compute completed/partial/open pair counts for one ticker.

    - completed: min(yes, no) // unit_size  (fully matched batches)
    - partial: any remaining position on either side after completed removal
    - open: resting bids but zero fills on both sides
    """
    completed = min(yes_filled, no_filled) // unit_size

    remaining_yes = yes_filled - completed * unit_size
    remaining_no = no_filled - completed * unit_size

    has_fills = yes_filled > 0 or no_filled > 0

    if has_fills and (remaining_yes > 0 or remaining_no > 0):
        partial = 1
    else:
        partial = 0

    if not has_fills and has_resting:
        open_count = 1
    else:
        open_count = 0

    return PairStats(completed=completed, partial=partial, open=open_count)


class ArgoPortfolioPanel(Static):
    """Pair status summary: completed/partial/open pairs + ticker counts."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self._completed: int = 0
        self._partial: int = 0
        self._open: int = 0
        self._total_tickers: int = 0
        self._active_tickers: int = 0

    def on_mount(self) -> None:
        self.border_title = "Portfolio"

    def render(self) -> str:
        return (
            f"Completed: {self._completed}\n"
            f"Partial:   {self._partial}\n"
            f"Open:      {self._open}\n"
            f"───────────────────\n"
            f"Tickers:   {self._total_tickers}\n"
            f"Active:    {self._active_tickers}"
        )

    def update_stats(
        self,
        *,
        completed: int,
        partial: int,
        open_pairs: int,
        total_tickers: int,
        active_tickers: int,
    ) -> None:
        self._completed = completed
        self._partial = partial
        self._open = open_pairs
        self._total_tickers = total_tickers
        self._active_tickers = active_tickers
        self.refresh()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_argo_portfolio_panel.py -v`
Expected: 7 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/argo/widgets.py tests/test_argo_portfolio_panel.py
git commit -m "feat: ArgoPortfolioPanel with pair counting math"
```

---

## Task 6: First-Run Setup Screen

**Files:**
- Create: `src/argo/first_run.py`
- Test: `tests/test_argo_first_run.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_argo_first_run.py
"""Test first-run setup: .env generation and validation."""
from pathlib import Path

from argo.first_run import write_env_file


def test_write_env_file_creates_valid_env(tmp_path: Path):
    """write_env_file creates a .env with the given credentials."""
    env_path = tmp_path / ".env"
    write_env_file(
        path=env_path,
        key_id="test-key-123",
        private_key_path="/path/to/key.pem",
        env="production",
    )
    assert env_path.exists()
    content = env_path.read_text()
    assert "KALSHI_KEY_ID=test-key-123" in content
    assert "KALSHI_PRIVATE_KEY_PATH=/path/to/key.pem" in content
    assert "KALSHI_ENV=production" in content


def test_write_env_file_demo_env(tmp_path: Path):
    """Demo environment is written correctly."""
    env_path = tmp_path / ".env"
    write_env_file(
        path=env_path,
        key_id="demo-key",
        private_key_path="C:\\keys\\demo.pem",
        env="demo",
    )
    content = env_path.read_text()
    assert "KALSHI_ENV=demo" in content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_argo_first_run.py -v`
Expected: ImportError

- [ ] **Step 3: Implement first_run.py**

Create `src/argo/first_run.py`:

```python
"""First-run setup — collects Kalshi credentials and writes .env."""

from __future__ import annotations

from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, RadioButton, RadioSet, Static


def write_env_file(
    *,
    path: Path,
    key_id: str,
    private_key_path: str,
    env: str = "production",
) -> None:
    """Write a .env file with Kalshi credentials."""
    content = (
        f"KALSHI_KEY_ID={key_id}\n"
        f"KALSHI_PRIVATE_KEY_PATH={private_key_path}\n"
        f"KALSHI_ENV={env}\n"
    )
    path.write_text(content)


class SetupScreen(Screen):
    """First-run credential setup screen."""

    BINDINGS = [("escape", "quit", "Cancel")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="setup-form"):
            yield Static("Welcome to Argo — MLB Props Trading Terminal\n", id="setup-title")
            yield Label("Kalshi API Key ID:")
            yield Input(placeholder="e.g. abc123-def456-...", id="key-id")
            yield Label("Path to RSA Private Key file:")
            yield Input(placeholder="e.g. C:\\Users\\you\\kalshi.key", id="key-path")
            yield Label("Environment:")
            with RadioSet(id="env-select"):
                yield RadioButton("Production", value=True, id="env-prod")
                yield RadioButton("Demo", id="env-demo")
            yield Static("", id="validation-msg")
            yield Button("Save & Launch", variant="primary", id="save-btn")
        yield Footer()

    @on(Button.Pressed, "#save-btn")
    def on_save(self) -> None:
        key_id = self.query_one("#key-id", Input).value.strip()
        key_path = self.query_one("#key-path", Input).value.strip()
        env_select = self.query_one("#env-select", RadioSet)
        env = "demo" if env_select.pressed_index == 1 else "production"

        # Validate
        msg_widget = self.query_one("#validation-msg", Static)
        if not key_id:
            msg_widget.update("[red]API Key ID is required[/red]")
            return
        if not key_path:
            msg_widget.update("[red]Private key path is required[/red]")
            return
        if not Path(key_path).is_file():
            msg_widget.update(f"[red]Key file not found: {key_path}[/red]")
            return

        self.dismiss((key_id, key_path, env))


class SetupApp(App):
    """Minimal app for first-run setup only."""

    CSS = """
    #setup-form { padding: 2 4; }
    #setup-title { text-style: bold; }
    #validation-msg { margin-top: 1; }
    """

    def __init__(self, data_dir: Path) -> None:
        super().__init__()
        self._data_dir = data_dir
        self.setup_complete = False

    def on_mount(self) -> None:
        self.push_screen(SetupScreen(), callback=self._on_setup_done)

    def _on_setup_done(self, result: tuple[str, str, str] | None) -> None:
        if result is None:
            self.exit()
            return
        key_id, key_path, env = result
        write_env_file(
            path=self._data_dir / ".env",
            key_id=key_id,
            private_key_path=key_path,
            env=env,
        )
        self.setup_complete = True
        self.exit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_argo_first_run.py -v`
Expected: 2 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/argo/first_run.py tests/test_argo_first_run.py
git commit -m "feat: first-run setup screen for Argo"
```

---

## Task 7: ArgoScanScreen

**Files:**
- Create: `src/argo/screens.py`
- Test: `tests/test_argo_scan.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_argo_scan.py
"""Test ArgoScanScreen event display."""
from argo.screens import format_scan_row
from talos.models.market import Event


def test_format_scan_row_extracts_fields():
    """format_scan_row returns (game, type, market_count) tuple."""
    event = Event(
        event_ticker="KXMLBHRR-26MAR261615TEXPHI",
        series_ticker="KXMLBHRR",
        title="Texas vs Philadelphia: Hits + Runs + RBIs",
        sub_title="TEX vs PHI (Mar 26)",
        category="Sports",
        mutually_exclusive=False,
    )
    row = format_scan_row(event, market_count=85)
    assert row.game == "TEX vs PHI (Mar 26)"
    assert row.prop_type == "Hits + Runs + RBIs"
    assert row.market_count == 85
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_argo_scan.py -v`
Expected: ImportError

- [ ] **Step 3: Implement ArgoScanScreen**

Create `src/argo/screens.py`:

```python
"""Argo-specific screens — prop event scanner."""

from __future__ import annotations

from dataclasses import dataclass

from textual import on
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Static

from argo.config import SERIES, SERIES_LABELS
from talos.models.market import Event


@dataclass
class ScanRow:
    """One row in the scan results table."""

    game: str
    prop_type: str
    market_count: int
    event: Event


def format_scan_row(event: Event, market_count: int) -> ScanRow:
    """Build a scan row from an Event + its market count."""
    return ScanRow(
        game=event.sub_title or event.title,
        prop_type=SERIES_LABELS.get(event.series_ticker, event.series_ticker),
        market_count=market_count,
        event=event,
    )


class ArgoScanScreen(ModalScreen[list[Event]]):
    """Scan results for MLB prop events."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("enter", "select", "Add Selected"),
        ("space", "toggle_row", "Toggle"),
    ]

    def __init__(self, rows: list[ScanRow]) -> None:
        super().__init__()
        self._rows = rows
        self._selected: set[int] = set()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"Found {len(self._rows)} prop events — select to add", id="scan-header")
        table = DataTable(id="scan-table")
        table.cursor_type = "row"
        table.add_columns("", "Game", "Type", "Markets")
        for i, row in enumerate(self._rows):
            table.add_row("[ ]", row.game, row.prop_type, str(row.market_count), key=str(i))
        yield table
        yield Footer()

    def action_toggle_row(self) -> None:
        table = self.query_one(DataTable)
        if table.cursor_row is None:
            return
        idx = table.cursor_row
        if idx in self._selected:
            self._selected.discard(idx)
            table.update_cell_at((idx, 0), "[ ]")
        else:
            self._selected.add(idx)
            table.update_cell_at((idx, 0), "[X]")

    def action_select(self) -> None:
        selected_events = [self._rows[i].event for i in sorted(self._selected)]
        self.dismiss(selected_events)

    def action_cancel(self) -> None:
        self.dismiss([])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_argo_scan.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/argo/screens.py tests/test_argo_scan.py
git commit -m "feat: ArgoScanScreen for MLB prop event discovery"
```

---

## Task 8: ArgoApp Subclass

**Files:**
- Create: `src/argo/app.py`

This is the central wiring — subclasses TalosApp, overrides scan, swaps portfolio panel.

- [ ] **Step 1: Create `src/argo/app.py`**

```python
"""ArgoApp — TalosApp subclass for MLB prop trading."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Static

from argo.config import FEE_RATE, FEE_TYPE, SERIES, SERIES_LABELS
from argo.screens import ArgoScanScreen, format_scan_row
from argo.widgets import ArgoPortfolioPanel, compute_pair_stats
from talos.position_ledger import Side
from talos.ui.app import TalosApp
from talos.ui.proposal_panel import ProposalPanel
from talos.ui.widgets import ActivityLog, OpportunitiesTable, OrderLog, PortfolioPanel

if TYPE_CHECKING:
    from talos.engine import TradingEngine

logger = structlog.get_logger()


class ArgoApp(TalosApp):
    """MLB Props trading terminal."""

    TITLE = "ARGO"
    SUB_TITLE = "MLB Props"

    def compose(self) -> ComposeResult:
        """Override compose to swap PortfolioPanel for ArgoPortfolioPanel."""
        yield Header()
        yield Static(
            "WEBSOCKET DISCONNECTED — ALL PRICES ARE STALE — RESTART ARGO",
            id="ws-disconnect-banner",
        )
        yield OpportunitiesTable(id="opportunities-table")
        if self._engine is not None:
            panel = ProposalPanel(self._engine.proposal_queue, id="proposal-panel")
            panel.display = False
            yield panel
        with Horizontal(id="bottom-panels"):
            yield ArgoPortfolioPanel(id="account-panel")
            yield ActivityLog(id="activity-log")
            yield OrderLog(id="order-log")
        yield Footer()

    # --- Override parent methods that query PortfolioPanel ---
    # TalosApp.refresh_opportunities, _poll_balance, _poll_settlements
    # all call self.query_one(PortfolioPanel) which would fail since
    # ArgoApp mounts ArgoPortfolioPanel instead. Override to intercept.

    async def _poll_balance(self) -> None:
        """Override: update balance but skip PortfolioPanel query."""
        if self._engine is None:
            return
        await self._engine.refresh_balance()
        # ArgoPortfolioPanel doesn't show cash — no-op here
        self._refresh_portfolio_panel()

    async def refresh_opportunities(self) -> None:
        """Override: call parent logic but replace portfolio panel update."""
        # Call parent for table refresh but catch PortfolioPanel queries
        if self._scanner is None:
            return
        table = self.query_one(OpportunitiesTable)
        table.refresh_from_scanner(self._scanner, engine=self._engine)
        if self._engine is not None:
            table.update_statuses(self._engine.event_statuses)
        self._refresh_portfolio_panel()

    async def _poll_settlements(self) -> None:
        """Override: skip PortfolioPanel P&L update (Argo doesn't show P&L)."""
        pass  # ArgoPortfolioPanel shows pair counts, not P&L

    async def action_scan(self) -> None:
        """Custom scan: discover MLB prop events via direct REST calls."""
        if self._engine is None:
            return
        rest = self._engine.rest_client

        # Fetch events for each prop series
        rows = []
        for series in SERIES:
            try:
                events = await rest.get_events(series_ticker=series)
                for event in events:
                    markets = await rest.get_markets(event_ticker=event.event_ticker)
                    active = [m for m in markets if m.status == "active"]
                    rows.append(format_scan_row(event, market_count=len(active)))
            except Exception:
                logger.exception("argo_scan_failed", series=series)

        if not rows:
            self.notify("No MLB prop events found", severity="warning")
            return

        def on_scan_result(selected_events: list) -> None:
            if selected_events:
                for event in selected_events:
                    self._add_event_markets(event)

        self.push_screen(ArgoScanScreen(rows), callback=on_scan_result)

    def _add_event_markets(self, event) -> None:
        """Add an event via the normal add_games path (triggers MarketPicker).

        Uses add_games (plural) since add_game (singular) doesn't exist.
        The non-sports path raises MarketPickerNeeded for multi-market events,
        which the inherited on_worker_state_changed handler catches and
        pushes MarketPickerScreen.
        """
        if self._engine is None:
            return
        self.run_worker(
            self._engine.add_games([event.event_ticker]),
            name=f"add-{event.event_ticker}",
        )

    def _refresh_portfolio_panel(self) -> None:
        """Compute pair stats from position ledgers and update the panel."""
        try:
            panel = self.query_one("#account-panel", ArgoPortfolioPanel)
        except Exception:
            return
        if self._engine is None:
            return

        completed = 0
        partial = 0
        open_pairs = 0
        total_tickers = 0
        active_tickers = 0
        unit_size = self._engine.unit_size

        for pair in self._engine.scanner.pairs:
            total_tickers += 1
            try:
                ledger = self._engine.adjuster.get_ledger(pair.event_ticker)
            except KeyError:
                continue

            yes_filled = ledger.filled_count(Side.A)
            no_filled = ledger.filled_count(Side.B)
            has_resting = (
                ledger.resting_order_id(Side.A) is not None
                or ledger.resting_order_id(Side.B) is not None
            )

            if yes_filled > 0 or no_filled > 0 or has_resting:
                active_tickers += 1

            stats = compute_pair_stats(yes_filled, no_filled, has_resting, unit_size)
            completed += stats.completed
            partial += stats.partial
            open_pairs += stats.open

        panel.update_stats(
            completed=completed,
            partial=partial,
            open_pairs=open_pairs,
            total_tickers=total_tickers,
            active_tickers=active_tickers,
        )
```

- [ ] **Step 2: Verify it imports cleanly**

Run: `.venv/Scripts/python -c "from argo.app import ArgoApp; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/argo/app.py
git commit -m "feat: ArgoApp subclass with scan override and portfolio panel swap"
```

---

## Task 9: Argo Entry Point (`__main__.py`)

**Files:**
- Create: `src/argo/__main__.py`

- [ ] **Step 1: Create the entry point**

This duplicates Talos's `__main__.py` wiring but with Argo-specific config. Key differences:
- Calls `set_data_dir()` first
- First-run check before loading config
- No `GameStatusResolver`
- No non-sports categories
- Uses `ArgoApp` instead of `TalosApp`

```python
"""Entry point: python -m argo."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _load_dotenv(env_file: Path) -> None:
    """Load .env file into os.environ."""
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


def main() -> None:
    """Launch the Argo MLB Props dashboard."""
    from argo.config import resolve_data_dir
    from talos.persistence import set_data_dir

    data_dir = resolve_data_dir()
    set_data_dir(data_dir)

    env_file = data_dir / ".env"

    # First-run setup if no credentials exist
    if not env_file.is_file():
        from argo.first_run import SetupApp

        setup = SetupApp(data_dir)
        setup.run()
        if not setup.setup_complete:
            sys.exit(0)

    _load_dotenv(env_file)

    try:
        from talos.config import KalshiConfig

        config = KalshiConfig.from_env()
    except ValueError as e:
        print(f"Configuration error: {e}")
        sys.exit(1)

    from talos.auth import KalshiAuth
    from talos.automation_config import AutomationConfig
    from talos.bid_adjuster import BidAdjuster
    from talos.data_collector import DataCollector
    from talos.engine import TradingEngine
    from talos.game_manager import GameManager
    from talos.lifecycle_feed import LifecycleFeed
    from talos.market_feed import MarketFeed
    from talos.orderbook import OrderBookManager
    from talos.persistence import (
        load_saved_games,
        load_saved_games_full,
        load_settings,
        save_games,
        save_games_full,
        save_settings,
    )
    from talos.portfolio_feed import PortfolioFeed
    from talos.position_feed import PositionFeed
    from talos.rest_client import KalshiRESTClient
    from talos.scanner import ArbitrageScanner
    from talos.settlement_tracker import SettlementCache
    from talos.suggestion_log import SuggestionLog
    from talos.ticker_feed import TickerFeed
    from talos.top_of_market import TopOfMarketTracker
    from talos.ws_client import KalshiWSClient

    from argo.app import ArgoApp

    auth = KalshiAuth(config.key_id, config.private_key_path)
    rest = KalshiRESTClient(auth, config)
    ws = KalshiWSClient(auth, config)
    books = OrderBookManager()
    feed = MarketFeed(ws, books)
    scanner = ArbitrageScanner(books)
    tracker = TopOfMarketTracker(books)
    settings = load_settings()

    from argo.config import DEFAULT_UNIT_SIZE

    unit_size = int(settings.get("unit_size", DEFAULT_UNIT_SIZE))  # type: ignore[arg-type]
    adjuster = BidAdjuster(books, [], unit_size=unit_size)
    portfolio_feed = PortfolioFeed(ws_client=ws)
    ticker_feed = TickerFeed(ws_client=ws)
    lifecycle_feed = LifecycleFeed(ws_client=ws)
    position_feed = PositionFeed(ws_client=ws)
    auto_config = AutomationConfig()

    from argo.config import FEE_RATE, FEE_TYPE

    game_mgr = GameManager(
        rest,
        feed,
        scanner,
        sports_enabled=False,
        nonsports_categories=[],
        nonsports_max_days=1,
        ticker_blacklist=settings.get("ticker_blacklist", []),  # type: ignore[arg-type]
        fee_type_override=FEE_TYPE,      # "fee_free" — MLB props have no fees
        fee_rate_override=FEE_RATE,      # 0.0
    )

    data_collector = DataCollector(data_dir / "talos_data.db")
    settlement_cache = SettlementCache(data_dir / "talos_data.db")

    # Wire scanner + tracker to book updates
    _app_ref: list[ArgoApp] = []

    def on_book_update(ticker: str) -> None:
        scanner.scan(ticker)
        for pair in scanner._pairs_by_ticker.get(ticker, []):
            for side_str in {pair.side_a, pair.side_b}:
                tracker.check(ticker, side=side_str)
        if _app_ref:
            for pair in scanner._pairs_by_ticker.get(ticker, []):
                _app_ref[0].mark_event_dirty(pair.event_ticker)

    feed.on_book_update = on_book_update

    # Wire game persistence
    saved_games_full = load_saved_games_full()
    saved_games = load_saved_games() if saved_games_full is None else []

    def _persist_games() -> None:
        save_games([p.event_ticker for p in game_mgr.active_games])
        save_games_full([
            {
                "event_ticker": p.event_ticker,
                "ticker_a": p.ticker_a,
                "ticker_b": p.ticker_b,
                "fee_type": p.fee_type,
                "fee_rate": p.fee_rate,
                "close_time": p.close_time,
                "expected_expiration_time": p.expected_expiration_time,
                "label": game_mgr.labels.get(p.event_ticker, ""),
                "sub_title": game_mgr.subtitles.get(p.event_ticker, ""),
                "side_a": p.side_a,
                "side_b": p.side_b,
                "kalshi_event_ticker": p.kalshi_event_ticker,
                "series_ticker": p.series_ticker,
            }
            for p in game_mgr.active_games
        ])

    game_mgr.on_change = _persist_games

    engine = TradingEngine(
        scanner=scanner,
        game_manager=game_mgr,
        rest_client=rest,
        market_feed=feed,
        tracker=tracker,
        adjuster=adjuster,
        initial_games=saved_games,
        initial_games_full=saved_games_full,
        automation_config=auto_config,
        portfolio_feed=portfolio_feed,
        ticker_feed=ticker_feed,
        lifecycle_feed=lifecycle_feed,
        position_feed=position_feed,
        game_status_resolver=None,
        data_collector=data_collector,
        settlement_cache=settlement_cache,
    )

    # Wire unit size persistence
    def _persist_unit_size(size: int) -> None:
        s = load_settings()
        s["unit_size"] = size
        save_settings(s)

    engine.on_unit_size_change = _persist_unit_size

    # Wire blacklist persistence
    def _persist_blacklist(blacklist: list[str]) -> None:
        s = load_settings()
        s["ticker_blacklist"] = blacklist
        save_settings(s)

    engine.on_blacklist_change = _persist_blacklist

    # Wire suggestion audit log
    suggestion_log = SuggestionLog(data_dir / "suggestions.log")
    engine.proposal_queue.on_lifecycle = suggestion_log.log

    app = ArgoApp(engine=engine)
    _app_ref.append(app)
    app.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it imports cleanly**

Run: `.venv/Scripts/python -c "from argo.__main__ import main; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/argo/__main__.py
git commit -m "feat: Argo entry point with engine wiring"
```

---

## Task 10: Icon Conversion + PyInstaller Spec

**Files:**
- Create: `icons/argo/argo.ico` (convert from SVG)
- Create: `argo.spec`

- [ ] **Step 1: Convert SVG to ICO**

```bash
.venv/Scripts/pip install Pillow cairosvg
.venv/Scripts/python -c "
import cairosvg
from PIL import Image
from io import BytesIO

# SVG → PNG at 256x256
png_data = cairosvg.svg2png(
    url='icons/argo/navigation/iter_02 - Copy.svg',
    output_width=256, output_height=256,
)
img = Image.open(BytesIO(png_data))
# Save as ICO with multiple sizes
img.save('icons/argo/argo.ico', format='ICO',
         sizes=[(16,16), (32,32), (48,48), (64,64), (128,128), (256,256)])
print('Created icons/argo/argo.ico')
"
```

- [ ] **Step 2: Create `argo.spec`**

```python
# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Argo — MLB Props trading terminal."""

a = Analysis(
    ["src/argo/__main__.py"],
    pathex=["src"],
    datas=[],
    hiddenimports=[
        "talos",
        "talos.ui",
        "talos.ui.app",
        "talos.ui.theme",
        "talos.ui.widgets",
        "talos.ui.screens",
        "talos.ui.proposal_panel",
        "talos.models",
        "talos.models.market",
        "talos.models.order",
        "talos.models.portfolio",
        "talos.models.strategy",
        "talos.models.ws",
        "argo",
        "argo.app",
        "argo.config",
        "argo.first_run",
        "argo.screens",
        "argo.widgets",
        # Textual internals PyInstaller misses
        "textual",
        "textual.css",
        "textual.widgets",
        "textual.screen",
        "textual._xterm_parser",
        "textual._animator",
        # Core deps PyInstaller may miss
        "structlog",
        "httpx",
        "httpx._transports",
        "pydantic",
        "pydantic._internal",
        "websockets",
        # Cryptography for RSA signing
        "cryptography.hazmat.primitives.asymmetric.padding",
        "cryptography.hazmat.primitives.asymmetric.rsa",
        "cryptography.hazmat.primitives.hashes",
        "cryptography.hazmat.primitives.serialization",
        # Talos modules with dynamic imports
        "talos.ui.event_review",
        "talos.auto_accept_log",
        "talos.suggestion_log",
    ],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="Argo",
    icon="icons/argo/argo.ico",
    console=True,
    onefile=True,
)
```

- [ ] **Step 3: Commit**

```bash
git add icons/argo/argo.ico argo.spec
git commit -m "build: PyInstaller spec + icon for Argo"
```

---

## Task 11: Build + Smoke Test

**Files:** None new — this is a verification task.

- [ ] **Step 1: Install PyInstaller**

```bash
.venv/Scripts/pip install pyinstaller
```

- [ ] **Step 2: Build the exe**

```bash
.venv/Scripts/pyinstaller argo.spec --noconfirm
```

Expected: `dist/Argo.exe` created (30-60MB)

- [ ] **Step 3: Test exe launches (dev machine)**

```bash
dist/Argo.exe
```

Expected: Since no `.env` exists in `dist/`, the SetupScreen should appear.

- [ ] **Step 4: Run full test suite to verify nothing is broken**

```bash
.venv/Scripts/python -m pytest -x
```

Expected: All tests pass (existing Talos tests + new Argo tests)

- [ ] **Step 5: Final commit with any fixes from smoke test**

```bash
git add -A
git commit -m "build: verified Argo.exe builds and launches"
```

---

## Task Order Summary

| Task | Description | Depends On |
|------|-------------|------------|
| 1 | Configurable data dir in persistence.py | — |
| 2 | Update hardcoded paths + rest_client property + fee override | 1 |
| 3 | Add `get_markets()` to REST client | — |
| 4 | pyproject.toml + Argo package scaffold | — |
| 5 | ArgoPortfolioPanel widget | 4 |
| 6 | First-run setup screen | 4 |
| 7 | ArgoScanScreen | 4 |
| 8 | ArgoApp subclass | 5, 7 |
| 9 | Argo entry point | 1, 2, 3, 6, 8 |
| 10 | Icon + PyInstaller spec | 4 |
| 11 | Build + smoke test | 9, 10 |

Tasks 1, 3, and 4 can run in parallel. Tasks 5, 6, 7 can run in parallel after 4. Task 8 depends on 5 + 7. Task 9 is the integration point. Task 11 is final verification.
