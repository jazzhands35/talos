# Data Collection Pipeline Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a write-only SQLite data collector capturing every observable event in Talos for future ML training.

**Architecture:** Single `DataCollector` class with `log_*` methods, one per event type. SQLite DB with 9 tables. Wired via callbacks in `__main__.py`, same pattern as `SuggestionLog`. 10s timer for market snapshots.

**Tech Stack:** Python 3.12+, sqlite3 (stdlib), structlog

**Spec:** `docs/plans/2026-03-15-data-collection-design.md`

---

## Task 1: DataCollector class with schema creation

**Files:**
- Create: `src/talos/data_collector.py`
- Create: `tests/test_data_collector.py`

Create `DataCollector` class that:
- Takes `db_path: Path` in constructor
- Creates all 9 tables with `CREATE TABLE IF NOT EXISTS` on init
- Sets `PRAGMA journal_mode=WAL`
- Has a `close()` method

Tables: `scan_results`, `scan_events`, `game_adds`, `orders`, `fills`, `market_snapshots`, `settlements`, `event_outcomes`

Tests: verify all tables exist after init, verify WAL mode.

## Task 2: log_scan and log_game_add methods

Add to `DataCollector`:
- `log_scan()` — inserts into `scan_results` + `scan_events`
- `log_game_add()` — inserts into `game_adds`

Tests: insert and verify row count + key fields.

## Task 3: log_order and log_fill methods

Add to `DataCollector`:
- `log_order()` — inserts into `orders`
- `log_fill()` — inserts into `fills`

Tests: insert and verify.

## Task 4: log_market_snapshots method

Add to `DataCollector`:
- `log_market_snapshots()` — bulk inserts into `market_snapshots` for all monitored events

Tests: insert multiple snapshots, verify count.

## Task 5: log_settlement and log_event_outcome methods

Add to `DataCollector`:
- `log_settlement()` — inserts into `settlements`
- `log_event_outcome()` — inserts into `event_outcomes` with trap detection

Tests: verify trap detection (balanced=0, imbalanced=1 with correct side/delta).

## Task 6: Wire into engine and app

**Files:**
- Modify: `src/talos/__main__.py`
- Modify: `src/talos/engine.py`
- Modify: `src/talos/ui/app.py`

Wire:
- Instantiate in `__main__.py`, pass to engine
- Engine: log orders on place/update, fills on WS, settlements on lifecycle
- App: log scans, game adds, 10s snapshot timer
- Add `talos_data.db` to `.gitignore`
