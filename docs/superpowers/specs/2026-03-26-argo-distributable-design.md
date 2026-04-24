# Argo — Distributable MLB Props Trading Terminal

## Summary

Argo is a single-file Windows executable that packages the Talos trading engine for MLB player prop bets. It targets a technically savvy friend who gets the exe, provides their own Kalshi API credentials on first launch, and trades "Hits + Runs + RBIs" and "Total Bases" markets using the same Yes/No arbitrage strategy Talos uses for non-sports markets.

## Goals

- Single `Argo.exe` file that runs on any Windows machine without Python installed
- First-run setup wizard collects Kalshi API key ID and private key file path
- Pre-configured to scan only `KXMLBHRR` (Hits + Runs + RBIs) and `KXMLBTB` (Total Bases) series
- Full Talos trading functionality: scanner, rebalance, bid placement, position tracking, settlement history
- Minimal, non-breaking changes to `src/talos/` (configurable data directory only)

## Non-Goals

- Cross-platform builds (Windows only)
- Auto-updates or crash reporting
- Non-sports category scanning
- External API integrations (ESPN, OddsEngine, PandaScore)

## Branding

- **Name:** Argo
- **Icon:** `icons/argo/navigation/iter_02 - Copy.svg` — astrolabe design (stone rings, amber accents, crimson throne marker). Convert to `.ico` for PyInstaller build.
- **Theme:** Inherits Talos Catppuccin Mocha (stone/marble aesthetic)

## Market Structure

Verified via Kalshi API (2026-03-26):

- **Series tickers:** `KXMLBHRR`, `KXMLBTB`
- **Events:** One per game per prop type. Example: `KXMLBHRR-26MAR261615TEXPHI` = "Texas vs Philadelphia: Hits + Runs + RBIs"
- **Markets per event:** 70-85 (one per player per threshold). Example: `KXMLBHRR-26MAR261615TEXPHI-PHIBHARPER3-5` = "Bryce Harper 5+ HRR"
- **Market type:** `mutually_exclusive: false` — standalone Yes/No bets, same paradigm as non-sports
- **Category:** "Sports" (but non-mutually-exclusive, treated as non-sports for trading purposes)

**Key structural fact:** These series (`KXMLBHRR`, `KXMLBTB`) are NOT in Talos's `SPORTS_SERIES` list (only `KXMLBGAME` is). This means `GameManager.add_game()` routes them through the non-sports path, which handles multi-market events via `MarketPickerNeeded`. This is correct behavior — the user picks individual player markets from the 70-85 options per event.

## Architecture

### Package Layout

```
src/argo/
    __init__.py          # Version, app name
    __main__.py          # Entry point: first-run → configure → launch
    config.py            # Series list, app defaults, data directory resolution
    first_run.py         # Textual setup screen for API credentials
```

### Required Talos Changes (Minimal)

One change to `src/talos/persistence.py`: make the data directory configurable.

Currently, multiple files resolve paths from `Path(__file__).resolve().parents[N]`. When PyInstaller bundles the code, `__file__` resolves to the temp extraction directory, not the exe's directory. Fix:

```python
# src/talos/persistence.py — add at module level
_data_dir: Path | None = None

def set_data_dir(path: Path) -> None:
    global _data_dir
    _data_dir = path

def get_data_dir() -> Path:
    return _data_dir or Path(__file__).resolve().parents[2]
```

All files with hardcoded path references must switch to `get_data_dir()`:

| File | Lines | Current | Notes |
|------|-------|---------|-------|
| `src/talos/persistence.py` | 12, 13, 39 | `parents[2]` | games.json, settings.json, games_full.json |
| `src/talos/__main__.py` | 12, 98, 181 | `parents[2]` | .env, talos_data.db, suggestions.log |
| `src/talos/ui/app.py` | 737 | `parents[3]` | talos_data.db (EventReview) — deeper nesting in ui/ |
| `src/talos/ui/app.py` | 1017 | `parents[3]` | auto_accept_sessions/ — also deeper nesting |

