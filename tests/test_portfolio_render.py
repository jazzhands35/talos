"""Tests for PortfolioPanel rendering.

Verifies the panel renders the Account + Coverage layout correctly
with various data states.
"""

from __future__ import annotations

from talos.ui.app import TalosApp
from talos.ui.widgets import ActivityLog, OrderLog, PortfolioPanel


class TestPortfolioPanelRendering:
    async def test_initial_content_visible(self) -> None:
        """Panel should show $0.00 and zero counts on mount."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            await pilot.pause()
            text = str(panel.render())
            assert "Cash:" in text
            assert "$0.00" in text
            assert "Matched:" in text
            assert "Events:" in text

    async def test_has_nonzero_dimensions(self) -> None:
        """Panel must have real width and height, not zero."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            await pilot.pause()
            assert panel.size.width > 0
            assert panel.size.height > 0

    async def test_content_size_nonzero(self) -> None:
        """Content size must be nonzero (content actually renders)."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            await pilot.pause()
            assert panel.content_size.width > 0
            assert panel.content_size.height > 0

    async def test_update_balance_reflects_in_render(self) -> None:
        """After update_balance, render() should show new cash value."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            panel.update_balance(125000, 210050)
            await pilot.pause()
            text = str(panel.render())
            assert "$1,250.00" in text

    async def test_update_account_reflects(self) -> None:
        """Matched/partial/locked/exposure should update."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            panel.update_account(matched=5, partial=3, locked=1500.0, exposure=800)
            await pilot.pause()
            text = str(panel.render())
            assert "5 units" in text
            assert "3 events" in text
            assert "$15.00" in text
            assert "$8.00" in text

    async def test_update_coverage_reflects(self) -> None:
        """Events/positions/bidding/unentered should update."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            panel.update_coverage(events=100, with_positions=20, bidding=30, unentered=50)
            await pilot.pause()
            text = str(panel.render())
            assert "100" in text
            assert "20" in text
            assert "30" in text
            assert "50" in text

    async def test_no_pnl_section(self) -> None:
        """P&L section (Today/Yesterday/7d) must be gone."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            await pilot.pause()
            text = str(panel.render())
            assert "Today:" not in text
            assert "Yesterday:" not in text
            assert "Last 7d:" not in text

    async def test_all_three_panels_have_regions(self) -> None:
        """All bottom panels must have nonzero regions."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            for widget_class in [PortfolioPanel, ActivityLog, OrderLog]:
                w = app.query_one(widget_class)
                assert w.region.width > 0
                assert w.region.height > 0

    async def test_render_lines_contain_text(self) -> None:
        """Actual rendered strips must contain panel text."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            panel.update_balance(500000, 0)
            await pilot.pause()
            from textual.geometry import Region

            lines = panel.render_lines(Region(0, 0, panel.size.width, panel.size.height))
            all_text = ""
            for strip in lines:
                for seg in strip._segments:
                    all_text += seg.text
            assert "$5,000.00" in all_text

    async def test_panel_visible_at_small_terminal(self) -> None:
        """Panel visible at 80x24."""
        app = TalosApp()
        async with app.run_test(size=(80, 24)) as pilot:
            panel = app.query_one(PortfolioPanel)
            await pilot.pause()
            assert panel.size.width > 0
            text = str(panel.render())
            assert "Cash:" in text

    async def test_panel_visible_at_large_terminal(self) -> None:
        """Panel visible at 200x50."""
        app = TalosApp()
        async with app.run_test(size=(200, 50)) as pilot:
            panel = app.query_one(PortfolioPanel)
            await pilot.pause()
            assert panel.size.width > 0
            text = str(panel.render())
            assert "Cash:" in text
