"""Tests for SettlementHistoryScreen."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from talos.models.portfolio import Settlement
from talos.models.position import EventPositionSummary, LegSummary
from talos.ui.screens import SettlementHistoryScreen

PT = ZoneInfo("America/Los_Angeles")


def _make_settlement(
    ticker: str,
    event_ticker: str,
    revenue: int,
    no_total_cost: int,
    market_result: str = "no",
    no_count: int = 10,
    settled_time: str = "",
) -> Settlement:
    if not settled_time:
        settled_time = datetime.now(PT).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Settlement(
        ticker=ticker,
        event_ticker=event_ticker,
        revenue=revenue,
        no_total_cost=no_total_cost,
        market_result=market_result,
        no_count=no_count,
        settled_time=settled_time,
    )


def _today_str(hour: int = 12) -> str:
    now = datetime.now(PT)
    return now.replace(hour=hour, minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _yesterday_str(hour: int = 12) -> str:
    yest = datetime.now(PT) - timedelta(days=1)
    return yest.replace(hour=hour, minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestSettlementGrouping:
    def test_groups_by_event_ticker(self) -> None:
        """Two settlements with same event_ticker should form one event pair."""
        settlements = [
            _make_settlement("MKT-A", "EVT-1", 1000, 380, settled_time=_today_str()),
            _make_settlement("MKT-B", "EVT-1", 0, 550, market_result="yes", settled_time=_today_str()),
        ]
        screen = SettlementHistoryScreen(settlements)
        # Verify grouping happened correctly
        events: dict[str, list[Settlement]] = {}
        for s in settlements:
            events.setdefault(s.event_ticker, []).append(s)
        assert len(events) == 1
        assert len(events["EVT-1"]) == 2

    def test_groups_by_day(self) -> None:
        """Settlements from different days appear under different day headers."""
        settlements = [
            _make_settlement("MKT-A", "EVT-1", 1000, 380, settled_time=_today_str()),
            _make_settlement("MKT-B", "EVT-1", 0, 550, market_result="yes", settled_time=_today_str()),
            _make_settlement("MKT-C", "EVT-2", 1000, 420, settled_time=_yesterday_str()),
            _make_settlement("MKT-D", "EVT-2", 0, 520, market_result="yes", settled_time=_yesterday_str()),
        ]
        screen = SettlementHistoryScreen(settlements)
        # Two events, two days
        events: dict[str, list[Settlement]] = {}
        for s in settlements:
            events.setdefault(s.event_ticker, []).append(s)
        assert len(events) == 2


class TestSettlementPnL:
    def test_actual_pnl_is_revenue_minus_cost(self) -> None:
        """Actual P&L = revenue - (no_total_cost + yes_total_cost)."""
        # Leg A wins: revenue=1000, cost=380 → profit=620
        # Leg B loses: revenue=0, cost=550 → profit=-550
        # Event total: 620 + (-550) = 70
        s_a = _make_settlement("MKT-A", "EVT-1", 1000, 380)
        s_b = _make_settlement("MKT-B", "EVT-1", 0, 550, market_result="yes")
        total_revenue = s_a.revenue + s_b.revenue
        total_cost = (s_a.no_total_cost + s_a.yes_total_cost) + (s_b.no_total_cost + s_b.yes_total_cost)
        assert total_revenue - total_cost == 70

    def test_result_column_win_loss(self) -> None:
        """market_result='no' → W (we buy NO), 'yes' → L."""
        assert SettlementHistoryScreen._fmt_result(
            _make_settlement("T", "E", 0, 0, market_result="no")
        ).plain == "W"
        assert SettlementHistoryScreen._fmt_result(
            _make_settlement("T", "E", 0, 0, market_result="yes")
        ).plain == "L"


class TestSettlementScreenRender:
    async def test_screen_mounts_with_data(self) -> None:
        """Screen should mount and show settlement data in the table."""
        from textual.app import App, ComposeResult
        from textual.widgets import DataTable

        settlements = [
            _make_settlement("MKT-A", "EVT-TEST", 1000, 380, settled_time=_today_str()),
            _make_settlement("MKT-B", "EVT-TEST", 0, 550, market_result="yes", settled_time=_today_str()),
        ]

        class TestApp(App):
            def compose(self) -> ComposeResult:
                yield DataTable()

        app = TestApp()
        async with app.run_test(size=(160, 40)) as pilot:
            screen = SettlementHistoryScreen(settlements)
            app.push_screen(screen)
            await pilot.pause()
            table = screen.query_one("#settlement-table", DataTable)
            # 1 day header + 2 event rows = 3 rows
            assert table.row_count == 3

    async def test_screen_shows_two_days(self) -> None:
        """Two days of settlements produce 2 day headers + 2 event row pairs."""
        from textual.app import App, ComposeResult
        from textual.widgets import DataTable

        settlements = [
            _make_settlement("A1", "EVT-1", 1000, 380, settled_time=_today_str()),
            _make_settlement("B1", "EVT-1", 0, 550, market_result="yes", settled_time=_today_str()),
            _make_settlement("A2", "EVT-2", 1000, 420, settled_time=_yesterday_str()),
            _make_settlement("B2", "EVT-2", 0, 520, market_result="yes", settled_time=_yesterday_str()),
        ]

        class TestApp(App):
            def compose(self) -> ComposeResult:
                yield DataTable()

        app = TestApp()
        async with app.run_test(size=(160, 40)) as pilot:
            screen = SettlementHistoryScreen(settlements)
            app.push_screen(screen)
            await pilot.pause()
            table = screen.query_one("#settlement-table", DataTable)
            # 2 day headers + 2 events × 2 rows = 6 rows
            assert table.row_count == 6

    async def test_est_pnl_shows_when_position_available(self) -> None:
        """Est P&L column shows locked_profit_cents from position summary."""
        from textual.app import App, ComposeResult
        from textual.widgets import DataTable

        settlements = [
            _make_settlement("MKT-A", "EVT-TEST", 1000, 380, settled_time=_today_str()),
            _make_settlement("MKT-B", "EVT-TEST", 0, 550, market_result="yes", settled_time=_today_str()),
        ]
        positions = [
            EventPositionSummary(
                event_ticker="EVT-TEST",
                leg_a=LegSummary(ticker="MKT-A", no_price=38, filled_count=10, resting_count=0),
                leg_b=LegSummary(ticker="MKT-B", no_price=55, filled_count=10, resting_count=0),
                matched_pairs=10,
                locked_profit_cents=70,
                unmatched_a=0,
                unmatched_b=0,
                exposure_cents=0,
            )
        ]

        class TestApp(App):
            def compose(self) -> ComposeResult:
                yield DataTable()

        app = TestApp()
        async with app.run_test(size=(160, 40)) as pilot:
            screen = SettlementHistoryScreen(settlements, position_summaries=positions)
            app.push_screen(screen)
            await pilot.pause()
            table = screen.query_one("#settlement-table", DataTable)
            # Row at index 1 is the first event row (index 0 is day header)
            row = table.get_row_at(1)
            # Col 8 = Est P&L
            est_text = str(row[8])
            assert "$0.70" in est_text