This is backward-compatible — when `_data_dir` is None (normal Talos usage), behavior is identical. Argo calls `set_data_dir()` before any other Talos import to redirect all path resolution to the exe's directory.

Also adds a public `get_markets(event_ticker: str) -> list[Market]` method to `KalshiRESTClient` (currently only `get_market` singular exists). This avoids Argo calling the private `_request` method directly during scan.

### Dependency Model

Argo is a thin wrapper that imports and configures Talos components:

```
Argo ──imports──► talos.engine (TradingEngine)
                  talos.scanner (ArbitrageScanner)
                  talos.rest_client (KalshiRESTClient)
                  talos.ws_client (KalshiWSClient)
                  talos.ui.app (TalosApp — subclassed to ArgoApp)
                  talos.ui.screens (all modal screens)
                  talos.ui.widgets (all dashboard widgets)
                  talos.models.* (all data models)
                  talos.persistence (set_data_dir before anything else)
                  talos.fees, talos.rebalance, talos.orderbook, ...
```

### Data Directory

All runtime files live next to the exe (or in the working directory when running from source):

```
Argo.exe
Argo.key              # User's RSA private key (placed by user)
.env                  # Generated by first-run wizard
settings.json         # Unit size, blacklist (auto-created with defaults)
games.json            # Persisted active game tickers
games_full.json       # Full pair data for instant startup
talos_data.db         # Trade history, fills, settlements (SQLite)
suggestions.log       # Audit log of proposals
```

Argo resolves the data directory from the exe location:
```python
import sys
from pathlib import Path

def resolve_data_dir() -> Path:
    if getattr(sys, 'frozen', False):
        # PyInstaller: use exe's directory, not temp extraction dir
        return Path(sys.executable).parent
    else:
        # Dev mode: use working directory
        return Path.cwd()
```

**Security note:** The `.env` file contains Kalshi API credentials in plain text. This is the same approach Talos uses. The recipient should keep the directory private.

### First-Run Setup Flow

```
Launch Argo.exe
    │
    ├─ .env exists? ──yes──► Normal startup
    │
    └─ .env missing? ──► Show SetupScreen
                              │
                              ├─ Collect: KALSHI_KEY_ID (text input)
                              ├─ Collect: KALSHI_PRIVATE_KEY_PATH (file path input)
                              ├─ Select: Environment (Production / Demo)
                              │
                              ├─ Validate: key file exists at given path
                              ├─ Validate: can authenticate with Kalshi API
                              │
                              ├─ Write .env file
                              ├─ Write settings.json with defaults (unit_size=10)
                              │
                              └─ Continue to normal startup
```

