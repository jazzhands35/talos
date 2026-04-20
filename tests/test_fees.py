"""Tests for maker fee calculations (quadratic fee model)."""

from __future__ import annotations

import pytest

from talos.fees import (
    KALSHI_FEE_RATE,
    KALSHI_MAKER_REBATE_RATE,
    MAKER_FEE_RATE,
    american_odds,
    coerce_persisted_fee_rate,
    compute_fee,
    effective_fee_rate,
    fee_adjusted_cost,
    fee_adjusted_edge,
    fee_adjusted_profit_matched,
    flat_fee,
    quadratic_fee,
    scenario_pnl,
)


class TestQuadraticFee:
    def test_fee_at_50(self) -> None:
        """Maximum fee at price 50 (symmetric)."""
        # 50 * 50 * 0.0175 / 100 = 0.4375
        assert quadratic_fee(50) == pytest.approx(0.4375)

    def test_fee_at_0(self) -> None:
        """No fee at price 0."""
        assert quadratic_fee(0) == 0.0

    def test_fee_at_100(self) -> None:
        """No fee at price 100."""
        assert quadratic_fee(100) == 0.0

    def test_fee_symmetric(self) -> None:
        """Fee is same for NO=30 and NO=70 (symmetric in price)."""
        assert quadratic_fee(30) == pytest.approx(quadratic_fee(70))

    def test_matches_actual_kalshi_data(self) -> None:
        """Verify against actual Kalshi API fee data."""
        # 22 contracts at NO=76: fee_cost = $0.07
        fee_per = quadratic_fee(76)  # cents per contract
        total = fee_per * 22 / 100  # dollars
        assert total == pytest.approx(0.07, abs=0.005)

        # 50 contracts at NO=77: fee_cost = $0.16
        fee_per = quadratic_fee(77)
        total = fee_per * 50 / 100
        assert total == pytest.approx(0.155, abs=0.005)


class TestAmericanOdds:
    def test_underdog_positive_odds(self) -> None:
        """NO at 38¢ (cheap) → positive odds."""
        odds = american_odds(38)
        assert odds is not None
        assert odds > 0

    def test_favorite_negative_odds(self) -> None:
        """NO at 55¢ (expensive) → negative odds."""
        odds = american_odds(55)
        assert odds is not None
        assert odds < -100

    def test_even_money(self) -> None:
        """NO at 50¢ → slightly worse than -100 due to fees."""
        odds = american_odds(50)
        assert odds is not None
        assert odds < -100  # fee pushes past -100

    def test_degenerate_zero(self) -> None:
        assert american_odds(0) is None

    def test_degenerate_100(self) -> None:
        assert american_odds(100) is None

    def test_uses_fee_adjusted_cost(self) -> None:
        """Odds are derived from fee-adjusted cost, not raw price."""
        odds = american_odds(76)
        assert odds is not None
        eff = fee_adjusted_cost(76)
        win = 100 - eff
        expected = -(eff / win) * 100
        assert odds == pytest.approx(expected)


class TestFeeAdjustedCost:
    def test_cost_at_50(self) -> None:
        # 50 + 50*50*0.0175/100 = 50 + 0.4375 = 50.4375
        assert fee_adjusted_cost(50) == pytest.approx(50.4375)

    def test_cost_at_0(self) -> None:
        # 0 + 0*100*0.0175/100 = 0
        assert fee_adjusted_cost(0) == pytest.approx(0.0)

    def test_cost_at_100(self) -> None:
        # 100 + 100*0*0.0175/100 = 100
        assert fee_adjusted_cost(100) == pytest.approx(100.0)

    def test_cost_higher_than_raw(self) -> None:
        assert fee_adjusted_cost(38) > 38

    def test_quadratic_smaller_than_linear(self) -> None:
        """Quadratic fee is always <= linear fee for valid prices."""
        for p in range(1, 100):
            quadratic = fee_adjusted_cost(p)
            linear = p + (100 - p) * MAKER_FEE_RATE
            assert quadratic <= linear

    def test_matches_actual_76(self) -> None:
        """NO=76: 76 + 76*24*0.0175/100 = 76.3192."""
        assert fee_adjusted_cost(76) == pytest.approx(76.3192)

    def test_matches_actual_22(self) -> None:
        """NO=22: 22 + 22*78*0.0175/100 = 22.3003."""
        assert fee_adjusted_cost(22) == pytest.approx(22.3003)


