# Talos Distributable Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package Talos as a single Windows exe (`Talos.exe`) with first-run credential setup, a sports/non-sports scan toggle, extended auto-accept, and configurable data directory for PyInstaller.

**Architecture:** Direct changes to `src/talos/` — no wrapper package. Six files modified, two new files created. All changes are backward-compatible with `python -m talos`.

**Tech Stack:** Python 3.12+, Textual (TUI), PyInstaller (onefile), Pydantic v2, httpx (async HTTP), cryptography (RSA auth)

**Spec:** `docs/superpowers/specs/2026-03-26-talos-distributable-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `src/talos/persistence.py` | Modify | Add `set_data_dir()` / `get_data_dir()`, convert path constants to functions |
| `src/talos/__main__.py` | Modify | Frozen-mode data dir, `_load_dotenv()` fix, first-run detection, production guard, default unit_size 5 |
| `src/talos/ui/first_run.py` | **Create** | `SetupScreen` Textual modal — credential entry, validation, `.env` + `settings.json` writing |
| `src/talos/ui/screens.py` | Modify | Raise auto-accept manual cap from 24h to 168h |
| `src/talos/ui/app.py` | Modify | Fix `parents[3]` paths, raise auto-start to 168h, add `m` keybinding for scan mode toggle |
| `src/talos/game_manager.py` | Modify | Add `scan_mode` parameter to `scan_events()` |
| `talos.spec` | **Create** | PyInstaller build specification |
| `tests/test_persistence_data_dir.py` | **Create** | Tests for `set_data_dir()` / `get_data_dir()` |
| `tests/test_first_run.py` | **Create** | Tests for `SetupScreen` env writing |
| `tests/test_auto_accept_duration.py` | **Create** | Tests for extended auto-accept duration (>24h) |

---

### Task 1: Configurable Data Directory in persistence.py

**Files:**
- Modify: `src/talos/persistence.py` (full file, 90 lines)
- Create: `tests/test_persistence_data_dir.py`

- [ ] **Step 1: Write failing tests for `set_data_dir` / `get_data_dir`**

Create `tests/test_persistence_data_dir.py`:

```python
"""Tests for configurable data directory in persistence module."""

from __future__ import annotations

from pathlib import Path

import pytest

from talos.persistence import get_data_dir, set_data_dir


class TestGetDataDir:
    """get_data_dir returns the correct path based on configuration."""

    def teardown_method(self) -> None:
        """Reset data dir after each test."""
        set_data_dir(None)

    def test_default_returns_project_root(self) -> None:
        """When set_data_dir was never called, returns parents[2] of persistence.py."""
        set_data_dir(None)
        result = get_data_dir()
        # persistence.py is at src/talos/persistence.py — parents[2] is project root
        assert result.name != ""
        assert result.is_dir()

    def test_set_data_dir_overrides(self, tmp_path: Path) -> None:
        """After set_data_dir(path), get_data_dir returns that path."""
        set_data_dir(tmp_path)
        assert get_data_dir() == tmp_path

    def test_set_data_dir_none_resets(self, tmp_path: Path) -> None:
        """set_data_dir(None) resets to default behavior."""
        set_data_dir(tmp_path)
        assert get_data_dir() == tmp_path
        set_data_dir(None)
        assert get_data_dir() != tmp_path


