"""Comprehensive tests for PortfolioPanel rendering.

Verifies that the panel actually renders visible content at various
terminal sizes and after data updates. Tests both render() output
and actual rendered terminal strips (what the user sees).
"""

from __future__ import annotations

from talos.ui.app import TalosApp
from talos.ui.widgets import ActivityLog, OrderLog, PortfolioPanel


class TestPortfolioPanelRendering:
    async def test_initial_content_visible(self) -> None:
        """Panel should show $0.00 values immediately on mount."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            await pilot.pause()
            text = str(panel.render())
            assert "Cash:" in text, f"Missing 'Cash:' in: {text[:200]}"
            assert "$0.00" in text, f"Missing '$0.00' in: {text[:200]}"
            assert "Today:" in text, f"Missing 'Today:' in: {text[:200]}"

    async def test_has_nonzero_dimensions(self) -> None:
        """Panel must have real width and height, not zero."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            await pilot.pause()
            assert panel.size.width > 0, f"Panel width is 0: {panel.size}"
            assert panel.size.height > 0, f"Panel height is 0: {panel.size}"

    async def test_content_size_nonzero(self) -> None:
        """Content size must be nonzero (content actually renders)."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            await pilot.pause()
            assert panel.content_size.width > 0, "Content width 0"
            assert panel.content_size.height > 0, "Content height 0"

    async def test_update_balance_reflects_in_render(self) -> None:
        """After update_balance, render() should show new values."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            panel.update_balance(125000, 210050)
            await pilot.pause()
            text = str(panel.render())
            assert "$1,250.00" in text, f"Missing $1,250.00 in: {text[:300]}"

    async def test_update_portfolio_summary_reflects(self) -> None:
        """Locked/exposure/invested should update."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            panel = app.query_one(PortfolioPanel)
            panel.update_portfolio_summary(locked=1500.0, exposure=800, invested=5000)
            await pilot.pause()
            text = str(panel.render())
            assert "$15.00" in text, f"Missing locked $15.00 in: {text[:300]}"

    async def test_all_three_panels_have_regions(self) -> None:
        """All bottom panels must have nonzero regions (visible on screen)."""
        app = TalosApp()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            for widget_class in [
                PortfolioPanel,
                ActivityLog,
                OrderLog,
            ]:
                w = app.query_one(widget_class)
                assert w.region.width > 0, f"{widget_class.__name__} region width 0"
                assert w.region.height > 0, f"{widget_class.__name__} region height 0"

    async def test_render_lines_contain_text(self) -> None:
        """The actual rendered strips (what the terminal sees) must contain panel text."""
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
            assert "$5,000.00" in all_text, f"$5,000.00 not in rendered strips: {all_text[:500]}"

    async def test_panel_visible_at_small_terminal(self) -> None:
        """Panel should still be visible at 80x24 terminal size."""
        app = TalosApp()
        async with app.run_test(size=(80, 24)) as pilot:
            panel = app.query_one(PortfolioPanel)
            await pilot.pause()
            assert panel.size.width > 0
            assert panel.size.height > 0
            text = str(panel.render())
            assert "Cash:" in text

    async def test_panel_visible_at_large_terminal(self) -> None:
        """Panel should still be visible at 200x50 terminal size."""
        app = TalosApp()
        async with app.run_test(size=(200, 50)) as pilot:
            panel = app.query_one(PortfolioPanel)
            await pilot.pause()
            assert panel.size.width > 0
            assert panel.size.height > 0
            text = str(panel.render())
            assert "Cash:" in text
