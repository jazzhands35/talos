"""Bps-aware fee variant tests (post-legacy-deletion).

These tests cover the sole fee API that remains in :mod:`talos.fees` —
the ``_bps`` variants that operate in internal bps/fp100 space. Legacy
cents-scale parity tests were retired alongside the cents-scale
functions themselves.
"""
from __future__ import annotations

import pytest

from talos.fees import (
    KALSHI_FEE_RATE,
    compute_fee_bps,
    fee_adjusted_cost_bps,
    fee_adjusted_edge_bps,
    fee_adjusted_profit_matched_bps,
    flat_fee_bps,
    max_profitable_price_bps,
    scenario_pnl_bps,
)
from talos.units import (
    ONE_CENT_BPS,
    ONE_CONTRACT_FP100,
    ONE_DOLLAR_BPS,
    cents_to_bps,
    contracts_to_fp100,
    quadratic_fee_bps,
)


class TestQuadraticFeeBps:
    @pytest.mark.parametrize("cents", [0, 1, 25, 50, 75, 99, 100])
    def test_integer_cents_formula_matches(self, cents: int) -> None:
        """Spot-check the quadratic formula: fee = rate * price * (1 - price)."""
        bps = cents_to_bps(cents)
        fee_bps = quadratic_fee_bps(bps, rate=KALSHI_FEE_RATE)
        # Expected: rate * cents * (100 - cents), scaled to bps (× 100 / 100 = × 1).
        expected = round(KALSHI_FEE_RATE * cents * (100 - cents))
        assert abs(fee_bps - expected) <= 1  # 1 bps tolerance for rounding

    def test_zero_and_hundred_have_zero_fee(self) -> None:
        assert quadratic_fee_bps(0, rate=KALSHI_FEE_RATE) == 0
        assert quadratic_fee_bps(ONE_DOLLAR_BPS, rate=KALSHI_FEE_RATE) == 0

    def test_monotonic_up_to_fifty_cents(self) -> None:
        """Fee is monotonically increasing on [0, 50¢]."""
        prior = -1
        for cents in range(0, 51):
            fee_bps = quadratic_fee_bps(cents_to_bps(cents), rate=KALSHI_FEE_RATE)
            assert fee_bps >= prior
            prior = fee_bps


class TestFlatFeeBps:
    @pytest.mark.parametrize("cents", [0, 25, 50, 100])
    def test_linear(self, cents: int) -> None:
        fee = flat_fee_bps(cents_to_bps(cents), rate=0.03)
        # flat = price_bps * rate (rounded)
        assert abs(fee - round(cents_to_bps(cents) * 0.03)) <= 1


class TestComputeFeeBpsDispatch:
    @pytest.mark.parametrize(
        "fee_type,expected_nonzero",
        [
            ("quadratic", True),
            ("quadratic_with_maker_fees", True),
            ("flat", True),
            ("fee_free", False),
            ("no_fee", False),
            ("unknown_type", True),  # falls back to quadratic
        ],
    )
    def test_dispatch(self, fee_type: str, expected_nonzero: bool) -> None:
        result = compute_fee_bps(cents_to_bps(48), fee_type=fee_type, rate=0.0175)
        if expected_nonzero:
            assert result > 0
        else:
            assert result == 0


class TestFeeAdjustedCostBps:
    @pytest.mark.parametrize("cents", [0, 10, 25, 50, 75, 99])
    def test_cost_equals_price_plus_fee(self, cents: int) -> None:
        price_bps = cents_to_bps(cents)
        cost_bps = fee_adjusted_cost_bps(price_bps)
        fee_bps = quadratic_fee_bps(price_bps, rate=KALSHI_FEE_RATE)
        assert cost_bps == price_bps + fee_bps


class TestMaxProfitablePriceBps:
    @pytest.mark.parametrize("other_cents", [10, 25, 50, 70, 90])
    def test_returns_profitable_price(self, other_cents: int) -> None:
        other_bps = cents_to_bps(other_cents)
        max_bps = max_profitable_price_bps(other_bps)
        if max_bps > 0:
            # Result + other cost should be < $1 after fees.
            total = fee_adjusted_cost_bps(max_bps) + fee_adjusted_cost_bps(other_bps)
            assert total < ONE_DOLLAR_BPS
        # The next cent up should NOT be profitable.
        if 0 < max_bps < 99 * ONE_CENT_BPS:
            next_bps = max_bps + ONE_CENT_BPS
            total_next = fee_adjusted_cost_bps(next_bps) + fee_adjusted_cost_bps(other_bps)
            assert total_next >= ONE_DOLLAR_BPS


