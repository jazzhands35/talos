"""Tests for maker fee calculations."""

from __future__ import annotations

import pytest

from talos.fees import (
    MAKER_FEE_RATE,
    american_odds,
    fee_adjusted_cost,
    fee_adjusted_edge,
    fee_adjusted_profit_matched,
    scenario_pnl,
)


class TestAmericanOdds:
    def test_underdog_positive_odds(self) -> None:
        """NO at 38¢ (p<0.5) → positive odds."""
        odds = american_odds(38)
        # ((1-0.38)/0.38)*100 * 0.9825 = 163.158 * 0.9825 ≈ 160.3
        assert odds is not None
        assert odds == pytest.approx(160.3, abs=0.1)

    def test_favorite_negative_odds(self) -> None:
        """NO at 55¢ (p>=0.5) → negative odds."""
        odds = american_odds(55)
        # -(0.55/0.45)*100 / 0.9825 = -122.222 / 0.9825 ≈ -124.4
        assert odds is not None
        assert odds == pytest.approx(-124.4, abs=0.1)

    def test_even_money(self) -> None:
        """NO at 50¢ → slightly worse than -100 due to fees."""
        odds = american_odds(50)
        assert odds is not None
        assert odds < -100  # fee pushes past -100

    def test_degenerate_zero(self) -> None:
        assert american_odds(0) is None

    def test_degenerate_100(self) -> None:
        assert american_odds(100) is None


class TestFeeAdjustedCost:
    def test_cost_at_50(self) -> None:
        # 50 + 50*0.0175 = 50.875
        assert fee_adjusted_cost(50) == pytest.approx(50.875)

    def test_cost_at_0(self) -> None:
        # 0 + 100*0.0175 = 1.75
        assert fee_adjusted_cost(0) == pytest.approx(1.75)

    def test_cost_at_100(self) -> None:
        # 100 + 0*0.0175 = 100 (no profit, no fee)
        assert fee_adjusted_cost(100) == pytest.approx(100.0)

    def test_cost_higher_than_raw(self) -> None:
        assert fee_adjusted_cost(38) > 38


class TestFeeAdjustedEdge:
    def test_symmetric_prices(self) -> None:
        """Equal NO prices → both scenarios identical."""
        edge = fee_adjusted_edge(45, 45)
        # raw_edge = 10, fee on 55¢ profit = 55*0.0175 = 0.9625
        expected = 55 * (1 - MAKER_FEE_RATE) - 45
        assert edge == pytest.approx(expected)

    def test_asymmetric_prices_takes_worst_case(self) -> None:
        """Cheaper leg winning = more profit = more fee = worst case."""
        # NO-A=38, NO-B=55 → raw_edge=7
        edge = fee_adjusted_edge(38, 55)
        # Scenario B wins (cheaper A wins): (100-38)*0.9825 - 55 = 60.915 - 55 = 5.915
        # Scenario A wins: (100-55)*0.9825 - 38 = 44.2125 - 38 = 6.2125
        assert edge == pytest.approx(5.915)

    def test_negative_raw_edge_still_computes(self) -> None:
        """Fee-adjusted edge is even more negative than raw."""
        edge = fee_adjusted_edge(60, 50)
        # raw = -10, both scenarios negative
        assert edge < -10

    def test_zero_raw_edge_becomes_negative(self) -> None:
        """At raw_edge=0 (cost=100), fees make it negative."""
        edge = fee_adjusted_edge(50, 50)
        assert edge < 0

    def test_small_edge_eaten_by_fees(self) -> None:
        """A 1¢ raw edge can become negative after fees."""
        # NO-A=49, NO-B=50 → raw_edge=1
        edge = fee_adjusted_edge(49, 50)
        # Scenario B wins: (100-49)*0.9825 - 50 = 50.1075 - 50 = 0.1075
        # Scenario A wins: (100-50)*0.9825 - 49 = 49.125 - 49 = 0.125
        assert edge == pytest.approx(0.1075)
        assert edge > 0  # barely positive

    def test_large_edge(self) -> None:
        """Large edge scenario — fees are small relative."""
        edge = fee_adjusted_edge(30, 40)
        # raw = 30
        # B wins: 70*0.9825 - 40 = 68.775 - 40 = 28.775
        # A wins: 60*0.9825 - 30 = 58.95 - 30 = 28.95
        assert edge == pytest.approx(28.775)


class TestScenarioPnl:
    def test_balanced_position_both_positive(self) -> None:
        """Equal fills on both sides → both outcomes profitable."""
        # 5 fills each at 31¢ and 67¢. Total costs: 5*31=155, 5*67=335
        net_a, net_b = scenario_pnl(5, 5 * 31, 5, 5 * 67)
        # A wins: (5*100 - 335)*0.9825 - 155 = 165*0.9825 - 155 = 162.1125 - 155 = 7.1125
        assert net_a == pytest.approx(165 * 0.9825 - 155)
        # B wins: (5*100 - 155)*0.9825 - 335 = 345*0.9825 - 335 = 338.9625 - 335 = 3.9625
        assert net_b == pytest.approx(345 * 0.9825 - 335)
        assert net_a > 0
        assert net_b > 0

    def test_ratio_close_to_one_when_balanced(self) -> None:
        """Equal fills → ratio of scenarios near 1."""
        net_a, net_b = scenario_pnl(10, 10 * 45, 10, 10 * 45)
        # Symmetric: both scenarios identical
        assert net_a == pytest.approx(net_b)

    def test_unbalanced_more_a_fills(self) -> None:
        """More A fills → B winning is much better than A winning."""
        net_a, net_b = scenario_pnl(400, 400 * 48, 200, 200 * 50)
        # B wins: (400*100 - 19200)*0.9825 - 10000 = 20800*0.9825 - 10000
        #       = 20436 - 10000 = 10436
        # A wins: (200*100 - 10000)*0.9825 - 19200 = 10000*0.9825 - 19200
        #       = 9825 - 19200 = -9375
        assert net_b > 0
        assert net_a < 0  # exposed if A wins

    def test_no_fills_both_zero(self) -> None:
        net_a, net_b = scenario_pnl(0, 0, 0, 0)
        assert net_a == 0.0
        assert net_b == 0.0


class TestFeeAdjustedProfitMatched:
    def test_zero_matched(self) -> None:
        assert fee_adjusted_profit_matched(0, 0, 0) == 0.0

    def test_single_pair(self) -> None:
        """5 matched pairs at 31¢ and 67¢ → raw profit = 2¢/pair."""
        profit = fee_adjusted_profit_matched(5, 5 * 31, 5 * 67)
        # Revenue = 500, cost_a = 155, cost_b = 335, raw = 10
        # Fee if A wins: (500 - 335) * 0.0175 = 165 * 0.0175 = 2.8875
        # Fee if B wins: (500 - 155) * 0.0175 = 345 * 0.0175 = 6.0375
        # Worst case = 6.0375
        assert profit == pytest.approx(10 - 6.0375)

    def test_symmetric_costs(self) -> None:
        """Equal costs per side → both fee scenarios identical."""
        profit = fee_adjusted_profit_matched(10, 450, 450)
        # Revenue = 1000, raw = 100, fee = (1000 - 450) * 0.0175 = 9.625
        assert profit == pytest.approx(100 - 9.625)

    def test_zero_raw_profit(self) -> None:
        """Costs sum to revenue → fees make profit negative."""
        profit = fee_adjusted_profit_matched(1, 50, 50)
        # Revenue = 100, raw = 0, fee = 50 * 0.0175 = 0.875
        assert profit < 0
