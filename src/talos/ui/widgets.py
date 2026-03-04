"""Dashboard widgets for Talos TUI."""

from __future__ import annotations

from textual.widgets import DataTable, Static


class OpportunitiesTable(DataTable):
    """Live-updating arbitrage opportunities table."""

    DEFAULT_CSS = """
    OpportunitiesTable {
        height: 1fr;
    }
    """

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_columns(
            "Event", "NO-A", "NO-B", "Cost", "Edge", "Qty", "Profit", ""
        )


class AccountPanel(Static):
    """Displays balance and open positions."""

    def on_mount(self) -> None:
        self.update("ACCOUNT\n\nCash: —\nPortfolio: —")


class OrderLog(Static):
    """Scrollable log of recent orders."""

    def on_mount(self) -> None:
        self.update("ORDERS\n\nNo orders yet")
