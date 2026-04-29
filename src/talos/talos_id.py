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

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

UNASSIGNED_DISPLAY = "—"


class InvalidTalosIdError(ValueError):
    """Raised when a talos_id value or string is malformed."""


def encode_talos_id(*, year: int, month: int, seq: int) -> int:
    """Pack (year, month, seq) into a 7-digit talos_id."""
    if not 0 <= year <= 99:
        # Accept either YY (0-99) or YYYY (1900-2099) for ergonomics
        if 1900 <= year <= 2099:
            year = year % 100
        else:
            raise InvalidTalosIdError(f"year must be 0-99 or 1900-2099, got {year}")
    if not 1 <= month <= 12:
        raise InvalidTalosIdError(f"month must be 1-12, got {month}")
    if not 1 <= seq <= 999:
        raise InvalidTalosIdError(f"seq must be 1-999, got {seq}")
    return year * 100_000 + month * 1_000 + seq


def format_talos_id(value: int) -> str:
    """Render ``value`` as ``"YY.MM.NNN"``; ``0`` renders as ``"—"``.

    Raises ``InvalidTalosIdError`` for any ``int`` not produced by
    ``encode_talos_id`` (i.e. outside the inclusive range
    ``1_001 .. 9_912_999`` and not the zero sentinel).
    """
    if value == 0:
        return UNASSIGNED_DISPLAY
    if not 1_001 <= value <= 9_912_999:
        raise InvalidTalosIdError(
            f"not a valid encoded talos_id: {value}"
        )
    yy = value // 100_000
    mm = (value // 1_000) % 100
    nnn = value % 1_000
    if not 1 <= mm <= 12 or not 1 <= nnn <= 999:
        raise InvalidTalosIdError(
            f"not a valid encoded talos_id: {value}"
        )
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


# ── Persistent monthly counter ─────────────────────────────────────────────

_COUNTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS talos_id_counter (
    year_month INTEGER PRIMARY KEY,
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

    Raises ``InvalidTalosIdError`` if the resulting seq would exceed 999.
    """
    key = _year_month_key(year, month)
    # Single-statement upsert: insert last_seq=1 for new month, otherwise
    # increment. SQLite serializes writes per database, so this is atomic
    # against any other writer on the same connection or db file.
    conn.execute(
        """
        INSERT INTO talos_id_counter(year_month, last_seq) VALUES (?, 1)
        ON CONFLICT(year_month) DO UPDATE SET last_seq = last_seq + 1
        """,
        (key,),
    )
    row = conn.execute(
        "SELECT last_seq FROM talos_id_counter WHERE year_month = ?",
        (key,),
    ).fetchone()
    nxt = int(row[0])
    if nxt > 999:
        # Roll back the increment so retries see the pre-overflow state.
        conn.execute(
            "UPDATE talos_id_counter SET last_seq = ? WHERE year_month = ?",
            (nxt - 1, key),
        )
        conn.commit()
        raise InvalidTalosIdError(
            f"seq exhausted for {year:04d}-{month:02d} (>999 adds)"
        )
    conn.commit()
    return nxt


# ── Local-time next ID ─────────────────────────────────────────────────────

_LOCAL_TZ = ZoneInfo("America/Los_Angeles")  # User's local timezone (Pacific).


def next_id(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """Assign the next ``talos_id`` for the current local month.

    ``now`` defaults to the current local time; pass an aware datetime to
    test month-boundary behavior. Local time (not UTC) determines the
    month so that a game added at 23:30 PT on April 30 is ``26.04.NNN``,
    not ``26.05.NNN``.

    Raises ``InvalidTalosIdError`` if ``now`` is naive (no ``tzinfo``).
    """
    moment = now if now is not None else datetime.now(_LOCAL_TZ)
    if moment.tzinfo is None:
        raise InvalidTalosIdError("now must be timezone-aware")
    local = moment.astimezone(_LOCAL_TZ)
    seq = bump_seq(conn, year=local.year, month=local.month)
    return encode_talos_id(year=local.year, month=local.month, seq=seq)