class TestFeeAdjustedEdge:
    def test_symmetric_prices(self) -> None:
        """Equal NO prices → single formula."""
        edge = fee_adjusted_edge(45, 45)
        # 100 - 2 * fee_adjusted_cost(45)
        expected = 100 - 2 * fee_adjusted_cost(45)
        assert edge == pytest.approx(expected)

    def test_asymmetric_prices(self) -> None:
        """Asymmetric prices use quadratic fees on both legs."""
        edge = fee_adjusted_edge(38, 55)
        expected = 100 - fee_adjusted_cost(38) - fee_adjusted_cost(55)
        assert edge == pytest.approx(expected)

    def test_negative_raw_edge_still_computes(self) -> None:
        edge = fee_adjusted_edge(60, 50)
        assert edge < -10

    def test_zero_raw_edge_becomes_negative(self) -> None:
        edge = fee_adjusted_edge(50, 50)
        assert edge < 0

    def test_small_edge_with_asymmetric_prices(self) -> None:
        """Quadratic fees are kinder to asymmetric pairs."""
        # NO=76 + NO=22 = 98, raw edge = 2
        edge = fee_adjusted_edge(76, 22)
        # Quadratic fee is smaller than linear, so edge stays more positive
        assert edge > 0
        assert edge == pytest.approx(100 - fee_adjusted_cost(76) - fee_adjusted_cost(22))

    def test_large_edge(self) -> None:
        edge = fee_adjusted_edge(30, 40)
        # raw = 30, fees are small
        assert edge > 28


class TestScenarioPnl:
    def test_balanced_position_both_positive(self) -> None:
        """Equal fills on both sides → both outcomes profitable (before fees)."""
        net_a, net_b = scenario_pnl(5, 5 * 31, 5, 5 * 67)
        # total_outlay = 155 + 335 = 490
        # net_a = 500 - 490 = 10
        # net_b = 500 - 490 = 10
        assert net_a == pytest.approx(10)
        assert net_b == pytest.approx(10)

    def test_balanced_with_fees(self) -> None:
        """Fees reduce guaranteed profit."""
        net_a, net_b = scenario_pnl(5, 5 * 31, 5, 5 * 67, fees_a=3, fees_b=5)
        # total_outlay = 155 + 335 + 3 + 5 = 498
        # net_a = net_b = 500 - 498 = 2
        assert net_a == pytest.approx(2)
        assert net_b == pytest.approx(2)

    def test_symmetric_fills_equal_scenarios(self) -> None:
        net_a, net_b = scenario_pnl(10, 10 * 45, 10, 10 * 45)
        assert net_a == pytest.approx(net_b)

    def test_unbalanced_more_a_fills(self) -> None:
        """More A fills → B winning is better."""
        net_a, net_b = scenario_pnl(400, 400 * 48, 200, 200 * 50)
        # total_outlay = 19200 + 10000 = 29200
        # A wins: 200*100 - 29200 = -9200
        # B wins: 400*100 - 29200 = 10800
        assert net_a == pytest.approx(-9200)
        assert net_b == pytest.approx(10800)

    def test_no_fills_both_zero(self) -> None:
        net_a, net_b = scenario_pnl(0, 0, 0, 0)
        assert net_a == 0.0
        assert net_b == 0.0

    def test_real_data_krusvi(self) -> None:
        """Verify against actual Kalshi data: KRUSVI 75/75."""
        # KRU: 25@76 + 50@77 = 5750, fees = 24
        # SVI: 25@22 + 50@23 = 1700, fees = 24
        net_a, net_b = scenario_pnl(75, 5750, 75, 1700, fees_a=24, fees_b=24)
        # total_outlay = 5750 + 1700 + 48 = 7498
        # Both: 7500 - 7498 = 2
        assert net_a == pytest.approx(2)
        assert net_b == pytest.approx(2)

    def test_real_data_andsin(self) -> None:
        """Verify against actual Kalshi data: ANDSIN 100/100."""
        # AND: 50@17 + 50@18 = 1750, fees = 26
        # SIN: 50@81 + 50@80 = 8050, fees = 28
        net_a, net_b = scenario_pnl(100, 1750, 100, 8050, fees_a=26, fees_b=28)
        # total_outlay = 1750 + 8050 + 54 = 9854
        # Both: 10000 - 9854 = 146
        assert net_a == pytest.approx(146)
        assert net_b == pytest.approx(146)


