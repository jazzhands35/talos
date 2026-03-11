# Talos

Kalshi arbitrage trading system — manual-first with progressive automation.

## Tech Stack

- **Language:** Python 3.12+
- **HTTP:** httpx (async)
- **WebSocket:** websockets
- **UI:** Textual (terminal UI)
- **Data models:** Pydantic v2
- **Logging:** structlog
- **Test:** pytest + pytest-asyncio
- **Lint:** ruff
- **Types:** pyright

## Development Commands

```bash
# All commands assume venv is activated: source .venv/Scripts/activate
pip install -e ".[dev]"

# Tests
.venv/Scripts/python -m pytest              # all tests
.venv/Scripts/python -m pytest tests/test_foo.py  # single file
.venv/Scripts/python -m pytest -x           # stop on first failure
.venv/Scripts/python -m pytest -k "test_name"     # run matching tests

# Lint & format
.venv/Scripts/python -m ruff check src/ tests/          # lint
.venv/Scripts/python -m ruff check --fix src/ tests/    # lint + auto-fix
.venv/Scripts/python -m ruff format src/ tests/         # format

# Type check
.venv/Scripts/python -m pyright
```

## Project Structure

- `src/talos/` — main package (all source code lives here)
- `tests/` — pytest test suite, mirrors src structure
- `brain/` — knowledge vault (architecture, decisions, patterns)
- `docs/` — Kalshi API reference specs and implementation plans

## Custom Agents

| Agent | Trigger | What It Does |
|-------|---------|-------------|
| **test-runner** | After writing/modifying code | Runs pytest, summarizes pass/fail |
| **lint-check** | Before commits, after code changes | Runs ruff + pyright |
| **backend-analyst** | Codebase exploration needing 3+ file reads | Traces data flows, explains architecture |

Run both **test-runner** and **lint-check** in parallel before commits. Use **direct Read/Grep** for quick single-file lookups (faster than agents).

## Key Conventions

- **Async-first:** Use `async`/`await` for all I/O (HTTP, WebSocket, file). No blocking calls in the event loop.
- **Pydantic models:** All API responses and domain objects are Pydantic models. No raw dicts for structured data.
- **Structured logging:** Use `structlog` with key-value pairs. Every log line should be machine-parseable.
- **Type everything:** All function signatures must have type annotations. `pyright` must pass clean.
- **src layout:** Imports are `from talos.xxx import yyy`. Never relative imports outside of a single module.
- **Test naming:** `tests/test_{module}.py` mirrors `src/talos/{module}.py`.
- **No secrets in code:** API keys and credentials go in `.env`, loaded via environment variables.

## Trading-Specific Rules

Demo environment by default — production requires explicit opt-in. See `brain/principles.md` for all trading rules.

**Cardinal rule: Kalshi is the single source of truth for positions and resting orders — always, unconditionally, without exception.** Talos must have a 100% accurate picture of what it holds at all times. Every suggestion, safety gate, and action depends on this accuracy. Before any money-touching action, re-fetch from Kalshi. If fresh data is unavailable, do not act. See Principles 7 and 15.

## Current Status

See `brain/architecture.md` for layer completion status.
