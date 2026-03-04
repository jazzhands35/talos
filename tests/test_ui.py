"""Tests for Talos TUI dashboard."""

from __future__ import annotations

import pytest

from talos.ui.app import TalosApp


class TestAppMount:
    async def test_app_mounts_without_error(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            assert app.query_one("#opportunities-table") is not None

    async def test_app_has_header_and_footer(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            from textual.widgets import Footer, Header

            assert len(app.query(Header)) == 1
            assert len(app.query(Footer)) == 1

    async def test_app_has_bottom_panels(self) -> None:
        app = TalosApp()
        async with app.run_test() as pilot:
            assert app.query_one("#account-panel") is not None
            assert app.query_one("#order-log") is not None