class TestFeeAdjustedProfitMatched:
    def test_zero_matched(self) -> None:
        assert fee_adjusted_profit_matched(0, 0, 0) == 0.0

    def test_simple_profit(self) -> None:
        """5 matched at 31/67 → raw profit = 10¢, no fees."""
        profit = fee_adjusted_profit_matched(5, 155, 335)
        assert profit == pytest.approx(10)

    def test_profit_with_fees(self) -> None:
        """Fees reduce profit."""
        profit = fee_adjusted_profit_matched(5, 155, 335, fees_a=3, fees_b=5)
        assert profit == pytest.approx(2)

    def test_symmetric_costs(self) -> None:
        profit = fee_adjusted_profit_matched(10, 450, 450)
        # 1000 - 450 - 450 = 100
        assert profit == pytest.approx(100)

    def test_zero_raw_profit_with_fees(self) -> None:
        """Costs sum to revenue, fees make it negative."""
        profit = fee_adjusted_profit_matched(1, 50, 50, fees_a=1, fees_b=1)
        assert profit == pytest.approx(-2)

    def test_real_data_krusvi(self) -> None:
        """75 matched, cost 5750+1700, fees 24+24 → net 2¢."""
        profit = fee_adjusted_profit_matched(75, 5750, 1700, 24, 24)
        assert profit == pytest.approx(2)

    def test_real_data_andsin(self) -> None:
        """100 matched, cost 1750+8050, fees 26+28 → net 146¢."""
        profit = fee_adjusted_profit_matched(100, 1750, 8050, 26, 28)
        assert profit == pytest.approx(146)


class TestDynamicFeeRate:
    """Phase 9: fee functions accept a custom rate parameter."""

    def test_quadratic_fee_with_custom_rate(self) -> None:
        default = quadratic_fee(50)
        custom = quadratic_fee(50, rate=0.02)
        assert custom > default  # higher rate → higher fee
        assert custom == pytest.approx(50 * 50 * 0.02 / 100)

    def test_fee_adjusted_cost_with_custom_rate(self) -> None:
        default = fee_adjusted_cost(45)
        custom = fee_adjusted_cost(45, rate=0.03)
        assert custom > default

    def test_fee_adjusted_edge_with_custom_rate(self) -> None:
        default_edge = fee_adjusted_edge(45, 48)
        custom_edge = fee_adjusted_edge(45, 48, rate=0.03)
        assert custom_edge < default_edge  # higher fees → less edge

    def test_american_odds_with_custom_rate(self) -> None:
        default = american_odds(50)
        custom = american_odds(50, rate=0.03)
        assert default is not None and custom is not None
        assert custom < default  # higher fee → worse odds

    def test_default_rate_is_backward_compatible(self) -> None:
        """All functions produce same results when rate is omitted."""
        assert quadratic_fee(45) == quadratic_fee(45, rate=MAKER_FEE_RATE)
        assert fee_adjusted_cost(45) == fee_adjusted_cost(45, rate=MAKER_FEE_RATE)
        assert fee_adjusted_edge(45, 48) == fee_adjusted_edge(45, 48, rate=MAKER_FEE_RATE)

    def test_flat_fee(self) -> None:
        assert flat_fee(50, rate=0.02) == pytest.approx(50 * 0.02)

    def test_compute_fee_dispatches_quadratic(self) -> None:
        assert compute_fee(50, fee_type="quadratic_with_maker_fees") == pytest.approx(
            quadratic_fee(50)
        )

    def test_compute_fee_dispatches_flat(self) -> None:
        assert compute_fee(50, fee_type="flat", rate=0.02) == pytest.approx(flat_fee(50, rate=0.02))

    def test_compute_fee_dispatches_fee_free(self) -> None:
        assert compute_fee(50, fee_type="fee_free") == 0.0
        assert compute_fee(50, fee_type="no_fee") == 0.0


