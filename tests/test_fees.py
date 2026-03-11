"""Tests for maker fee calculations (quadratic fee model)."""

from __future__ import annotations

import pytest

from talos.fees import (
    MAKER_FEE_RATE,
    american_odds,
    fee_adjusted_cost,
    fee_adjusted_edge,
    fee_adjusted_profit_matched,
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
