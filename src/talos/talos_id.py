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
