# Talos ID Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the runtime-counter `talos_id` with a date-stamped, durably-unique identifier of form `YY.MM.NNN` (e.g. `26.04.188` = 188th game added in April 2026), so that any pair can be referenced by its `#` alone — across sessions, restarts, and months — without screenshots or extra context.

**Architecture:** Store `talos_id` as a 7-digit integer encoding `YYMMNNN` (e.g. `2604188`). Format helpers convert int ↔ `"YY.MM.NNN"` string for display and parsing. A small `talos_id_counter` table in `talos_data.db` persists the per-month sequence so it survives restarts. Existing 139 pairs (all currently `talos_id=0` due to a separate persist bug fixed in this plan) are migrated using true first-seen timestamps from the existing `game_adds` table.

**Tech Stack:** Python 3.12, Pydantic v2, sqlite3, pytest, ruff, pyright (project standards from CLAUDE.md).

**Self-contained background — read before starting:**
- `talos_id` today is an `int` assigned by a runtime counter (`scanner._next_id`) at `src/talos/scanner.py:71`. It's persisted to `games_full.json` via `engine._maybe_save_state` at `src/talos/engine.py:4970`.
- **Pre-existing bug:** every entry in the live `games_full.json` has `talos_id: 0` despite the persist code reading `p.talos_id`. **Root cause:** there are two parallel `ArbPair` instances per game. `game_manager.add_game` constructs one with `talos_id=0` (line 639), stores it in `self._games`, then calls `scanner.add_pair(..., talos_id=0)` which constructs a *second* `ArbPair` with the assigned `_next_id`. Persist iterates `self._game_manager.active_games` — i.e. the first object, which still has `talos_id=0`. Task 4 fixes this; without that fix nothing else in the plan works because the IDs would be wiped on the next save.
- The `#` column in the proposer UI is rendered from `_ColSpec("id", "#", 3, "right", False, "talos_id")` at `src/talos/ui/widgets.py:292`. Width `3` accommodates up to `999`. New format `26.04.188` needs 9 chars.
- The `game_adds` table in `talos_data.db` stamps every event-add with an ISO timestamp `ts`. 114k rows total; we use `MIN(ts) GROUP BY event_ticker` to recover true first-seen.

---

## File Structure

