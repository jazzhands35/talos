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