class TestFeeAdjustedEdgeBps:
    @pytest.mark.parametrize(
        "a,b,expect_positive",
        [(25, 50, True), (40, 60, False), (10, 80, True), (55, 55, False)],
    )
    def test_sign(self, a: int, b: int, expect_positive: bool) -> None:
        edge = fee_adjusted_edge_bps(cents_to_bps(a), cents_to_bps(b))
        if expect_positive:
            assert edge > 0
        else:
            assert edge <= 0


class TestScenarioPnlBps:
    def test_matched_100_pair(self) -> None:
        # 100 contracts both sides, costs 50¢/contract side A and 45¢ side B.
        # If A wins: B's 100 contracts pay 100¢ each (= $100), minus outlay.
        net_a, net_b = scenario_pnl_bps(
            filled_a_fp100=contracts_to_fp100(100),
            total_cost_bps_a=cents_to_bps(5000),
            filled_b_fp100=contracts_to_fp100(100),
            total_cost_bps_b=cents_to_bps(4500),
            fees_bps_a=cents_to_bps(50),
            fees_bps_b=cents_to_bps(40),
        )
        # payout_a = (100 * 100 contracts) * 10_000 bps / 100 = 1_000_000 bps
        # outlay = 500_000 + 450_000 + 5_000 + 4_000 = 959_000 bps
        # net_a = 1_000_000 - 959_000 = 41_000 bps
        assert net_a == 41_000
        assert net_b == 41_000


class TestFeeAdjustedProfitMatchedBps:
    @pytest.mark.parametrize(
        "matched,ca,cb,fa,fb,expected_bps",
        [
            # 10 pairs; A=500¢, B=400¢, fees=5¢+4¢.
            # payout = 10 × $1 = 1000¢ = 100_000 bps.
            # outlay = 500+400+5+4 = 909¢ = 90_900 bps → profit = 9_100 bps.
            (10, 500, 400, 5, 4, 9_100),
            # 100 pairs; A=5000¢, B=4500¢, fees=50¢+40¢.
            # payout = 100 × $1 = 10000¢ = 1_000_000 bps.
            # outlay = 5000+4500+50+40 = 9590¢ = 959_000 bps → profit = 41_000 bps.
            (100, 5000, 4500, 50, 40, 41_000),
            (0, 0, 0, 0, 0, 0),
        ],
    )
    def test_various(
        self,
        matched: int,
        ca: int,
        cb: int,
        fa: int,
        fb: int,
        expected_bps: int,
    ) -> None:
        result = fee_adjusted_profit_matched_bps(
            contracts_to_fp100(matched),
            cents_to_bps(ca),
            cents_to_bps(cb),
            fees_bps_a=cents_to_bps(fa),
            fees_bps_b=cents_to_bps(fb),
        )
        assert result == expected_bps


class TestSubCentBpsPaths:
    """The bps variants handle sub-cent prices that a cents-scale API can't."""

    def test_quadratic_fee_at_subcent(self) -> None:
        # Price 4.88¢ = 488 bps. Fee ≈ 0.0175 * 0.0488 * 0.9512 ≈ 0.000813
        # = 8.13 bps → 8 bps (half-even round).
        assert quadratic_fee_bps(488, rate=KALSHI_FEE_RATE) in (8, 9)

    def test_fee_adjusted_cost_at_subcent(self) -> None:
        # Cost = 488 + ~8 = ~496 bps (4.96¢)
        result = fee_adjusted_cost_bps(488, rate=KALSHI_FEE_RATE)
        assert 494 <= result <= 498


class TestScenarioPnlFractional:
    """fp100 counts preserve fractional fills that a whole-contract API can't."""

    def test_fractional_fill_preserved(self) -> None:
        # 1.89 contracts matched against 1.89 contracts; both at 48.88¢
        net_a, net_b = scenario_pnl_bps(
            filled_a_fp100=189,
            total_cost_bps_a=9237,
            filled_b_fp100=189,
            total_cost_bps_b=9237,
        )
        # (189 * 10_000) // 100 = 18_900 bps payout on win side
        # outlay = 9237 + 9237 = 18_474 bps
        # net = 18_900 - 18_474 = 426 bps (= ~4.26¢ profit each scenario)
        assert net_a == 18_900 - 18_474
        assert net_b == 18_900 - 18_474


def test_one_cent_bps_round_trip() -> None:
    """Sanity check on unit constants used throughout the bps API."""
    assert ONE_CENT_BPS == 100
    assert ONE_DOLLAR_BPS == 10_000
    assert ONE_CONTRACT_FP100 == 100