class TestPathFunctions:
    """File-path functions resolve against get_data_dir."""

    def teardown_method(self) -> None:
        set_data_dir(None)

    def test_load_settings_uses_data_dir(self, tmp_path: Path) -> None:
        """load_settings reads from get_data_dir() / 'settings.json'."""
        import json

        set_data_dir(tmp_path)
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"unit_size": 42}))
        from talos.persistence import load_settings

        result = load_settings()
        assert result["unit_size"] == 42

    def test_save_settings_uses_data_dir(self, tmp_path: Path) -> None:
        """save_settings writes to get_data_dir() / 'settings.json'."""
        import json

        set_data_dir(tmp_path)
        from talos.persistence import save_settings

        save_settings({"unit_size": 7})
        result = json.loads((tmp_path / "settings.json").read_text())
        assert result["unit_size"] == 7

    def test_load_saved_games_uses_data_dir(self, tmp_path: Path) -> None:
        """load_saved_games reads from get_data_dir() / 'games.json'."""
        import json

        set_data_dir(tmp_path)
        (tmp_path / "games.json").write_text(json.dumps(["EVT-1", "EVT-2"]))
        from talos.persistence import load_saved_games

        assert load_saved_games() == ["EVT-1", "EVT-2"]

    def test_save_games_uses_data_dir(self, tmp_path: Path) -> None:
        """save_games writes to get_data_dir() / 'games.json'."""
        import json

        set_data_dir(tmp_path)
        from talos.persistence import save_games

        save_games(["A", "B"])
        result = json.loads((tmp_path / "games.json").read_text())
        assert result == ["A", "B"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_persistence_data_dir.py -v`
Expected: FAIL — `set_data_dir` and `get_data_dir` not defined

- [ ] **Step 3: Implement `set_data_dir` / `get_data_dir` and convert path constants**

Replace the entire `src/talos/persistence.py` with:

```python
"""Game list persistence — saves/loads event tickers to a JSON file."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Configurable data directory
# ---------------------------------------------------------------------------
_data_dir: Path | None = None


def set_data_dir(path: Path | None) -> None:
    """Override the base directory for all runtime files.

    Call before any other persistence function. Pass None to reset.
    """
    global _data_dir
    _data_dir = path


def get_data_dir() -> Path:
    """Return the data directory.

    Resolution order:
    1. Explicitly set via set_data_dir()
    2. PyInstaller frozen → directory containing the exe
    3. Development → two parents up from this file (project root)
    """
    if _data_dir is not None:
        return _data_dir
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Path helpers (resolve against get_data_dir at call time, not import time)
# ---------------------------------------------------------------------------
def _games_file(path: Path | None = None) -> Path:
    return path or (get_data_dir() / "games.json")


def _settings_file(path: Path | None = None) -> Path:
    return path or (get_data_dir() / "settings.json")


def _games_full_file(path: Path | None = None) -> Path:
    return path or (get_data_dir() / "games_full.json")


# ---------------------------------------------------------------------------
# Games persistence
# ---------------------------------------------------------------------------
def load_saved_games(path: Path | None = None) -> list[str]:
    """Load saved game event tickers from disk."""
    games_file = _games_file(path)
    if not games_file.is_file():
        return []
    try:
        data = json.loads(games_file.read_text())
        if isinstance(data, list):
            return [str(t) for t in data if isinstance(t, str)]
    except Exception:
        logger.debug("load_saved_games_failed", path=str(games_file))
    return []


def save_games(tickers: list[str], path: Path | None = None) -> None:
    """Save game event tickers to disk (legacy format)."""
    games_file = _games_file(path)
    try:
        games_file.write_text(json.dumps(tickers, indent=2) + "\n")
    except Exception:
        logger.debug("save_games_failed", path=str(games_file))


def save_games_full(
    games: list[dict[str, str | float | None]], path: Path | None = None
) -> None:
    """Save full game data so startup can skip REST calls."""
    games_file = _games_full_file(path)
    try:
        games_file.write_text(json.dumps(games, indent=2) + "\n")
    except Exception:
        logger.debug("save_games_full_failed", path=str(games_file))


def load_saved_games_full(
    path: Path | None = None,
) -> list[dict[str, str | float]] | None:
    """Load full game data. Returns None if not available (fallback to tickers)."""
    games_file = _games_full_file(path)
    if not games_file.is_file():
        return None
    try:
        data = json.loads(games_file.read_text())
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data
    except Exception:
        logger.debug("load_saved_games_full_failed", path=str(games_file))
    return None


# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------
def load_settings(path: Path | None = None) -> dict[str, object]:
    """Load persisted settings from disk."""
    settings_file = _settings_file(path)
    if not settings_file.is_file():
        return {}
    try:
        data = json.loads(settings_file.read_text())
        if isinstance(data, dict):
            return data
    except Exception:
        logger.debug("load_settings_failed", path=str(settings_file))
    return {}


def save_settings(settings: dict[str, object], path: Path | None = None) -> None:
    """Save settings to disk."""
    settings_file = _settings_file(path)
    try:
        settings_file.write_text(json.dumps(settings, indent=2) + "\n")
    except Exception:
        logger.debug("save_settings_failed", path=str(settings_file))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_persistence_data_dir.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Run full test suite to verify no regressions**

Run: `.venv/Scripts/python -m pytest -x`
Expected: All existing tests still pass (functions accept same `path` overrides)

- [ ] **Step 6: Commit**

```bash
git add src/talos/persistence.py tests/test_persistence_data_dir.py
git commit -m "feat: add configurable data directory to persistence module"
```

---

### Task 2: Update __main__.py for Frozen Mode + Defaults

**Files:**
- Modify: `src/talos/__main__.py` (lines 10-14, 28, 80, 98-100, 181)

**Depends on:** Task 1

- [ ] **Step 1: Update `_load_dotenv()` to use `get_data_dir()`**

In `src/talos/__main__.py`, replace the `_load_dotenv` function (lines 10-23):

```python
def _load_dotenv() -> None:
    """Load .env file from data directory if it exists."""
    from talos.persistence import get_data_dir

    env_file = get_data_dir() / ".env"
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
```

- [ ] **Step 2: Add frozen-mode detection + production guard at top of `main()`**

Replace lines 26-41 of `main()` with:

```python
def main() -> None:
    """Launch the Talos dashboard."""
    # Frozen mode (PyInstaller): set data dir to exe's directory
    if getattr(sys, "frozen", False):
        from talos.persistence import set_data_dir

        set_data_dir(Path(sys.executable).parent)

    _load_dotenv()

    # Production-only guard for frozen builds
    if getattr(sys, "frozen", False) and os.environ.get("KALSHI_ENV") != "production":
        os.environ["KALSHI_ENV"] = "production"

    try:
        from talos.config import KalshiConfig

        config = KalshiConfig.from_env()
    except ValueError:
        # No .env yet — launch first-run setup if frozen, else error out
        if getattr(sys, "frozen", False):
            _run_first_time_setup()
            # Reload .env and retry — exit if still broken
            _load_dotenv()
            try:
                config = KalshiConfig.from_env()
            except ValueError:
                print("Setup did not complete — exiting.")
                sys.exit(1)
        else:
            print("Configuration error — create a .env file (see .env.example)")
            sys.exit(1)
```

- [ ] **Step 3: Add `_run_first_time_setup()` helper**

Add after `_load_dotenv()`:

```python
def _run_first_time_setup() -> None:
    """Launch the first-run setup screen to collect credentials."""
    from talos.ui.first_run import FirstRunApp

    app = FirstRunApp()
    app.run()
```

- [ ] **Step 4: Change default unit_size from 10 to 5**

Replace line 80:
```python
    unit_size = int(settings.get("unit_size", 5))  # type: ignore[arg-type]
```

- [ ] **Step 5: Replace all remaining hardcoded paths with `get_data_dir()`**

Replace lines 98-100:
```python
    from talos.persistence import get_data_dir

    db_dir = get_data_dir()
    data_collector = DataCollector(db_dir / "talos_data.db")
    settlement_cache = SettlementCache(db_dir / "talos_data.db")
```

Replace line 181:
```python
    log_path = get_data_dir() / "suggestions.log"
```

- [ ] **Step 6: Run full test suite**

Run: `.venv/Scripts/python -m pytest -x`
Expected: All pass (normal dev mode still uses `parents[2]` via `get_data_dir()` default)

- [ ] **Step 7: Commit**

```bash
git add src/talos/__main__.py
git commit -m "feat: frozen-mode data dir, first-run detection, default unit_size 5"
```

---

### Task 3: Fix Hardcoded Paths in app.py

**Files:**
- Modify: `src/talos/ui/app.py` (lines 322, 737-740, 1017)

**Depends on:** Task 1

- [ ] **Step 1: Fix `_start_watchdog` freeze log path (line 322)**

Replace:
```python
        log_path = "talos_freeze.log"
```
With:
```python
        from talos.persistence import get_data_dir

        log_path = str(get_data_dir() / "talos_freeze.log")
```

- [ ] **Step 2: Fix `action_review_event` paths (lines 737-739)**

Replace:
```python
        from pathlib import Path

        base = Path(__file__).resolve().parents[3]
        db_path = base / "talos_data.db"
        log_path = base / "suggestions.log"
```
With:
```python
        from talos.persistence import get_data_dir

        base = get_data_dir()
        db_path = base / "talos_data.db"
        log_path = base / "suggestions.log"
```

- [ ] **Step 3: Fix `_start_auto_accept` session log path (line 1017)**

Replace:
```python
        from pathlib import Path

        self._auto_accept.start(hours=hours)

        log_dir = Path(__file__).resolve().parents[3] / "auto_accept_sessions"
```
With:
```python
        from talos.persistence import get_data_dir

        self._auto_accept.start(hours=hours)

        log_dir = get_data_dir() / "auto_accept_sessions"
```

- [ ] **Step 4: Run full test suite**

Run: `.venv/Scripts/python -m pytest -x`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/talos/ui/app.py
git commit -m "fix: replace hardcoded parents[3] paths with get_data_dir()"
```

---

### Task 4: Raise Auto-Accept Duration Cap

**Files:**
- Modify: `src/talos/ui/screens.py` (line 266-268)
- Modify: `src/talos/ui/app.py` (line 129)
- Create: `tests/test_auto_accept_duration.py`

**Note:** `auto_accept.py` needs no changes — `AutoAcceptState` already supports arbitrary `timedelta`. Only the UI validation cap needs raising.

- [ ] **Step 1: Write failing test for 168h cap**

Create `tests/test_auto_accept_duration.py`:

```python
"""Tests for extended auto-accept duration cap."""

from __future__ import annotations

from talos.auto_accept import AutoAcceptState


class TestAutoAcceptDuration:
    """AutoAcceptState handles durations beyond 24h."""

    def test_accepts_168h_duration(self) -> None:
        state = AutoAcceptState()
        state.start(hours=168.0)
        assert state.active
        assert state.duration is not None
        assert state.duration.total_seconds() == 168 * 3600

    def test_remaining_seconds_for_long_duration(self) -> None:
        state = AutoAcceptState()
        state.start(hours=100.0)
        # Should have roughly 100h remaining (minus tiny elapsed)
        assert state.remaining_seconds() > 99 * 3600

    def test_not_expired_within_168h(self) -> None:
        state = AutoAcceptState()
        state.start(hours=168.0)
        assert not state.is_expired()
```

- [ ] **Step 2: Run tests to verify they pass (AutoAcceptState already supports this)**

Run: `.venv/Scripts/python -m pytest tests/test_auto_accept_duration.py -v`
Expected: All 3 PASS (state has no cap — these validate the contract)

- [ ] **Step 3: Raise manual cap from 24h to 168h in AutoAcceptScreen**

In `src/talos/ui/screens.py`, replace lines 266-269:

```python
            if hours <= 0 or hours > 168:
                self.query_one("#modal-error", Label).update(
                    "Duration must be greater than 0 and at most 168 hours"
                )
```

- [ ] **Step 4: Raise auto-start from 24h to 168h in app.py**

In `src/talos/ui/app.py`, replace line 129:

```python
            self._start_auto_accept(168.0)
```

- [ ] **Step 5: Run full test suite**

Run: `.venv/Scripts/python -m pytest -x`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add src/talos/ui/screens.py src/talos/ui/app.py tests/test_auto_accept_duration.py
git commit -m "feat: raise auto-accept duration cap from 24h to 168h"
```

---

### Task 5: Sports / Non-Sports Scan Mode Toggle

**Files:**
- Modify: `src/talos/game_manager.py` (line 626 — `scan_events` signature)
- Modify: `src/talos/ui/app.py` (BINDINGS, `__init__`, `action_scan`, new `action_toggle_scan_mode`)

- [ ] **Step 1: Add `scan_mode` parameter to `scan_events()`**

In `src/talos/game_manager.py`, change line 626:

```python
    async def scan_events(
        self, scan_mode: str = "sports",
    ) -> list[Event]:
```

Then gate the two paths on `scan_mode`. Replace lines 638-639:
```python
        sports_events: list[Event] = []
        if self._sports_enabled and scan_mode in ("sports", "both"):
```

Replace lines 668-669:
```python
        nonsports_events: list[Event] = []
        if self._nonsports_categories and scan_mode in ("nonsports", "both"):
```

- [ ] **Step 2: Add scan mode state and keybinding to TalosApp**

In `src/talos/ui/app.py`, add to BINDINGS (after line 72):
```python
        ("m", "toggle_scan_mode", "Mode"),
```

In `__init__` (after line 89):
```python
        self._scan_mode: str = "sports"
```

- [ ] **Step 3: Add toggle action and status display**

Add new method to `TalosApp`:

```python
    def action_toggle_scan_mode(self) -> None:
        """Toggle between sports and non-sports scan mode."""
        if self._scan_mode == "sports":
            self._scan_mode = "nonsports"
        else:
            self._scan_mode = "sports"
        mode_label = "SPORTS" if self._scan_mode == "sports" else "NON-SPORTS"
        self.sub_title = f"[{mode_label}]"
        self.notify(f"Scan mode: {mode_label}")
```

Set initial subtitle in `on_mount` (after line 108, before the `if` blocks):
```python
        self.sub_title = "[SPORTS]"
```

- [ ] **Step 4: Pass scan_mode to scan_events in _run_scan**

In `_run_scan`, replace line 651:
```python
            events = await self._engine.game_manager.scan_events(
                scan_mode=self._scan_mode,
            )
```

- [ ] **Step 5: Run tests**

Run: `.venv/Scripts/python -m pytest -x`
Expected: All pass — existing callers don't pass `scan_mode`, so they get default `"sports"` (matching current UI default). Any code needing both paths should pass `scan_mode="both"` explicitly.

- [ ] **Step 6: Commit**

```bash
git add src/talos/game_manager.py src/talos/ui/app.py
git commit -m "feat: add sports/non-sports scan mode toggle (m key)"
```

---

### Task 6: First-Run Setup Screen

**Files:**
- Create: `src/talos/ui/first_run.py`
- Create: `tests/test_first_run.py`

**Depends on:** Task 1

- [ ] **Step 1: Write failing tests**

Create `tests/test_first_run.py`:

```python
"""Tests for first-run setup screen."""

from __future__ import annotations

import json
from pathlib import Path

from talos.ui.first_run import write_env_file, write_default_settings


class TestWriteEnvFile:
    """write_env_file creates a valid .env file."""

    def test_writes_production_env(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        write_env_file(
            env_path,
            key_id="abc-123",
            key_path=r"C:\Users\test\kalshi.key",
        )
        content = env_path.read_text()
        assert "KALSHI_KEY_ID=abc-123" in content
        assert r"KALSHI_PRIVATE_KEY_PATH=C:\Users\test\kalshi.key" in content
        assert "KALSHI_ENV=production" in content

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text("OLD=stuff\n")
        write_env_file(env_path, key_id="new", key_path="/new/path")
        content = env_path.read_text()
        assert "OLD" not in content
        assert "KALSHI_KEY_ID=new" in content


class TestWriteDefaultSettings:
    """write_default_settings creates settings.json with correct defaults."""

    def test_writes_unit_size_5(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        write_default_settings(settings_path)
        data = json.loads(settings_path.read_text())
        assert data["unit_size"] == 5
        assert data["ticker_blacklist"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_first_run.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Create `src/talos/ui/first_run.py`**

```python
"""First-run setup — collects Kalshi credentials on initial launch."""

from __future__ import annotations

import json
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Footer, Header, Input, Label, Static

from talos.errors import KalshiAPIError
from talos.ui.theme import APP_CSS


def write_env_file(path: Path, *, key_id: str, key_path: str) -> None:
    """Write a .env file with Kalshi production credentials."""
    path.write_text(
        f"KALSHI_KEY_ID={key_id}\n"
        f"KALSHI_PRIVATE_KEY_PATH={key_path}\n"
        f"KALSHI_ENV=production\n"
    )


def write_default_settings(path: Path) -> None:
    """Write default settings.json for new installs."""
    path.write_text(json.dumps({"unit_size": 5, "ticker_blacklist": []}, indent=2) + "\n")


class SetupScreen(Static):
    """Credential entry form for first-time setup."""

    def compose(self) -> ComposeResult:
        yield Label("Talos — First-Time Setup", classes="modal-title")
        yield Label("")
        yield Label("Kalshi API Key ID:")
        yield Input(placeholder="e.g. abc123-def456-...", id="key-id-input")
        yield Label("")
        yield Label("Path to RSA Private Key file:")
        yield Input(placeholder=r"e.g. C:\Users\you\kalshi.key", id="key-path-input")
        yield Label("")
        yield Label("", id="setup-error", classes="modal-error")
        yield Button("Save & Launch", id="save-btn", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "save-btn":
            return
        key_id = self.query_one("#key-id-input", Input).value.strip()
        key_path = self.query_one("#key-path-input", Input).value.strip()
        error_label = self.query_one("#setup-error", Label)

        if not key_id:
            error_label.update("API Key ID is required")
            return
        if not key_path:
            error_label.update("Private key path is required")
            return
        if not Path(key_path).is_file():
            error_label.update(f"File not found: {key_path}")
            return

        # Validate authentication
        error_label.update("Validating credentials...")
        self.app.call_later(self._validate_and_save, key_id, key_path)

    async def _validate_and_save(self, key_id: str, key_path: str) -> None:
        """Attempt auth, then write config files and exit."""
        error_label = self.query_one("#setup-error", Label)
        try:
            from talos.auth import KalshiAuth
            from talos.config import KalshiConfig, KalshiEnvironment

            auth = KalshiAuth(key_id, Path(key_path))
            config = KalshiConfig(
                environment=KalshiEnvironment.PRODUCTION,
                key_id=key_id,
                private_key_path=Path(key_path),
                rest_base_url="https://api.elections.kalshi.com/trade-api/v2",
                ws_url="wss://api.elections.kalshi.com/trade-api/ws/v2",
            )
            from talos.rest_client import KalshiRESTClient

            rest = KalshiRESTClient(auth, config)
            await rest.get_balance()
        except FileNotFoundError:
            error_label.update("Could not read private key file — check the path")
            return
        except (ValueError, OSError) as e:
            error_label.update(f"Could not read private key file: {e}")
            return
        except KalshiAPIError as e:
            if e.status_code in (401, 403):
                error_label.update("Authentication failed — check your API key ID")
            else:
                error_label.update(f"API error ({e.status_code}): {e}")
            return
        except Exception as e:
            error_label.update(f"Could not reach Kalshi — check your internet: {e}")
            return

        # Success — write config files
        from talos.persistence import get_data_dir

        data_dir = get_data_dir()
        write_env_file(data_dir / ".env", key_id=key_id, key_path=key_path)
        write_default_settings(data_dir / "settings.json")
        error_label.update("Setup complete — restarting...")
        self.app.exit()


class FirstRunApp(App):
    """Minimal Textual app for first-run credential setup."""

    CSS = APP_CSS
    TITLE = "TALOS SETUP"

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="setup-container"):
            yield SetupScreen()
        yield Footer()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_first_run.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Run full test suite**

Run: `.venv/Scripts/python -m pytest -x`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add src/talos/ui/first_run.py tests/test_first_run.py
git commit -m "feat: add first-run setup screen for credential entry"
```

---

### Task 7: PyInstaller Build Spec

**Files:**
- Create: `talos.spec`

- [ ] **Step 1: Create `talos.spec` in project root**

```python
# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Talos.exe — single-file distributable."""

from PyInstaller.utils.hooks import collect_data_files

textual_datas = collect_data_files("textual")

a = Analysis(
    ["src/talos/__main__.py"],
    pathex=["src"],
    datas=textual_datas,
    hiddenimports=[
        # Talos core
        "talos", "talos.auth", "talos.config", "talos.errors",
        "talos.engine", "talos.scanner", "talos.game_manager",
        "talos.bid_adjuster", "talos.rebalance", "talos.fees",
        "talos.persistence", "talos.orderbook", "talos.rest_client",
        "talos.ws_client", "talos.game_status", "talos.automation_config",
        "talos.market_feed", "talos.ticker_feed", "talos.portfolio_feed",
        "talos.position_feed", "talos.lifecycle_feed",
        "talos.top_of_market", "talos.position_ledger",
        "talos.opportunity_proposer", "talos.proposal_queue",
        "talos.auto_accept", "talos.auto_accept_log",
        "talos.suggestion_log", "talos.data_collector",
        "talos.settlement_tracker", "talos.cpm",
        # Talos UI
        "talos.ui", "talos.ui.app", "talos.ui.theme",
        "talos.ui.widgets", "talos.ui.screens", "talos.ui.first_run",
        "talos.ui.proposal_panel", "talos.ui.event_review",
        # Talos models
        "talos.models", "talos.models.market", "talos.models.order",
        "talos.models.portfolio", "talos.models.strategy", "talos.models.ws",
        # Dependencies
        "structlog", "httpx", "httpx._transports",
        "pydantic", "pydantic._internal", "websockets",
        # Textual internals
        "textual", "textual.css", "textual.widgets", "textual.screen",
        "textual._xterm_parser", "textual._animator",
        # Cryptography (RSA signing)
        "cryptography.hazmat.primitives.asymmetric.padding",
        "cryptography.hazmat.primitives.asymmetric.rsa",
        "cryptography.hazmat.primitives.hashes",
        "cryptography.hazmat.primitives.serialization",
    ],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="Talos",
    icon="icon.ico",
    console=True,
    onefile=True,
)
```

- [ ] **Step 2: Verify PyInstaller is available**

Run: `.venv/Scripts/python -m PyInstaller --version`
If missing: `pip install pyinstaller`

- [ ] **Step 3: Build the exe**

Run: `.venv/Scripts/python -m PyInstaller talos.spec --noconfirm`
Expected: `dist/Talos.exe` created (50-70 MB)

- [ ] **Step 4: Smoke test the exe**

1. Copy `dist/Talos.exe` to a clean temp directory
2. Double-click — should show first-run setup screen (no `.env` present)
3. Verify it can accept input and the UI renders correctly
4. Press Ctrl+C / close window to exit

- [ ] **Step 5: Commit**

```bash
git add talos.spec
git commit -m "build: add PyInstaller spec for Talos.exe distributable"
```

---

## Task Dependency Graph

```
Task 1 (persistence)
  ├──→ Task 2 (__main__.py)
  ├──→ Task 3 (app.py paths)
  └──→ Task 6 (first-run screen)
Task 4 (auto-accept cap) — independent
Task 5 (scan mode toggle) — independent
Task 7 (PyInstaller) — depends on all above
```

**Parallel opportunities:** Tasks 1→{2,3,6} can run first. Tasks 4 and 5 are independent and can run in parallel with anything. Task 7 is the final integration step.