The SetupScreen is a Textual Screen, same aesthetic as existing Talos modals (Catppuccin Mocha theme). Unit size defaults to 10 (intentionally lower than Talos's production 20, since this is a new user starting out).

### Scan and Add Workflow

**This is the key design difference from Talos.** MLB prop events have 70-85 markets per event, which doesn't fit Talos's sports path (expects exactly 2 markets). Instead, Argo uses a custom scan flow:

**Scan (keybinding `c`):**
1. Argo overrides the scan action in ArgoApp
2. Calls `rest_client.get_events(series_ticker=series)` directly for each of `KXMLBHRR` and `KXMLBTB`
3. Shows discovered events in a scan results screen (e.g., "TEX vs PHI: Hits + Runs + RBIs — 85 markets")
4. User selects which events to add

**Add from scan result:**
1. For each selected event, calls `rest_client.get_markets(event_ticker=ticker)` (new public method) to fetch all 70-85 player markets
2. Presents the MarketPicker screen (already exists in Talos at `screens.py:811`, supports arbitrary-length market lists with scrollable DataTable, volume-sorting, space/shift-space toggle, and select-all)
3. User selects individual markets to monitor (e.g., "Bryce Harper: 3+", "Corey Seager: 2+")
4. Each selected market is added as a same-ticker YES/NO pair via `GameManager.add_game()` using the non-sports single-market path

**Manual add (keybinding `a`):**
- User can paste a Kalshi URL or event ticker directly
- Routes through existing `GameManager.add_game()` → non-sports path → MarketPickerNeeded → MarketPicker screen

The scan bypasses `GameManager.scan_events()` entirely — Argo implements its own scan as a direct REST call filtered to its two series. The add flow reuses the existing non-sports `add_game()` path, which already handles multi-market events correctly via MarketPickerNeeded.

**ArgoScanScreen (new):** The existing `ScanScreen` at `screens.py:283` assumes 2-market sports events (columns: Spt, Lg, 24h A, 24h B — indexes `active_mkts[0]` and `active_mkts[1]`). Argo needs its own scan results screen with columns appropriate for prop events:

| Column | Source |
|--------|--------|
| Game | `event.sub_title` (e.g., "TEX vs PHI (Mar 26)") |
| Type | Series label from config (e.g., "Hits + Runs + RBIs") |
| Markets | Count of active markets in event |
| Start | Game start time |

User selects events from this screen, then each selected event opens the existing MarketPicker for individual player market selection.

### Startup Sequence (Normal)

```python
# src/argo/__main__.py (simplified)

def main():
    data_dir = resolve_data_dir()

    # Configure Talos to use Argo's data directory
    from talos.persistence import set_data_dir
    set_data_dir(data_dir)

    if not (data_dir / ".env").exists():
        run_first_time_setup(data_dir)

    load_dotenv(data_dir / ".env")
    config = KalshiConfig.from_env()

    # Build engine — duplicates Talos __main__.py wiring but:
    # - Uses Argo's series list (KXMLBHRR, KXMLBTB)
    # - No external API keys
    # - No game_status resolver (set to None)
    # - No non-sports scanning

    auth = KalshiAuth(config.key_id, Path(config.private_key_path))
    rest = KalshiRESTClient(auth, config)
    ws = KalshiWSClient(config)
    # ... (wire scanner, feeds, engine, etc.)

    app = ArgoApp(engine=engine)
    app.run()
```

Note: Talos's `__main__.py` is ~120 lines of inline wiring, not a reusable `build_engine()` function. Argo's `__main__.py` will duplicate this wiring with the Argo-specific configuration. This is intentional — keeping the wiring inline makes each entry point self-contained and avoids coupling their startup sequences.

### ArgoApp Subclass

```python
class ArgoApp(TalosApp):
    TITLE = "Argo"
    SUB_TITLE = "MLB Props"

    async def action_scan(self) -> None:
        """Override scan to use Argo's prop series discovery."""
        # Custom scan: REST call for KXMLBHRR + KXMLBTB events
        # Show results in scan screen
        # Selected events → MarketPicker → add_game()
```

The subclass overrides:
- App title and subtitle
- `action_scan()` — custom prop-series discovery instead of Talos's sports/non-sports scan
- No non-sports scan options (no category configuration)

All other behavior (bid placement, rebalance, proposals, settlement history, event review, keybindings) is inherited unchanged.

### Portfolio Panel Replacement

Talos's `PortfolioPanel` shows cash, locked, exposure, invested, and historical P&L. This is replaced entirely in Argo with a simpler **pair status summary** that gives an at-a-glance view of trading progress.

**ArgoPortfolioPanel display:**

```
Completed: 12
Partial:    3
Open:       8
───────────────────
Tickers:  45
Active:   23
```

**Definitions (all relative to `unit_size` / batch size):**

| Metric | Definition |
|--------|-----------|
| **Completed** | Fully arbed pairs — full batch filled on BOTH YES and NO. Per ticker: `min(yes_qty, no_qty) // unit_size`. Summed across all tickers. |
| **Partial** | Pairs with some fills but not a matched full batch on both sides. Per ticker: after removing completed batches, if any remaining position exists on either side, count the unmatched batches (full or partial). Summed across all tickers. |
| **Open** | Pairs with resting bids on at least one side but ZERO fills on both sides. Summed across all tickers. |
| **Tickers** | Total number of market tickers currently in the opportunities table. |
| **Active** | Number of tickers where we have either a position (fills > 0) OR resting orders. |

**Worked example** (unit_size = 10):

| Ticker | YES filled | NO filled | Resting? | Completed | Partial | Open |
|--------|-----------|----------|----------|-----------|---------|------|
| HARPER-3 | 20 | 10 | — | 1 | 1 | 0 |
| SEAGER-2 | 10 | 10 | — | 1 | 0 | 0 |
| BOHM-1 | 5 | 0 | — | 0 | 1 | 0 |
| TURNER-4 | 0 | 0 | YES bid | 0 | 0 | 1 |
| **Totals** | | | | **2** | **2** | **1** |

For HARPER-3: `min(20, 10) // 10 = 1` completed. Remaining: 10 YES, 0 NO → 1 unmatched batch → 1 partial.
For BOHM-1: `min(5, 0) // 10 = 0` completed. 5 YES remaining → partial fill, not a full batch, but still counts as 1 partial.

**Data sources:** The panel reads from `PositionLedger` (fill counts per ticker per side) and `TradingEngine` (resting order state). It refreshes on every position or order state change, same as the existing panel.

**Implementation:** New `ArgoPortfolioPanel(Static)` widget in `src/argo/widgets.py`. ArgoApp's `compose()` mounts this instead of the Talos `PortfolioPanel`.

### Zero-Fee Configuration

MLB player props are currently fee-free on Kalshi. Argo hardcodes this rather than reading it from the API, so behavior doesn't silently change if Kalshi adds fees later.

**How it works:** The existing fee system is fully parameterized — every `ArbPair` carries `fee_type` and `fee_rate`, propagated through scanner edge calculations, rebalance catch-up pricing, `max_profitable_price()`, and position safety checks. Argo overrides these at pair construction time:

```python
# In Argo's add flow, when constructing ArbPair:
ArbPair(
    ...,
    fee_type="fee_free",   # from argo.config.FEE_TYPE
    fee_rate=0.0,           # from argo.config.FEE_RATE
)
```

The existing `compute_fee()` in `fees.py:34` already returns `0.0` for `fee_type="fee_free"`. No changes to any fee calculation code — this is purely a config override at pair construction.

**Impact on calculations:**
- `fee_adjusted_edge()` → raw edge (no fee deduction)
- `fee_adjusted_cost()` → just the NO price (no fee added)
- `max_profitable_price()` → higher ceiling (no fee drag)
- `scenario_pnl()` → higher per-pair profit
- Scanner opportunity detection → more opportunities surface (lower threshold)

**Future:** When Kalshi adds fees to MLB props, update `FEE_TYPE` and `FEE_RATE` in `argo/config.py` and rebuild.

### Configuration

`src/argo/config.py`:

```python
APP_NAME = "Argo"
APP_SUBTITLE = "MLB Props"

# MLB player prop series — the only markets Argo scans
SERIES = ["KXMLBHRR", "KXMLBTB"]

# Display-friendly names for the scan screen
SERIES_LABELS = {
    "KXMLBHRR": "Hits + Runs + RBIs",
    "KXMLBTB": "Total Bases",
}

DEFAULT_UNIT_SIZE = 10

# MLB props are currently fee-free on Kalshi.
# Hardcode this so Argo ignores any future API fee_type changes.
FEE_TYPE = "fee_free"
FEE_RATE = 0.0
```

## Build

### PyInstaller Spec

`argo.spec` in repo root:

```python
# argo.spec
a = Analysis(
    ['src/argo/__main__.py'],
    pathex=['src'],
    datas=[],          # No data files bundled — all created at runtime
    hiddenimports=[
        'talos', 'talos.ui', 'talos.ui.theme', 'talos.models',
        # Textual internals that PyInstaller misses
        'textual.css', 'textual.widgets',
        # Cryptography for RSA signing
        'cryptography.hazmat.primitives.asymmetric.padding',
    ],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name='Argo',
    icon='icons/argo/argo.ico',
    console=True,       # Textual needs a real console
    onefile=True,
)
```

Note: `console=True` is required because Textual runs in the terminal. The user will see a console window when launching Argo.exe. This is expected behavior for a TUI app.

### Build Command

```bash
pip install pyinstaller
pyinstaller argo.spec
# Output: dist/Argo.exe
```

### pyproject.toml Additions

```toml
[project.scripts]
argo = "argo.__main__:main"

# Add src/argo to hatch build targets
[tool.hatch.build.targets.wheel]
packages = ["src/talos", "src/argo"]
```

## What Talos Has That Argo Inherits (Unchanged)

- Full trading engine (engine.py)
- Arbitrage scanner with edge detection
- Rebalance / catch-up order logic
- Quadratic fee model
- Position ledger and tracking
- Bid adjuster and order management
- WebSocket orderbook, ticker, portfolio feeds
- REST client for all Kalshi endpoints
- Proposal panel with approve/reject
- Settlement history
- Event review screen
- Auto-accept mode
- Unit size configuration
- Market blacklisting
- All keybindings (except scan, which is overridden)

## What Argo Replaces

| Talos Component | Argo Replacement | Reason |
|----------------|-----------------|--------|
| `PortfolioPanel` (cash, locked, exposure, P&L) | `ArgoPortfolioPanel` (completed/partial/open pairs, ticker counts) | Pair status is more actionable than raw account metrics for prop trading |
| `ScanScreen` (2-market sports events) | `ArgoScanScreen` (multi-market prop events) | MLB props have 70-85 markets per event |

## What Argo Removes / Does Not Include

| Feature | Reason |
|---------|--------|
| 80+ sports series list | Replaced by 2 MLB prop series |
| Non-sports category scanning | Not applicable |
| `kalshi_history.db` / `kalshi_nonsports.db` | Discovery databases not needed |
| ESPN / OddsEngine / PandaScore integration | Game status resolution not needed for props |
| External API key configuration | Only Kalshi credentials needed |
| Game status resolver | Props don't need external status tracking |

## Testing Strategy

- Existing Talos test suite continues to pass (data_dir change is backward-compatible)
- New tests for Argo-specific code:
  - `tests/test_argo_config.py` — series list, defaults
  - `tests/test_argo_first_run.py` — setup screen writes correct .env
  - `tests/test_argo_scan.py` — prop series scan discovers events correctly
  - `tests/test_persistence_data_dir.py` — configurable data dir works, default unchanged
- Manual smoke test: build exe, run on clean machine, complete first-run, scan MLB props, select markets, place a bid

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| PyInstaller misses hidden imports (Textual, cryptography) | Test build on clean VM; expand `hiddenimports` as discovered |
| Large exe size (50MB+) | Acceptable for a trading terminal; can use UPX compression if needed |
| Startup latency (3-5s for onefile unpack) | Acceptable; user launches once and runs for hours |
| RSA key path handling on different machines | First-run wizard validates path exists before proceeding |
| Talos internal changes break Argo | Same-repo import; changes are visible immediately in tests |
| Textual CSS not loading in frozen bundle | Include `talos.ui.theme` in hiddenimports; test on frozen build |
| MarketPicker overwhelmed by 70-85 options | Already scrollable; add search/filter if needed post-launch |
| `.env` credentials in plain text next to exe | Standard approach; recipient keeps directory private |