**Create:**
- `src/talos/talos_id.py` — formatting, parsing, counter logic. One module to centralize all `talos_id` knowledge.
- `tests/test_talos_id.py` — unit tests for the helpers.
- `scripts/migrate_talos_ids.py` — one-time migration script that reads `game_adds`, assigns IDs by first-seen order, and rewrites `games_full.json`.
- `tests/test_migrate_talos_ids.py` — tests for the migration logic (in-memory; doesn't touch real DB).

**Modify:**
- `src/talos/data_collector.py` — add `talos_id_counter` table to schema.
- `src/talos/scanner.py` — replace integer `_next_id` counter with call into `talos_id.next_id()`.
- `src/talos/game_manager.py` — fix the persist-zero bug by stamping the assigned ID back onto `self._games[event_ticker]` after `scanner.add_pair`.
- `src/talos/ui/widgets.py` — column width `3` → `9`; render path formats int as `YY.MM.NNN`.
- `src/talos/engine.py` — `_display_name` and the QUEUE log message use the format helper.
- `src/talos/models/strategy.py` — keep `talos_id: int` type, add docstring noting the format encoding.

---

### Task 1: Add `talos_id` format/parse helpers

**Files:**
- Create: `src/talos/talos_id.py`
- Test: `tests/test_talos_id.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_talos_id.py
"""Tests for talos_id formatting and parsing."""

from __future__ import annotations

import pytest

from talos.talos_id import (
    InvalidTalosIdError,
    encode_talos_id,
    format_talos_id,
    parse_talos_id,
)


def test_format_round_numbers() -> None:
    assert format_talos_id(2604188) == "26.04.188"
    assert format_talos_id(2604001) == "26.04.001"
    assert format_talos_id(2612999) == "26.12.999"
    assert format_talos_id(2701001) == "27.01.001"


def test_format_zero_renders_unassigned() -> None:
    # Zero is the "unassigned" sentinel — still possible during migration window.
    assert format_talos_id(0) == "—"


def test_parse_canonical_form() -> None:
    assert parse_talos_id("26.04.188") == 2604188
    assert parse_talos_id("26.04.001") == 2604001


def test_parse_rejects_garbage() -> None:
    for bad in ("", "26", "26.4", "26.04.1", "26.13.001", "26.00.001", "abc"):
        with pytest.raises(InvalidTalosIdError):
            parse_talos_id(bad)


def test_encode_from_components() -> None:
    assert encode_talos_id(year=2026, month=4, seq=188) == 2604188
    assert encode_talos_id(year=2026, month=12, seq=1) == 2612001


def test_encode_rejects_out_of_range() -> None:
    with pytest.raises(InvalidTalosIdError):
        encode_talos_id(year=2026, month=13, seq=1)
    with pytest.raises(InvalidTalosIdError):
        encode_talos_id(year=2026, month=4, seq=1000)
    with pytest.raises(InvalidTalosIdError):
        encode_talos_id(year=2026, month=4, seq=0)


def test_int_form_sorts_chronologically() -> None:
    assert 2604001 < 2604999 < 2605001 < 2701001
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_talos_id.py -v`
Expected: ImportError / ModuleNotFoundError for `talos.talos_id`.

- [ ] **Step 3: Write the minimal module**

```python
# src/talos/talos_id.py
"""Talos ID encoding: YYMMNNN integer ↔ "YY.MM.NNN" string.

Format
------
A talos_id is a 7-digit integer encoding ``YYMMNNN`` where:
- ``YY`` = two-digit year (e.g. 26 for 2026)
- ``MM`` = two-digit month (01-12)
- ``NNN`` = three-digit per-month sequence (001-999), assigned in
  add-order with monthly reset.

Examples: ``2604188`` ⇄ ``"26.04.188"``.

Zero (``0``) is the "unassigned" sentinel; it renders as ``"—"`` and
must never be parsed back via ``parse_talos_id``.

The integer form sorts chronologically by add-time.
"""

from __future__ import annotations

UNASSIGNED_DISPLAY = "—"


class InvalidTalosIdError(ValueError):
    """Raised when a talos_id value or string is malformed."""


def encode_talos_id(*, year: int, month: int, seq: int) -> int:
    """Pack (year, month, seq) into a 7-digit talos_id."""
    if not 0 <= year <= 99:
        raise InvalidTalosIdError(f"year must be 0-99, got {year}")
    if not 1 <= month <= 12:
        raise InvalidTalosIdError(f"month must be 1-12, got {month}")
    if not 1 <= seq <= 999:
        raise InvalidTalosIdError(f"seq must be 1-999, got {seq}")
    return year * 100_000 + month * 1_000 + seq


def format_talos_id(value: int) -> str:
    """Render ``value`` as ``"YY.MM.NNN"``; ``0`` renders as ``"—"``."""
    if value == 0:
        return UNASSIGNED_DISPLAY
    yy = value // 100_000
    mm = (value // 1_000) % 100
    nnn = value % 1_000
    return f"{yy:02d}.{mm:02d}.{nnn:03d}"


def parse_talos_id(text: str) -> int:
    """Parse ``"YY.MM.NNN"`` back into the integer form. Strict."""
    parts = text.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise InvalidTalosIdError(f"not in YY.MM.NNN form: {text!r}")
    if (len(parts[0]), len(parts[1]), len(parts[2])) != (2, 2, 3):
        raise InvalidTalosIdError(f"part widths must be 2/2/3: {text!r}")
    yy, mm, nnn = (int(p) for p in parts)
    return encode_talos_id(year=yy, month=mm, seq=nnn)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_talos_id.py -v`
Expected: 7 passed.

- [ ] **Step 5: Lint & type-check**

Run: `.venv/Scripts/python -m ruff check src/talos/talos_id.py tests/test_talos_id.py && .venv/Scripts/python -m pyright src/talos/talos_id.py tests/test_talos_id.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/talos/talos_id.py tests/test_talos_id.py
git commit -m "feat(talos_id): add YY.MM.NNN encoding helpers"
```

---

### Task 2: Add the `talos_id_counter` table

**Files:**
- Modify: `src/talos/data_collector.py:50` (add new CREATE TABLE next to `game_adds`)
- Modify: `src/talos/data_collector.py:267` (extend whitelist if it gates table creation)

- [ ] **Step 1: Read the current schema to find the right insertion point**

Open `src/talos/data_collector.py` and locate the block of `CREATE TABLE IF NOT EXISTS` statements (starts around line 20). The new table goes alongside the others — the convention is one schema block executed at startup.

- [ ] **Step 2: Write the failing test**

```python
# Append to tests/test_talos_id.py

import sqlite3

from talos.talos_id import bump_seq, peek_seq, ensure_counter_schema


def test_counter_starts_empty() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_counter_schema(conn)
    assert peek_seq(conn, year=2026, month=4) == 0


def test_bump_seq_returns_next_value() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_counter_schema(conn)
    assert bump_seq(conn, year=2026, month=4) == 1
    assert bump_seq(conn, year=2026, month=4) == 2
    assert bump_seq(conn, year=2026, month=4) == 3


def test_bump_seq_resets_per_month() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_counter_schema(conn)
    assert bump_seq(conn, year=2026, month=4) == 1
    assert bump_seq(conn, year=2026, month=4) == 2
    assert bump_seq(conn, year=2026, month=5) == 1
    assert bump_seq(conn, year=2026, month=4) == 3  # April resumes


def test_bump_seq_persists_across_connections() -> None:
    import tempfile
    import pathlib
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "test.db"
        c1 = sqlite3.connect(path)
        ensure_counter_schema(c1)
        assert bump_seq(c1, year=2026, month=4) == 1
        assert bump_seq(c1, year=2026, month=4) == 2
        c1.close()
        c2 = sqlite3.connect(path)
        ensure_counter_schema(c2)
        assert bump_seq(c2, year=2026, month=4) == 3


def test_bump_seq_overflow_raises() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_counter_schema(conn)
    # Fast-forward to 999 then bump once more
    conn.execute(
        "INSERT OR REPLACE INTO talos_id_counter(year_month, last_seq) VALUES (?, ?)",
        (2026 * 100 + 4, 999),
    )
    conn.commit()
    from talos.talos_id import InvalidTalosIdError
    with pytest.raises(InvalidTalosIdError):
        bump_seq(conn, year=2026, month=4)
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_talos_id.py -v -k counter`
Expected: ImportError on `bump_seq`/`peek_seq`/`ensure_counter_schema`.

- [ ] **Step 4: Add the counter functions to `src/talos/talos_id.py`**

Append to the module:

```python
import sqlite3

_COUNTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS talos_id_counter (
    year_month INTEGER PRIMARY KEY,  -- YYYY*100 + MM, e.g. 202604
    last_seq INTEGER NOT NULL
);
"""


def ensure_counter_schema(conn: sqlite3.Connection) -> None:
    """Create the counter table if it doesn't exist. Idempotent."""
    conn.execute(_COUNTER_SCHEMA)
    conn.commit()


def _year_month_key(year: int, month: int) -> int:
    if not 1 <= month <= 12:
        raise InvalidTalosIdError(f"month must be 1-12, got {month}")
    return year * 100 + month


def peek_seq(conn: sqlite3.Connection, *, year: int, month: int) -> int:
    """Return the current ``last_seq`` for the given month, or 0 if none."""
    row = conn.execute(
        "SELECT last_seq FROM talos_id_counter WHERE year_month = ?",
        (_year_month_key(year, month),),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def bump_seq(conn: sqlite3.Connection, *, year: int, month: int) -> int:
    """Atomically increment and return the next seq for the given month.

    Raises ``InvalidTalosIdError`` if the seq would exceed 999.
    """
    key = _year_month_key(year, month)
    cur = peek_seq(conn, year=year, month=month)
    nxt = cur + 1
    if nxt > 999:
        raise InvalidTalosIdError(
            f"seq exhausted for {year:04d}-{month:02d} (>999 adds)"
        )
    conn.execute(
        "INSERT OR REPLACE INTO talos_id_counter(year_month, last_seq) VALUES (?, ?)",
        (key, nxt),
    )
    conn.commit()
    return nxt
```

- [ ] **Step 5: Wire the schema into `data_collector.py`**

Open `src/talos/data_collector.py`. Find the multi-table SQL block that starts around line 20 (where `scan_results`, `scan_events`, `game_adds`, etc. are declared). Append, before the trailing closing quote:

```sql
CREATE TABLE IF NOT EXISTS talos_id_counter (
    year_month INTEGER PRIMARY KEY,
    last_seq INTEGER NOT NULL
);
```

Then check around line 267 for any explicit table whitelist. If the file has a `_TABLES = (...)` or similar tuple/list used for `CREATE` calls, add `"talos_id_counter"` there. (If the schema is a single multi-statement string passed to `executescript`, no list change is needed.)

- [ ] **Step 6: Run all the new tests**

Run: `.venv/Scripts/python -m pytest tests/test_talos_id.py -v`
Expected: all 12 tests pass.

- [ ] **Step 7: Lint & type-check**

Run: `.venv/Scripts/python -m ruff check src/talos/talos_id.py src/talos/data_collector.py tests/test_talos_id.py && .venv/Scripts/python -m pyright src/talos/talos_id.py`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/talos/talos_id.py src/talos/data_collector.py tests/test_talos_id.py
git commit -m "feat(talos_id): persistent monthly counter in talos_data.db"
```

---

### Task 3: `next_id` — full assignment helper using the counter

**Files:**
- Modify: `src/talos/talos_id.py`
- Modify: `tests/test_talos_id.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_talos_id.py

from datetime import datetime
from zoneinfo import ZoneInfo

from talos.talos_id import next_id


def test_next_id_assigns_for_current_local_month() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_counter_schema(conn)
    now = datetime(2026, 4, 28, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert next_id(conn, now=now) == 2604001
    assert next_id(conn, now=now) == 2604002


def test_next_id_uses_local_time_for_month_boundary() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_counter_schema(conn)
    # 23:30 local on April 30 — local month is still April, even though UTC is May.
    late_april_local = datetime(
        2026, 4, 30, 23, 30, tzinfo=ZoneInfo("America/Los_Angeles")
    )
    assert next_id(conn, now=late_april_local) == 2604001
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_talos_id.py -v -k next_id`
Expected: ImportError on `next_id`.

- [ ] **Step 3: Implement `next_id`**

Append to `src/talos/talos_id.py`:

```python
from datetime import datetime
from zoneinfo import ZoneInfo


_LOCAL_TZ = ZoneInfo("America/Los_Angeles")  # User's local timezone (Pacific).


def next_id(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """Assign the next ``talos_id`` for the current local month.

    ``now`` defaults to the current local time; pass an aware datetime to test
    month-boundary behavior. Local time (not UTC) determines the month so that
    a game added at 23:30 PT on April 30 is `26.04.NNN`, not `26.05.NNN`.
    """
    moment = now if now is not None else datetime.now(_LOCAL_TZ)
    if moment.tzinfo is None:
        raise InvalidTalosIdError("now must be timezone-aware")
    local = moment.astimezone(_LOCAL_TZ)
    seq = bump_seq(conn, year=local.year, month=local.month)
    return encode_talos_id(year=local.year % 100, month=local.month, seq=seq)
```

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_talos_id.py -v`
Expected: all 14 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/talos/talos_id.py tests/test_talos_id.py
git commit -m "feat(talos_id): next_id helper with local-month boundaries"
```

---

### Task 4: Fix the persist-zero bug

**Files:**
- Modify: `src/talos/scanner.py` (return assigned ID from `add_pair`)
- Modify: `src/talos/game_manager.py:665-677` (stamp the returned ID back onto `self._games[event_ticker].talos_id`)
- Test: `tests/test_persist_talos_id.py`

**Why this comes before wiring `next_id`:** the bug means any new IDs would be wiped on the next `_maybe_save_state` call. We need `game_manager._games[event_ticker].talos_id` to reflect the actually-assigned ID before changing how IDs are assigned.

- [ ] **Step 1: Write the failing regression test**

```python
# tests/test_persist_talos_id.py
"""Regression: assigned talos_id must round-trip through persistence."""

from __future__ import annotations

# Use the existing test helpers/fixtures the project already has for engine + game_manager.
# Replace the imports below with whatever the project's existing test files import
# (look at tests/test_game_manager.py or tests/test_engine.py for the canonical pattern).
import pytest

from talos.game_manager import GameManager
from talos.scanner import Scanner


def test_added_pair_has_nonzero_talos_id_in_game_manager() -> None:
    """When game_manager.add_game registers a pair, the ArbPair stored in
    self._games must carry the talos_id assigned by the scanner — NOT 0."""
    scanner = Scanner()
    gm = GameManager(scanner=scanner)
    # Use whatever the project's add-by-data path looks like; if there's a
    # different helper used in the existing tests, mirror that.
    data = {
        "event_ticker": "TEST-EVENT-1",
        "ticker_a": "TEST-EVENT-1-A",
        "ticker_b": "TEST-EVENT-1-B",
        "close_time": "2026-12-31T23:59:00Z",
        "fee_type": "quadratic_with_maker_fees",
        "fee_rate": 0.0175,
        "talos_id": 0,  # legacy / unassigned
    }
    pair = gm._restore_from_dict(data)  # or whichever helper matches add_game's data path
    assert pair is not None
    assert pair.talos_id != 0, (
        "game_manager._games entry must reflect scanner's assigned id"
    )
    # And the scanner's view should match
    assert scanner.get_talos_id("TEST-EVENT-1") == pair.talos_id
```

> **Note for executor:** the exact helper name on `GameManager` may differ — search for the entry point that game_manager uses to ingest a record dict (look near `add_game`, `_restore_from_dict`, or wherever line 615-688 of `game_manager.py` is reached). Adjust the test to call that path.

- [ ] **Step 2: Run to verify the test fails (proves the bug)**

Run: `.venv/Scripts/python -m pytest tests/test_persist_talos_id.py -v`
Expected: FAIL — `pair.talos_id == 0`.

- [ ] **Step 3: Change `Scanner.add_pair` to return the assigned ID**

Open `src/talos/scanner.py`. The current signature ends `-> None`. Change it to return `int` (the assigned `talos_id`):

```python
def add_pair(
    self,
    event_ticker: str,
    ticker_a: str,
    ticker_b: str,
    *,
    fee_type: str = "quadratic_with_maker_fees",
    fee_rate: float = 0.0175,
    close_time: str | None = None,
    expected_expiration_time: str | None = None,
    side_a: str = "no",
    side_b: str = "no",
    kalshi_event_ticker: str = "",
    talos_id: int = 0,
    fractional_trading_enabled: bool = False,
    tick_bps: int = 100,
) -> int:
    """Register a pair of markets to monitor. Returns the assigned talos_id."""
    existing = next((p for p in self._pairs if p.event_ticker == event_ticker), None)
    if existing is not None:
        return existing.talos_id  # idempotent — return the existing id
    assigned_id = talos_id if talos_id > 0 else self._next_id
    self._next_id = max(self._next_id, assigned_id + 1)
    pair = ArbPair(
        talos_id=assigned_id,
        # ... (rest unchanged)
    )
    # ... (rest of body unchanged)
    return assigned_id
```

> Two changes only: signature `-> int`, the early-return-when-duplicate now returns the existing id, and the function returns `assigned_id` at the end.

- [ ] **Step 4: Update `game_manager._restore_from_dict` (or equivalent) to stamp the ID back**

Open `src/talos/game_manager.py` around line 665. Change:

```python
self._scanner.add_pair(
    event_ticker,
    ticker_a,
    ticker_b,
    side_a=side_a,
    side_b=side_b,
    kalshi_event_ticker=kalshi_event_ticker,
    fee_type=pair.fee_type,
    fee_rate=pair.fee_rate,
    close_time=pair.close_time,
    expected_expiration_time=pair.expected_expiration_time,
    talos_id=talos_id,
)
self._games[event_ticker] = pair
```

to:

```python
assigned_id = self._scanner.add_pair(
    event_ticker,
    ticker_a,
    ticker_b,
    side_a=side_a,
    side_b=side_b,
    kalshi_event_ticker=kalshi_event_ticker,
    fee_type=pair.fee_type,
    fee_rate=pair.fee_rate,
    close_time=pair.close_time,
    expected_expiration_time=pair.expected_expiration_time,
    talos_id=talos_id,
)
# Re-stamp so self._games carries the actually-assigned id (was the
# persist-zero bug: scanner had the id, game_manager's parallel ArbPair
# kept talos_id=0).
pair = pair.model_copy(update={"talos_id": assigned_id})
self._games[event_ticker] = pair
```

> **Why model_copy:** `ArbPair` is a Pydantic v2 BaseModel; mutating `pair.talos_id = ...` is allowed by default but `model_copy(update=...)` is the idiomatic pattern in this codebase. If a search of `model_copy` in the existing source shows it's used routinely, keep this form; if direct attribute mutation is the pattern elsewhere, mirror that instead.

- [ ] **Step 5: Search for any other call site of `scanner.add_pair` that ignores the return value**

Run: `grep -rn "scanner.add_pair\|self._scanner.add_pair" src/ tests/`
For each hit, verify the caller doesn't need the returned id. If it's another path that creates a parallel ArbPair, apply the same fix. Likely candidates: `engine.py` startup paths, any restore helper.

- [ ] **Step 6: Run the regression test + full suite**

Run: `.venv/Scripts/python -m pytest tests/test_persist_talos_id.py -v`
Expected: PASS.

Run: `.venv/Scripts/python -m pytest`
Expected: all existing tests still pass (the signature change from `-> None` to `-> int` is backward-compatible at call sites that ignore the return).

- [ ] **Step 7: Lint & type-check**

Run: `.venv/Scripts/python -m ruff check src/talos/scanner.py src/talos/game_manager.py tests/test_persist_talos_id.py && .venv/Scripts/python -m pyright`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/talos/scanner.py src/talos/game_manager.py tests/test_persist_talos_id.py
git commit -m "fix(persist): stamp assigned talos_id back onto game_manager pair

Resolves the persist-zero bug: scanner.add_pair now returns the assigned id,
and game_manager re-stamps it onto its parallel ArbPair so the value
persisted to games_full.json matches the live scanner state."
```

---

### Task 5: Wire `scanner` to use the new persistent counter

**Files:**
- Modify: `src/talos/scanner.py:71-72` (replace `_next_id` with `talos_id.next_id(conn)`)
- Modify: `src/talos/scanner.py:__init__` (accept a sqlite connection or factory)
- Modify: callers that construct `Scanner` (search for `Scanner(`).

- [ ] **Step 1: Find all `Scanner(` construction sites**

Run: `grep -rn "Scanner(" src/ tests/`
Note them. The new constructor needs a way to obtain a sqlite connection to `talos_data.db`.

- [ ] **Step 2: Decide how the connection is supplied**

Two reasonable shapes:
- **(a)** `Scanner(..., id_db: sqlite3.Connection | None = None)` — pass the existing `data_collector` connection in.
- **(b)** `Scanner(..., id_assigner: Callable[[], int] | None = None)` — inject a callable, default to a no-op that uses the in-memory `_next_id` if no DB. Maximally test-friendly.

Use **(b)**. It keeps Scanner's tests independent of sqlite, and production code passes a real assigner that wraps `talos_id.next_id(conn)`.

- [ ] **Step 3: Write the failing tests**

```python
# Append to tests/test_scanner.py (or wherever Scanner tests live)

from talos.scanner import Scanner


def test_scanner_uses_injected_id_assigner() -> None:
    seq = iter([2604001, 2604002, 2604003])
    scanner = Scanner(id_assigner=lambda: next(seq))
    scanner.add_pair("E1", "E1-A", "E1-B")
    scanner.add_pair("E2", "E2-A", "E2-B")
    assert scanner.get_talos_id("E1") == 2604001
    assert scanner.get_talos_id("E2") == 2604002


def test_scanner_falls_back_to_legacy_counter_without_assigner() -> None:
    """Backward-compat: existing tests that don't pass id_assigner still work."""
    scanner = Scanner()
    assigned = scanner.add_pair("E1", "E1-A", "E1-B")
    assert assigned > 0  # any positive int is fine for legacy fallback
```

- [ ] **Step 4: Run tests to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_scanner.py -v -k id_assigner`
Expected: FAIL (`Scanner.__init__` doesn't accept `id_assigner`).

- [ ] **Step 5: Update `Scanner.__init__` and `add_pair`**

In `src/talos/scanner.py`:

```python
from collections.abc import Callable

class Scanner:
    def __init__(
        self,
        # ... existing params ...
        id_assigner: Callable[[], int] | None = None,
    ) -> None:
        # ... existing init ...
        self._id_assigner = id_assigner
        self._next_id = 1  # legacy fallback only

    def add_pair(self, ...) -> int:
        existing = next((p for p in self._pairs if p.event_ticker == event_ticker), None)
        if existing is not None:
            return existing.talos_id
        if talos_id > 0:
            assigned_id = talos_id
        elif self._id_assigner is not None:
            assigned_id = self._id_assigner()
        else:
            assigned_id = self._next_id
        self._next_id = max(self._next_id, assigned_id + 1)
        # ... rest unchanged ...
```

- [ ] **Step 6: Wire production callers to pass the real assigner**

Find where `Scanner(` is constructed in production (likely `src/talos/engine.py` or `src/talos/__main__.py`). At that site, also obtain the existing `talos_data.db` connection used by `data_collector` — there should be one. Pass:

```python
import sqlite3
from talos.talos_id import next_id, ensure_counter_schema

conn = sqlite3.connect("talos_data.db")  # or reuse the existing connection
ensure_counter_schema(conn)
scanner = Scanner(..., id_assigner=lambda: next_id(conn))
```

> **Important:** if there's already a single connection used elsewhere (`data_collector` likely owns one), reuse it rather than opening a second one. Search for `sqlite3.connect("talos_data.db")` to find the canonical site.

- [ ] **Step 7: Run scanner tests + full suite**

Run: `.venv/Scripts/python -m pytest tests/test_scanner.py -v`
Expected: new tests pass.

Run: `.venv/Scripts/python -m pytest`
Expected: full suite green.

- [ ] **Step 8: Commit**

```bash
git add src/talos/scanner.py tests/test_scanner.py src/talos/engine.py  # or __main__.py
git commit -m "feat(scanner): use persistent monthly id_assigner"
```

---

### Task 6: Update display formatters everywhere

**Files:**
- Modify: `src/talos/ui/widgets.py:292` (column width 3 → 9)
- Modify: `src/talos/ui/widgets.py:560-561` (render path — pass through `format_talos_id`)
- Modify: `src/talos/ui/widgets.py:730` (the other render path)
- Modify: `src/talos/engine.py:362-364` (`_display_name`)
- Modify: `src/talos/engine.py:3335` (`QUEUE` log)

- [ ] **Step 1: Read each call site to understand the local code**

Open each file at the listed lines. The pattern at each call site is `f"#{tid}"` or returning the int directly. Each needs to call `format_talos_id(tid)` instead.

- [ ] **Step 2: Write the failing test for the UI render path**

```python
# tests/test_ui_widgets.py — add to existing or create new

from talos.ui.widgets import _ColSpec, _format_cell  # adjust to actual export

def test_id_column_renders_yy_mm_nnn() -> None:
    # Whichever helper widgets.py uses to convert the raw int to display string;
    # if there isn't one extracted, add a small format helper that the test can call.
    assert _format_id_cell(2604188) == "26.04.188"
    assert _format_id_cell(0) == "—"
```

> If no clean seam exists, add a small `_format_id_cell(value: int) -> str` helper in `widgets.py` that just calls `format_talos_id` — that's the unit you test.

- [ ] **Step 3: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_ui_widgets.py -v -k id_column`
Expected: FAIL or ImportError.

- [ ] **Step 4: Update `widgets.py`**

At line 292, change column width:
```python
_ColSpec("id", "#", 9, "right", False, "talos_id"),  # was width=3
```

At lines 560-561 (the render path that returns the value for `key_name == "talos_id"`), wrap the return with `format_talos_id`:

```python
from talos.talos_id import format_talos_id

# in the render dispatch:
if key_name == "talos_id":
    return format_talos_id(self._talos_ids.get(opp.event_ticker, 0))
```

Same change at line 730.

- [ ] **Step 5: Update `engine._display_name` (line 362-364)**

```python
from talos.talos_id import format_talos_id

def _display_name(self, event_ticker: str) -> str:
    """Resolve event ticker to short human-readable label with Talos ID prefix."""
    tid = self._scanner.get_talos_id(event_ticker)
    label = self._game_manager.labels.get(event_ticker, event_ticker)
    return f"#{format_talos_id(tid)} {label}" if tid else label
```

- [ ] **Step 6: Update QUEUE log (line 3335)**

```python
summary = (
    f"QUEUE: #{format_talos_id(pair.talos_id)} {name} "
    f"{resting_price}c → {improved_price}c "
    f"(queue {queue_k}, ETA {eta_str}, game in {tr_str})"
)
```

- [ ] **Step 7: Search for any other uses of `talos_id` in f-strings**

Run: `grep -rn 'f".*talos_id\|f".*p\.talos_id\|f".*pair\.talos_id\|f"#{tid' src/`
For each hit, wrap with `format_talos_id`.

- [ ] **Step 8: Run UI tests + full suite**

Run: `.venv/Scripts/python -m pytest tests/test_ui_widgets.py -v && .venv/Scripts/python -m pytest`
Expected: all green.

- [ ] **Step 9: Lint & type-check**

Run: `.venv/Scripts/python -m ruff check src/ tests/ && .venv/Scripts/python -m pyright`
Expected: clean.

- [ ] **Step 10: Commit**

```bash
git add src/talos/ui/widgets.py src/talos/engine.py tests/test_ui_widgets.py
git commit -m "feat(ui): render talos_id as YY.MM.NNN throughout"
```

---

### Task 7: Migration script — backfill IDs from `game_adds`

**Files:**
- Create: `scripts/migrate_talos_ids.py`
- Test: `tests/test_migrate_talos_ids.py`

**Approach:** for each `event_ticker` currently in `games_full.json` with `talos_id == 0`, look up `MIN(ts)` in `game_adds`, group by `(year, month)`, assign sequential 001..NNN per month in chronological order, and write back. Also bump the persistent `talos_id_counter` so post-migration assignments don't collide.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migrate_talos_ids.py
"""Test the migration logic with an in-memory sqlite + synthetic JSON."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from talos.talos_id import ensure_counter_schema, peek_seq
from scripts.migrate_talos_ids import migrate


def _seed_game_adds(conn: sqlite3.Connection, rows: list[tuple[str, str]]) -> None:
    conn.execute(
        "CREATE TABLE game_adds (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts TEXT NOT NULL, event_ticker TEXT)"
    )
    for ts, ticker in rows:
        conn.execute(
            "INSERT INTO game_adds(ts, event_ticker) VALUES (?, ?)", (ts, ticker)
        )
    conn.commit()


def test_migrate_assigns_chronological_ids(tmp_path: Path) -> None:
    db = sqlite3.connect(":memory:")
    ensure_counter_schema(db)
    _seed_game_adds(db, [
        ("2026-04-10T12:00:00+00:00", "EVT-A"),
        ("2026-04-15T12:00:00+00:00", "EVT-B"),
        ("2026-04-15T13:00:00+00:00", "EVT-A"),  # duplicate add — earlier wins
        ("2026-04-20T12:00:00+00:00", "EVT-C"),
    ])
    games_json = tmp_path / "games_full.json"
    games_json.write_text(json.dumps({
        "schema_version": 1,
        "games": [
            {"event_ticker": "EVT-A", "talos_id": 0, "ticker_a": "x", "ticker_b": "y"},
            {"event_ticker": "EVT-B", "talos_id": 0, "ticker_a": "x", "ticker_b": "y"},
            {"event_ticker": "EVT-C", "talos_id": 0, "ticker_a": "x", "ticker_b": "y"},
        ],
    }))

    migrate(db=db, games_path=games_json)

    after = json.loads(games_json.read_text())
    by_ticker = {g["event_ticker"]: g["talos_id"] for g in after["games"]}
    assert by_ticker["EVT-A"] == 2604001  # earliest add
    assert by_ticker["EVT-B"] == 2604002
    assert by_ticker["EVT-C"] == 2604003
    # Counter is bumped so post-migration adds start at 004
    assert peek_seq(db, year=2026, month=4) == 3


def test_migrate_skips_already_assigned(tmp_path: Path) -> None:
    db = sqlite3.connect(":memory:")
    ensure_counter_schema(db)
    _seed_game_adds(db, [("2026-04-10T12:00:00+00:00", "EVT-A")])
    games_json = tmp_path / "games_full.json"
    games_json.write_text(json.dumps({
        "schema_version": 1,
        "games": [
            {"event_ticker": "EVT-A", "talos_id": 2604042, "ticker_a": "x", "ticker_b": "y"},
        ],
    }))
    migrate(db=db, games_path=games_json)
    after = json.loads(games_json.read_text())
    assert after["games"][0]["talos_id"] == 2604042  # untouched


def test_migrate_handles_pair_not_in_game_adds(tmp_path: Path) -> None:
    """If an event_ticker has no row in game_adds, fall back to current local month."""
    db = sqlite3.connect(":memory:")
    ensure_counter_schema(db)
    _seed_game_adds(db, [])  # empty
    games_json = tmp_path / "games_full.json"
    games_json.write_text(json.dumps({
        "schema_version": 1,
        "games": [
            {"event_ticker": "ORPHAN", "talos_id": 0, "ticker_a": "x", "ticker_b": "y"},
        ],
    }))
    migrate(db=db, games_path=games_json)
    after = json.loads(games_json.read_text())
    assert after["games"][0]["talos_id"] != 0  # got *something* current-month-ish
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_migrate_talos_ids.py -v`
Expected: ImportError.

- [ ] **Step 3: Write the migration script**

```python
# scripts/migrate_talos_ids.py
"""One-time migration: backfill talos_id for existing pairs in games_full.json.

Strategy: for each event_ticker with talos_id==0, look up MIN(ts) in game_adds,
sort all such pairs chronologically, then assign per-month sequential ids
(YY.MM.NNN) and bump the talos_id_counter so post-migration assignments don't
collide.

Pairs without any game_adds row (rare) get assigned in current local month
after all the dated ones.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from talos.talos_id import (
    bump_seq,
    encode_talos_id,
    ensure_counter_schema,
)

_LOCAL_TZ = ZoneInfo("America/Los_Angeles")


def _first_seen_for_tickers(
    conn: sqlite3.Connection, tickers: list[str]
) -> dict[str, datetime]:
    """Return {event_ticker: earliest ts as aware datetime} for given tickers."""
    if not tickers:
        return {}
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"SELECT event_ticker, MIN(ts) FROM game_adds "
        f"WHERE event_ticker IN ({placeholders}) GROUP BY event_ticker",
        tickers,
    ).fetchall()
    out: dict[str, datetime] = {}
    for ticker, ts in rows:
        if ts is None:
            continue
        # game_adds stores ISO format with timezone; parse as aware
        out[ticker] = datetime.fromisoformat(ts)
    return out


def migrate(*, db: sqlite3.Connection, games_path: Path) -> None:
    """Run the migration. Idempotent — pairs with talos_id != 0 are left alone."""
    ensure_counter_schema(db)
    payload = json.loads(games_path.read_text())
    games = payload["games"]

    needs_migration = [g for g in games if int(g.get("talos_id", 0)) == 0]
    if not needs_migration:
        print("All pairs already have talos_id assigned. No migration needed.")
        return

    tickers = [g["event_ticker"] for g in needs_migration]
    first_seen = _first_seen_for_tickers(db, tickers)

    now_local = datetime.now(_LOCAL_TZ)
    fallback_dt = now_local  # for orphans with no game_adds row

    # Sort: dated pairs by their first-seen timestamp; orphans last (in payload order).
    def _sort_key(g: dict) -> tuple[int, datetime]:
        ts = first_seen.get(g["event_ticker"])
        return (0, ts) if ts is not None else (1, fallback_dt)

    needs_migration.sort(key=_sort_key)

    assignments: dict[str, int] = {}
    for g in needs_migration:
        ticker = g["event_ticker"]
        ts = first_seen.get(ticker, fallback_dt).astimezone(_LOCAL_TZ)
        seq = bump_seq(db, year=ts.year, month=ts.month)
        assignments[ticker] = encode_talos_id(
            year=ts.year % 100, month=ts.month, seq=seq
        )

    # Apply back to payload
    for g in games:
        if int(g.get("talos_id", 0)) == 0 and g["event_ticker"] in assignments:
            g["talos_id"] = assignments[g["event_ticker"]]

    games_path.write_text(json.dumps(payload, indent=2))
    print(f"Migrated {len(assignments)} pairs.")
    for ticker, tid in sorted(assignments.items(), key=lambda x: x[1]):
        from talos.talos_id import format_talos_id
        print(f"  {format_talos_id(tid)}  {ticker}")


def _main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="talos_data.db")
    p.add_argument("--games", default="games_full.json")
    args = p.parse_args()
    db = sqlite3.connect(args.db)
    migrate(db=db, games_path=Path(args.games))


if __name__ == "__main__":
    _main()
```

- [ ] **Step 4: Run the tests**

Run: `.venv/Scripts/python -m pytest tests/test_migrate_talos_ids.py -v`
Expected: 3 tests pass.

- [ ] **Step 5: Lint & type-check**

Run: `.venv/Scripts/python -m ruff check scripts/migrate_talos_ids.py tests/test_migrate_talos_ids.py && .venv/Scripts/python -m pyright scripts/migrate_talos_ids.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add scripts/migrate_talos_ids.py tests/test_migrate_talos_ids.py
git commit -m "feat(migration): backfill talos_ids from game_adds first-seen"
```

---

### Task 8: Run the migration on real data and verify

**Files:**
- Modify (one-off): `talos_data.db`, `games_full.json` (in-place migration)

**Pre-flight:** Talos must be **stopped** before running this. The migration rewrites `games_full.json`; a running Talos process will overwrite the rewrite on its next save tick.

- [ ] **Step 1: Confirm Talos is not running**

Run (Bash): `tasklist | grep -i talos || echo "no talos process"`
Or check Task Manager. If running, stop it cleanly first (don't kill mid-write — could corrupt `games_full.json`).

- [ ] **Step 2: Back up the files**

```bash
cp games_full.json games_full.json.bak.before-talos-id-migration
cp talos_data.db talos_data.db.bak.before-talos-id-migration
```

- [ ] **Step 3: Run the migration**

Run: `.venv/Scripts/python scripts/migrate_talos_ids.py --db talos_data.db --games games_full.json`
Expected output: `Migrated 139 pairs.` followed by a sorted list of `26.MM.NNN  EVT-TICKER`.

- [ ] **Step 4: Spot-check a handful of assignments**

Run:
```bash
.venv/Scripts/python -c "
import json
d = json.load(open('games_full.json'))
ids = [(g['talos_id'], g['event_ticker']) for g in d['games']]
ids.sort()
print('first 5:', ids[:5])
print('last 5:', ids[-5:])
print('zeros remaining:', sum(1 for tid, _ in ids if tid == 0))
"
```
Expected: zeros remaining = 0 (or only count of pairs with no game_adds history). First entries should be from the earliest months in `game_adds` (likely March 2026 based on sample row `2026-03-15` we saw).

- [ ] **Step 5: Verify counter state**

Run:
```bash
.venv/Scripts/python -c "
import sqlite3
con = sqlite3.connect('talos_data.db')
for row in con.execute('SELECT * FROM talos_id_counter ORDER BY year_month'):
    print(row)
"
```
Expected: rows for each (year, month) that had migrations, with `last_seq` matching the highest assigned seq for that month.

- [ ] **Step 6: Start Talos and visually confirm**

Launch Talos. Open the proposer screen. The `#` column should now show values like `26.03.001`, `26.04.087`, etc. — no more `48`-style integers, no `—` for any actively-loaded game.

- [ ] **Step 7: Trigger a fresh add and verify it gets a current-month ID**

Add a new game via the UI. Its `#` should be `26.04.NNN` where NNN = (max April seq) + 1 — i.e. continuing from where migration left off.

- [ ] **Step 8: Commit the migration artifacts (if appropriate)**

`talos_data.db` likely isn't committed (operator state); `games_full.json` may or may not be. Check `.gitignore`. If `games_full.json` is committed, commit it now:

```bash
git status  # confirm what changed
# If games_full.json is tracked:
git add games_full.json
git commit -m "chore(migration): backfill talos_ids on games_full.json"
```

---

## Self-Review

**Spec coverage check (against the brainstormed decisions):**

| Decision | Task |
|----------|------|
| Format `YY.MM.NNN` (e.g. `26.04.188`) | Task 1 (encoding), Task 6 (display) |
| Stored as 7-digit int `YYMMNNN` | Task 1 |
| Monthly sequence reset, persistent across restarts | Task 2 (table), Task 3 (`next_id`) |
| Local-time month boundary | Task 3 |
| Display full form in `#` column (column widens) | Task 6 |
| Migrate via `game_adds.MIN(ts)` per event_ticker | Task 7 |
| Fix the persist-zero bug as prerequisite | Task 4 |
| Counter state in `talos_data.db` | Task 2 |

All decisions covered.

**Placeholder scan:** No "TBD", "fill in later", or generic "add error handling" — every step has concrete code or commands. Two soft references ("adjust to actual export", "mirror the project's existing test pattern") are intentional because the executor needs to read a tiny bit of local code to confirm the symbol name; the surrounding context tells them exactly what to look for.

**Type consistency:** `talos_id: int` everywhere (preserves Pydantic model type). Format helpers `format_talos_id(int) -> str`, `parse_talos_id(str) -> int`, `next_id(conn) -> int`, `Scanner.add_pair(...) -> int`. All consistent.

**Risks called out for executor:**
- Pre-existing persist-zero bug must be fixed before migration runs (Task 4 before Task 7), else migration's writes get overwritten on next save tick.
- Talos must be stopped during migration (Task 8 step 1).
- `Scanner.__init__` callers may construct without the new `id_assigner` arg — backward-compat is preserved by the legacy fallback path in Task 5 step 5.
