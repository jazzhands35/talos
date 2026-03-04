"""Main Talos TUI application."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header

from talos.scanner import ArbitrageScanner
from talos.ui.theme import APP_CSS
from talos.ui.widgets import AccountPanel, OpportunitiesTable, OrderLog


class TalosApp(App):
    """Talos arbitrage trading dashboard."""

    CSS = APP_CSS
    TITLE = "TALOS"
    BINDINGS = [
        ("a", "add_games", "Add Games"),
        ("d", "remove_game", "Remove Game"),
        ("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        scanner: ArbitrageScanner | None = None,
    ) -> None:
        super().__init__()
        self._scanner = scanner

    def compose(self) -> ComposeResult:
        yield Header()
        yield OpportunitiesTable(id="opportunities-table")
        with Horizontal(id="bottom-panels"):
            yield AccountPanel(id="account-panel")
            yield OrderLog(id="order-log")
        yield Footer()

    def refresh_opportunities(self) -> None:
        """Update the opportunities table from scanner state."""
        table = self.query_one(OpportunitiesTable)
        table.refresh_from_scanner(self._scanner)

    def action_add_games(self) -> None:
        """Placeholder — will open Add Games modal."""

    def action_remove_game(self) -> None:
        """Placeholder — will remove selected game."""
