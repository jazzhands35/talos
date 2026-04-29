"""Tests for talos_id formatting and parsing."""

from __future__ import annotations

import sqlite3

import pytest

from talos.talos_id import (
    InvalidTalosIdError,
    bump_seq,
    encode_talos_id,
    ensure_counter_schema,
    format_talos_id,
    parse_talos_id,
    peek_seq,
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
    # Year out-of-range: gap between YY (0-99) and YYYY (1900-2099)
    with pytest.raises(InvalidTalosIdError):
        encode_talos_id(year=100, month=1, seq=1)
    with pytest.raises(InvalidTalosIdError):
        encode_talos_id(year=1899, month=1, seq=1)
    with pytest.raises(InvalidTalosIdError):
        encode_talos_id(year=2100, month=1, seq=1)


def test_int_form_sorts_chronologically() -> None:
    ids = [
        encode_talos_id(year=26, month=4, seq=1),
        encode_talos_id(year=26, month=4, seq=999),
        encode_talos_id(year=26, month=5, seq=1),
        encode_talos_id(year=27, month=1, seq=1),
    ]
    assert ids == sorted(ids)


def test_format_rejects_negative() -> None:
    with pytest.raises(InvalidTalosIdError):
        format_talos_id(-1)


def test_format_rejects_unencoded_int() -> None:
    # 99001 has month=99 in the YYMMNNN slot — never producible.
    with pytest.raises(InvalidTalosIdError):
        format_talos_id(99_001)


def test_format_rejects_out_of_band_month() -> None:
    # 2613001 looks like 26.13.001 which encode would reject.
    with pytest.raises(InvalidTalosIdError):
        format_talos_id(2_613_001)


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


def test_bump_seq_persists_across_connections(tmp_path) -> None:
    path = tmp_path / "test.db"
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
    with pytest.raises(InvalidTalosIdError):
        bump_seq(conn, year=2026, month=4)
    post = peek_seq(conn, year=2026, month=4)
    assert post == 999, f"rollback failed: persisted {post} instead of 999"


def test_bump_seq_rejects_invalid_month() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_counter_schema(conn)
    with pytest.raises(InvalidTalosIdError):
        bump_seq(conn, year=2026, month=13)
    with pytest.raises(InvalidTalosIdError):
        bump_seq(conn, year=2026, month=0)
