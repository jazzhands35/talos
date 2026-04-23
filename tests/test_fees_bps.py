"""Equivalence tests for bps-aware fee variants vs legacy cents formulas."""
from __future__ import annotations

import pytest

from talos.fees import (
    KALSHI_FEE_RATE,
    compute_fee,
    compute_fee_bps,
    fee_adjusted_cost,
    fee_adjusted_cost_bps,
    fee_adjusted_edge,
    fee_adjusted_edge_bps,
    fee_adjusted_profit_matched,
    fee_adjusted_profit_matched_bps,
    flat_fee,
    flat_fee_bps,
    max_profitable_price,
    max_profitable_price_bps,
    quadratic_fee,
    scenario_pnl,
    scenario_pnl_bps,
)
from talos.units import (
    cents_to_bps,
    contracts_to_fp100,
    quadratic_fee_bps,
)


class TestQuadraticFeeEquivalence:
    @pytest.mark.parametrize("cents", list(range(0, 101)))
    def test_every_integer_cent(self, cents):
        cents_fee = quadratic_fee(cents)  # float cents
        bps_fee = quadratic_fee_bps(
            cents_to_bps(cents), rate=KALSHI_FEE_RATE
        )  # int bps
        # Convert cents float fee to bps (expected ≈ cents_fee * 100)
        assert abs(bps_fee - cents_fee * 100) <= 1  # ≤1 bps drift


class TestFlatFeeEquivalence:
    @pytest.mark.parametrize("cents", [0, 1, 25, 50, 75, 99, 100])
    def test_various_prices(self, cents):
        cents_fee = flat_fee(cents, rate=0.03)
        bps_fee = flat_fee_bps(cents_to_bps(cents), rate=0.03)
        assert abs(bps_fee - cents_fee * 100) <= 1


class TestComputeFeeDispatch:
    @pytest.mark.parametrize(
        "fee_type",
        [
            "quadratic",
            "quadratic_with_maker_fees",
            "flat",
            "fee_free",
            "no_fee",
            "unknown_type",
        ],
    )
    def test_dispatch_matches_legacy(self, fee_type):
        price_cents = 48
        price_bps = cents_to_bps(price_cents)
        rate = 0.0175
        legacy = compute_fee(price_cents, fee_type=fee_type, rate=rate)
        new_bps = compute_fee_bps(price_bps, fee_type=fee_type, rate=rate)
        assert abs(new_bps - legacy * 100) <= 1


class TestFeeAdjustedCostEquivalence:
    @pytest.mark.parametrize("cents", list(range(0, 101, 5)))
    def test_every_fifth_cent(self, cents):
        legacy = fee_adjusted_cost(cents)
        new_bps = fee_adjusted_cost_bps(cents_to_bps(cents))
        assert abs(new_bps - legacy * 100) <= 1


class TestMaxProfitablePriceEquivalence:
    @pytest.mark.parametrize("other_cents", [10, 25, 50, 70, 90])
    def test_selected_other_prices(self, other_cents):
        legacy_max = max_profitable_price(float(other_cents))
        new_bps_max = max_profitable_price_bps(cents_to_bps(other_cents))
        # Results should agree at the whole-cent grid
        assert new_bps_max == cents_to_bps(legacy_max)


class TestFeeAdjustedEdgeEquivalence:
    @pytest.mark.parametrize("a,b", [(25, 50), (40, 60), (10, 80), (55, 55)])
    def test_sample_pairs(self, a, b):
        legacy = fee_adjusted_edge(a, b)
        new_bps = fee_adjusted_edge_bps(cents_to_bps(a), cents_to_bps(b))
        assert abs(new_bps - legacy * 100) <= 2  # 2 bps drift — two fees rounded


class TestScenarioPnlEquivalence:
    def test_matched_100_pair(self):
        legacy_a, legacy_b = scenario_pnl(
            100, 5000, 100, 4500, fees_a=50, fees_b=40
        )
        new_a, new_b = scenario_pnl_bps(
            contracts_to_fp100(100),
            cents_to_bps(5000),
            contracts_to_fp100(100),
            cents_to_bps(4500),
            fees_bps_a=cents_to_bps(50),
            fees_bps_b=cents_to_bps(40),
        )
        assert abs(new_a - legacy_a * 100) <= 1
        assert abs(new_b - legacy_b * 100) <= 1


class TestFeeAdjustedProfitMatchedEquivalence:
    @pytest.mark.parametrize(
        "matched,ca,cb,fa,fb",
        [
            (10, 500, 400, 5, 4),
            (100, 5000, 4500, 50, 40),
            (0, 0, 0, 0, 0),
        ],
    )
    def test_various(self, matched, ca, cb, fa, fb):
        legacy = fee_adjusted_profit_matched(
            matched, ca, cb, fees_a=fa, fees_b=fb
        )
        new_bps = fee_adjusted_profit_matched_bps(
            contracts_to_fp100(matched),
            cents_to_bps(ca),
            cents_to_bps(cb),
            fees_bps_a=cents_to_bps(fa),
            fees_bps_b=cents_to_bps(fb),
        )
        assert abs(new_bps - legacy * 100) <= 1


class TestSubCentBpsPaths:
    """The bps variants must handle sub-cent prices that the legacy path can't."""

    def test_quadratic_fee_at_subcent(self):
        # Price 4.88¢ = 488 bps. Fee ≈ 0.0175 * 0.0488 * 0.9512 ≈ 0.000813 = 8.13 bps → 8 bps.
        assert quadratic_fee_bps(488, rate=KALSHI_FEE_RATE) in (8, 9)

    def test_fee_adjusted_cost_at_subcent(self):
        # Cost = 488 + ~8 = ~496 bps (4.96¢)
        result = fee_adjusted_cost_bps(488, rate=KALSHI_FEE_RATE)
        assert 494 <= result <= 498


class TestScenarioPnlFractional:
    """fp100 counts preserve fractional fills; legacy path would truncate."""

    def test_fractional_fill_preserved(self):
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