class TestEffectiveFeeRate:
    """Kalshi fee rate is a platform-wide constant — ``series.fee_multiplier``
    is unreliable (observed returning 1.0 on both ``quadratic`` and
    ``quadratic_with_maker_fees`` series, which would produce edges near
    -50¢ on coin-flip pairs).
    """

    def test_constants(self) -> None:
        assert KALSHI_FEE_RATE == 0.0175
        assert KALSHI_MAKER_REBATE_RATE == 0.00875
        assert MAKER_FEE_RATE == KALSHI_FEE_RATE  # back-compat alias

    def test_quadratic_with_maker_fees(self) -> None:
        assert effective_fee_rate("quadratic_with_maker_fees") == 0.0175

    def test_quadratic(self) -> None:
        assert effective_fee_rate("quadratic") == 0.0175

    def test_fee_free(self) -> None:
        assert effective_fee_rate("fee_free") == 0.0
        assert effective_fee_rate("no_fee") == 0.0

    def test_maker_rebate(self) -> None:
        assert effective_fee_rate("quadratic_with_maker_fees", maker_rebate=True) == 0.00875

    def test_maker_rebate_still_zero_on_fee_free(self) -> None:
        assert effective_fee_rate("fee_free", maker_rebate=True) == 0.0

    def test_kxipo_sanity_at_full_rate(self) -> None:
        """The -46.4¢ regression: NO-A=58 + NO-B=40 should show a small
        positive edge, not catastrophic negative, at the correct rate."""
        rate = effective_fee_rate("quadratic_with_maker_fees")
        edge = fee_adjusted_edge(58, 40, rate=rate)
        # raw edge = 2¢; quadratic fees at 0.0175 eat ~0.85¢ → ~+1.15¢
        assert 0 < edge < 2


class TestCoercePersistedFeeRate:
    """Heals caches that persisted a bad rate before the effective_fee_rate fix."""

    def test_accepts_canonical_full_rate(self) -> None:
        assert coerce_persisted_fee_rate("quadratic_with_maker_fees", 0.0175) == 0.0175

    def test_accepts_canonical_rebate_rate(self) -> None:
        assert coerce_persisted_fee_rate("quadratic_with_maker_fees", 0.00875) == 0.00875

    def test_accepts_zero_on_fee_free(self) -> None:
        assert coerce_persisted_fee_rate("fee_free", 0.0) == 0.0

    def test_repairs_zero_on_paying_series(self) -> None:
        # Historical bug: "quadratic" + fee_multiplier=1.0 routed to 0.0 via
        # old maker_fee_rate. That would understate fees on taker fills.
        assert coerce_persisted_fee_rate("quadratic", 0.0) == 0.0175

    def test_repairs_sentinel_one(self) -> None:
        """The active bug: Kalshi returned fee_multiplier=1.0 and we stored
        it as the rate, producing -46.4¢ edges on KXIPO coin flips."""
        assert coerce_persisted_fee_rate("quadratic_with_maker_fees", 1.0) == 0.0175
        assert coerce_persisted_fee_rate("quadratic", 1.0) == 0.0175

    def test_repairs_arbitrary_garbage(self) -> None:
        assert coerce_persisted_fee_rate("quadratic_with_maker_fees", 0.07) == 0.0175
        assert coerce_persisted_fee_rate("quadratic_with_maker_fees", 42.0) == 0.0175

    def test_fee_free_garbage_falls_to_zero(self) -> None:
        assert coerce_persisted_fee_rate("fee_free", 1.0) == 0.0
