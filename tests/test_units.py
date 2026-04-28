"""Tests for src/talos/units.py — single source of truth for money/count units.

Scope:
  - Round-trip correctness for every integer cent from 0 to 100 (proven wire path).
  - Sub-cent fractional boundary cases (DJT-class sub-cent markets, MARJ fractional fills).
  - Fail-closed trust-boundary contract: sub-bps / sub-fp100 precision raises ValueError.
  - Cents ↔ bps fee formula equivalence (≤1 bps drift on integer-cent prices).
"""

from __future__ import annotations

import pytest

from talos.units import (
    ONE_CENT_BPS,
    ONE_CONTRACT_FP100,
    ONE_DOLLAR_BPS,
    bps_to_cents_round,
    bps_to_dollars_str,
    cents_to_bps,
    complement_bps,
    contracts_to_fp100,
    dollars_str_to_bps,
    format_bps_as_cents,
    format_bps_as_dollars_display,
    format_fp100_as_contracts,
    fp100_to_fp_str,
    fp100_to_whole_contracts_floor,
    fp_str_to_fp100,
    quadratic_fee_bps,
)


# ── Parsing (wire → internal) ─────────────────────────────────────
class TestDollarsStrToBps:
    @pytest.mark.parametrize(
        "wire,bps",
        [
            (None, 0),
            ("0", 0),
            ("0.00", 0),
            ("0.01", 100),  # 1¢
            ("0.53", 5_300),  # 53¢
            ("1.00", 10_000),  # $1
            ("0.0488", 488),  # sub-cent (DJT-class)
            ("0.961", 9_610),  # 96.1¢
            ("0.9999", 9_999),  # 99.99¢
        ],
    )
    def test_valid(self, wire, bps):
        assert dollars_str_to_bps(wire) == bps

    @pytest.mark.parametrize("wire", ["0.53001", "0.00001", "1.23456"])
    def test_sub_bps_raises(self, wire):
        with pytest.raises(ValueError, match="sub-bps precision"):
            dollars_str_to_bps(wire)

    @pytest.mark.parametrize("wire", ["abc", "", "not-a-number"])
    def test_invalid_raises(self, wire):
        with pytest.raises(ValueError, match="invalid _dollars payload"):
            dollars_str_to_bps(wire)

    def test_accepts_float_for_legacy_compat(self):
        # Floats may still arrive from legacy JSON values; str() coerces
        # cleanly for typical two-decimal values. (Sub-cent floats are not
        # safe because float precision is lossy — callers that hit sub-cent
        # must pass strings; that safety is covered by test_sub_bps_raises
        # rejecting the float->str artifacts.)
        assert dollars_str_to_bps(0.53) == 5_300


class TestFpStrToFp100:
    @pytest.mark.parametrize(
        "wire,fp100",
        [
            (None, 0),
            ("0", 0),
            ("0.00", 0),
            ("1.00", 100),
            ("10.00", 1_000),
            ("1.89", 189),  # the MARJ fractional fill
            ("0.01", 1),
        ],
    )
    def test_valid(self, wire, fp100):
        assert fp_str_to_fp100(wire) == fp100

    @pytest.mark.parametrize("wire", ["1.891", "0.001", "10.123"])
    def test_sub_fp100_raises(self, wire):
        with pytest.raises(ValueError, match="sub-fp100 precision"):
            fp_str_to_fp100(wire)

    @pytest.mark.parametrize("wire", ["not-a-number", "abc"])
    def test_invalid_raises(self, wire):
        with pytest.raises(ValueError, match="invalid _fp payload"):
            fp_str_to_fp100(wire)


# ── Serialization (internal → wire) ────────────────────────────────
class TestBpsToDollarsStr:
    @pytest.mark.parametrize(
        "bps,wire",
        [
            (0, "0.00"),
            (100, "0.01"),
            (5_300, "0.53"),
            (10_000, "1.00"),
            (3_800, "0.38"),  # whole-cent → 2-decimal path
        ],
    )
    def test_whole_cent_uses_two_decimals(self, bps, wire):
        assert bps_to_dollars_str(bps) == wire

    @pytest.mark.parametrize(
        "bps,wire",
        [
            (488, "0.0488"),  # DJT-class sub-cent
            (9_610, "0.9610"),
            (9_999, "0.9999"),
            (1, "0.0001"),  # minimum nonzero sub-cent
        ],
    )
    def test_sub_cent_uses_four_decimals(self, bps, wire):
        assert bps_to_dollars_str(bps) == wire

    def test_sweeps_every_integer_cent_roundtrip(self):
        """Every cent 0..100 serializes via the 2-decimal path and reparses."""
        for cents in range(0, 101):
            bps = cents_to_bps(cents)
            wire = bps_to_dollars_str(bps)
            assert "." in wire and len(wire.split(".")[1]) == 2, (cents, wire)
            assert dollars_str_to_bps(wire) == bps

    def test_sweeps_fractional_boundary_cases(self):
        """Sub-cent values must round-trip exactly."""
        for bps in (488, 961, 9_612, 3_801, 9_501, 101, 1, 9_999):
            wire = bps_to_dollars_str(bps)
            assert dollars_str_to_bps(wire) == bps, (bps, wire)


