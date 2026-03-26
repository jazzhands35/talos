# Talos Distributable — Design Spec

**Date:** 2026-03-26
**Goal:** Package Talos as a single Windows executable (`Talos.exe`) with first-run setup, so it can be sent to someone with zero Python knowledge.

## Summary

The recipient receives `Talos.exe`, launches it, enters their Kalshi API credentials, and is immediately trading — full sports + non-sports scanning, NO+NO arbitrage, bid adjustment, rebalancing. No Python install, no `.env` editing, no terminal knowledge required.

## Non-Goals

- Demo environment support (production only)
- Custom branding (same Talos name and stone helmet icon)
- New trading features — this is the existing Talos, packaged

## Architecture

No new package. Changes go directly into `src/talos/`:

1. **Configurable data directory** in `persistence.py` — all runtime files resolve relative to exe location when frozen
2. **First-run setup screen** in `ui/first_run.py` — collects credentials, writes `.env`
3. **Auto-accept cap removal** in `ui/screens.py` and `auto_accept.py` — raise from 24h to 168h (1 week)
4. **Default unit_size change** — 5 instead of 10 when no settings.json exists
5. **Sports/non-sports toggle** — keybinding in the main app to switch scan modes
6. **PyInstaller spec** — `talos.spec` in project root

## First-Run Setup

### Detection

```
Launch → resolve data_dir →
  .env exists in data_dir? → load .env → normal startup
  .env missing?            → push SetupScreen before main app
```

### SetupScreen (Textual modal)

New file: `src/talos/ui/first_run.py`

Fields:
- **Kalshi API Key ID** — text input, required
- **Path to RSA Private Key** — text input, required (absolute path to `.pem` file)

Buttons:
- **Save & Launch** — validates, writes config, dismisses to main app

Validation:
1. Both fields non-empty
2. Key file exists at given path
3. Can authenticate with Kalshi production API (attempt `GET /portfolio/balance`)

On success:
- Writes `.env` to data_dir:
  ```
  KALSHI_KEY_ID=<entered value>
  KALSHI_PRIVATE_KEY_PATH=<entered path>
  KALSHI_ENV=production
  ```
- Writes `settings.json` with defaults:
  ```json
  {
    "unit_size": 5,
    "ticker_blacklist": []
  }
  ```
- Reloads environment and continues to normal TalosApp startup

### UX Notes

- No environment toggle — production is hardcoded
- No unit_size input during setup — defaults to 5, changeable later via existing `u` keybinding
- Plain `input()` is NOT needed here — Textual Input widgets work fine inside Textual apps (the conhost paste issue only affects mixed Textual + raw input)

## Configurable Data Directory

### persistence.py Changes

Add module-level state:

```python
_data_dir: Path | None = None

def set_data_dir(path: Path | None) -> None:
    """Override the base directory for all runtime files."""
    global _data_dir
    _data_dir = path

def get_data_dir() -> Path:
    """Return the data directory — exe dir if frozen, project root otherwise."""
    if _data_dir is not None:
        return _data_dir
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parents[2]
```

All file-path constants (`_GAMES_FILE`, `_SETTINGS_FILE`, `_GAMES_FULL_FILE`) become functions that resolve against `get_data_dir()`.

**Call ordering constraint:** `set_data_dir()` must be called before any persistence function is invoked. The frozen-mode fallback in `get_data_dir()` provides a safety net, but explicit `set_data_dir()` at startup is the primary mechanism.

### __main__.py Changes

At the top of `main()`, before `_load_dotenv()`:

```python
import sys
from talos.persistence import set_data_dir, get_data_dir

if getattr(sys, "frozen", False):
    set_data_dir(Path(sys.executable).parent)
```

`_load_dotenv()` must also resolve `.env` from `get_data_dir()` instead of `parents[2]`:

```python
def _load_dotenv() -> None:
    from talos.persistence import get_data_dir
    env_file = get_data_dir() / ".env"
    ...
```

All other hardcoded `Path(__file__).resolve().parents[2]` references in `__main__.py` (lines 98-100, 181) use `get_data_dir()` instead.

### app.py Path Fixes

`app.py` has three hardcoded paths that will break under PyInstaller:

| Line | Current | Fix |
|------|---------|-----|
| 737 | `Path(__file__).resolve().parents[3]` | `get_data_dir()` — for `talos_data.db` and `suggestions.log` |
| 1017 | `Path(__file__).resolve().parents[3] / "auto_accept_sessions"` | `get_data_dir() / "auto_accept_sessions"` |
| 322 | `"talos_freeze.log"` (relative) | `get_data_dir() / "talos_freeze.log"` |

### Backward Compatibility

When `set_data_dir()` is never called (normal `python -m talos`), `get_data_dir()` returns `parents[2]` — identical to current behavior. The frozen-mode fallback (`sys.executable.parent`) provides defense-in-depth. Zero impact on development workflow.

## Auto-Accept Duration

### Current Behavior
- `AutoAcceptScreen` caps input at 24 hours (line 266 in `screens.py`)
- `AutoAcceptState.is_expired()` enforces the duration
- Auto-accept auto-starts at 24h on app mount (line 129 in `app.py`)

### Change
- Raise the manual cap from 24 to 168 hours (1 week) in `AutoAcceptScreen`
- Raise the auto-start duration from 24h to 168h in `app.py` (line 129: `self._start_auto_accept(168.0)`)
- Update validation message accordingly
- No change to `AutoAcceptState` — it already supports arbitrary `timedelta`

## Default Unit Size

### Current
`__main__.py` line 80: `unit_size = int(settings.get("unit_size", 10))`

### Change
Default to `5` for ALL users (not just distributable). This is intentional — 5 is a safer starting point:
```python
unit_size = int(settings.get("unit_size", 5))
```