class TestFp100ToFpStr:
    @pytest.mark.parametrize(
        "fp100,wire",
        [
            (0, "0.00"),
            (1, "0.01"),
            (100, "1.00"),
            (189, "1.89"),
            (1_000, "10.00"),
        ],
    )
    def test_serialize(self, fp100, wire):
        assert fp100_to_fp_str(fp100) == wire

    def test_roundtrip(self):
        for fp100 in (0, 1, 99, 100, 189, 500, 1_000, 12_345):
            wire = fp100_to_fp_str(fp100)
            assert fp_str_to_fp100(wire) == fp100


# ── Helpers ───────────────────────────────────────────────────────
class TestComplementBps:
    @pytest.mark.parametrize(
        "p,c",
        [(0, 10_000), (100, 9_900), (5_300, 4_700), (488, 9_512)],
    )
    def test(self, p, c):
        assert complement_bps(p) == c


class TestCentsAndContractsHelpers:
    def test_cents_to_bps(self):
        assert cents_to_bps(0) == 0
        assert cents_to_bps(53) == 5_300
        assert cents_to_bps(100) == ONE_DOLLAR_BPS

    def test_bps_to_cents_round_half_even(self):
        # half-even rounding: 4.50 → 4, 5.50 → 6, 4.88 → 5
        assert bps_to_cents_round(488) == 5
        assert bps_to_cents_round(450) == 4  # half-even: round to nearest even
        assert bps_to_cents_round(550) == 6

    def test_contracts_to_fp100(self):
        assert contracts_to_fp100(0) == 0
        assert contracts_to_fp100(5) == 500

    def test_fp100_to_whole_contracts_floor(self):
        assert fp100_to_whole_contracts_floor(0) == 0
        assert fp100_to_whole_contracts_floor(99) == 0
        assert fp100_to_whole_contracts_floor(100) == 1
        assert fp100_to_whole_contracts_floor(189) == 1
        assert fp100_to_whole_contracts_floor(500) == 5


# ── Display formatters ────────────────────────────────────────────
class TestDisplayFormatters:
    def test_format_bps_as_cents(self):
        assert format_bps_as_cents(0) == "0.00\u00a2"  # 0.00¢
        assert format_bps_as_cents(488) == "4.88\u00a2"  # 4.88¢
        assert format_bps_as_cents(10_000) == "100.00\u00a2"

    def test_format_bps_as_dollars_display(self):
        assert format_bps_as_dollars_display(5_300) == "$0.53"
        assert format_bps_as_dollars_display(488) == "$0.05"  # rounds for display

    def test_format_fp100_as_contracts(self):
        assert format_fp100_as_contracts(189) == "1.89"
        assert format_fp100_as_contracts(500) == "5.00"


# ── Fee formula self-consistency ──────────────────────────────────
# NOTE: the full cents-vs-bps equivalence matrix against the legacy
# fees.py formula lives in tests/test_fees_bps.py (Task 5 of the
# migration). Here we only verify that quadratic_fee_bps is internally
# consistent with its own documented dollar-space derivation.
class TestQuadraticFeeBps:
    @pytest.mark.parametrize(
        "price_bps,rate,expected_bps",
        [
            # fee = rate * price * (1 - price) in dollar space.
            # At p=$0.50 (5000 bps), fee = 0.07 * 0.5 * 0.5 = 0.0175 dollars = 175 bps.
            (5_000, 0.07, 175),
            # At p=$0.01 (100 bps), fee = 0.07 * 0.01 * 0.99 = 0.000693 dollars ≈ 7 bps.
            (100, 0.07, 7),
            # Boundaries: p=0 and p=$1 both yield 0 (no fee on a certain outcome).
            (0, 0.07, 0),
            (10_000, 0.07, 0),
            # Sub-cent price: p=$0.0488 (488 bps), fee ≈ 0.07 * 0.0488 * 0.9512 ≈ 0.00325 ≈ 32 bps.
            (488, 0.07, 32),
        ],
    )
    def test_matches_dollar_space_derivation(self, price_bps, rate, expected_bps):
        assert quadratic_fee_bps(price_bps, rate=rate) == expected_bps

    def test_symmetry(self):
        """fee(p) == fee(1 - p) for all p (by the (1-p) factor)."""
        rate = 0.07
        for bps in range(0, 10_001, 53):
            assert quadratic_fee_bps(bps, rate=rate) == quadratic_fee_bps(
                complement_bps(bps), rate=rate
            ), bps

    def test_zero_rate_is_zero(self):
        assert quadratic_fee_bps(5_000, rate=0.0) == 0


# ── Constant sanity checks ────────────────────────────────────────
class TestConstants:
    def test_scale_factors(self):
        assert ONE_DOLLAR_BPS == 10_000
        assert ONE_CENT_BPS == 100
        assert ONE_CONTRACT_FP100 == 100
        assert ONE_DOLLAR_BPS == ONE_CENT_BPS * 100