Existing users who already have `settings.json` with `"unit_size": 20` are unaffected — the default only applies when the key is absent. Since settings persist to `settings.json` on any change, the user's chosen value survives restarts.

## Sports / Non-Sports Toggle

### Behavior
- Default scan mode: **sports** (all 57 leagues)
- Keybinding `m` (for "mode") toggles to non-sports and back
- Status bar shows current mode: `[SPORTS]` or `[NON-SPORTS]`
- Toggle is a **UI-level scan filter only** — it controls what `Scan` (`s` key) discovers
- Games already added from either mode remain active regardless of current toggle
- `add_game()` is unrestricted by mode — manual ticker entry always works for both types

### Implementation

The toggle does NOT modify `GameManager._sports_enabled` or `_nonsports_categories`. Instead:

1. Add `scan_mode: Literal["sports", "nonsports"]` to `TalosApp` state, default `"sports"`
2. Add `scan_mode` parameter to `GameManager.scan_events()`:
   - `"sports"` → only run the sports series discovery path
   - `"nonsports"` → only run the non-sports category discovery path
3. `GameManager` keeps both paths enabled internally — `add_game()` works for any ticker regardless of scan mode
4. Wire `m` keybinding in `TalosApp` to flip `scan_mode` and update status display
5. When user presses `s` (scan), pass `self.scan_mode` to `scan_events()`

This keeps `GameManager` stateless with respect to the toggle — the UI owns the mode, the manager accepts whatever it's told to scan.

## PyInstaller Spec

New file: `talos.spec` (project root)

```python
a = Analysis(
    ["src/talos/__main__.py"],
    pathex=["src"],
    hiddenimports=[
        # Talos modules
        "talos", "talos.ui", "talos.ui.app", "talos.ui.theme",
        "talos.ui.widgets", "talos.ui.screens", "talos.ui.first_run",
        "talos.ui.proposal_panel", "talos.ui.event_review",
        "talos.models", "talos.models.market", "talos.models.order",
        "talos.models.portfolio", "talos.models.strategy", "talos.models.ws",
        # Core modules
        "talos.engine", "talos.scanner", "talos.game_manager",
        "talos.bid_adjuster", "talos.rebalance", "talos.fees",
        "talos.persistence", "talos.orderbook", "talos.rest_client",
        "talos.ws_client", "talos.auth", "talos.config",
        "talos.market_feed", "talos.ticker_feed", "talos.portfolio_feed",
        "talos.position_feed", "talos.lifecycle_feed",
        "talos.top_of_market", "talos.position_ledger",
        "talos.opportunity_proposer", "talos.proposal_queue",
        "talos.auto_accept", "talos.auto_accept_log",
        "talos.suggestion_log", "talos.data_collector",
        "talos.settlement_tracker", "talos.cpm",
        "talos.game_status", "talos.automation_config",
        "talos.errors",
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
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas,
    name="Talos",
    icon="icon.ico",
    console=True,
    onefile=True,
)
```

Key decisions:
- `console=True` — Textual needs a real terminal
- `onefile=True` — single exe for easy distribution
- `a.zipfiles` included — some deps (pydantic, cryptography) may ship as zip-safe packages
- Icon: existing `icon.ico` in project root
- No bundled data files — `.env`, `settings.json`, DBs are created at runtime

**Textual CSS note:** Textual bundles internal `.tcss` files. If the exe fails to render widgets, add `collect_data_files("textual")` to the Analysis `datas` parameter or use `--collect-data textual` on the command line.

## Production-Only Safety

`KalshiConfig.from_env()` defaults to `"demo"` when `KALSHI_ENV` is unset. Since this distributable is production-only, add a guard in `__main__.py` after loading `.env`:

```python
if getattr(sys, "frozen", False) and os.environ.get("KALSHI_ENV") != "production":
    os.environ["KALSHI_ENV"] = "production"
```

This prevents silent fallback to demo if the `.env` is malformed or the line is missing.

## First-Run Error Handling

The SetupScreen validation (step 3: authenticate) should distinguish between:
- **Key file unreadable** — `ValueError` / `cryptography` exception → "Could not read private key file — check the path and format"
- **Auth failed** — HTTP 401/403 → "Authentication failed — check your API key ID"
- **Network error** — connection timeout → "Could not reach Kalshi — check your internet connection"

Surface these as user-friendly messages in the SetupScreen error label, not raw tracebacks.

## Files Changed

| File | Change |
|------|--------|
| `src/talos/persistence.py` | Add `set_data_dir()` / `get_data_dir()`, convert constants to functions |
| `src/talos/__main__.py` | Frozen-mode data dir, `_load_dotenv()` uses `get_data_dir()`, first-run detection, production guard, default unit_size 5 |
| `src/talos/ui/first_run.py` | **New** — `SetupScreen` modal with credential validation and error handling |
| `src/talos/ui/screens.py` | Raise auto-accept cap from 24h to 168h |
| `src/talos/ui/app.py` | Replace `parents[3]` paths with `get_data_dir()`, raise auto-start from 24h to 168h, add sports/non-sports toggle keybinding + status display |
| `src/talos/game_manager.py` | Add `scan_mode` parameter to `scan_events()` |
| `talos.spec` | **New** — PyInstaller build spec |

## Test Coverage

| Test File | What It Covers |
|-----------|---------------|
| `tests/test_persistence_data_dir.py` | `set_data_dir()` / `get_data_dir()` backward compat, frozen-mode resolution |
| `tests/test_first_run.py` | SetupScreen writes correct `.env` and `settings.json` |
| `tests/test_auto_accept_duration.py` | Accepts durations > 24h, rejects > 168h |

Existing test suite must continue passing — no behavioral changes to trading logic.
