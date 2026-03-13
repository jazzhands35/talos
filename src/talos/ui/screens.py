"""Modal screens for Talos TUI."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, TextArea

from talos.models.strategy import BidConfirmation, Opportunity


class AddGamesScreen(ModalScreen[list[str] | None]):
    """Modal for adding games by URL or ticker."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label("Add Games", classes="modal-title")
            yield Label("Paste Kalshi game URLs or event tickers, one per line:")
            yield TextArea(id="url-input")
            yield Label("", id="modal-error", classes="modal-error")
            with Vertical(id="modal-buttons"):
                yield Button("Cancel", id="cancel-btn", variant="default")
                yield Button("Add", id="add-btn", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.dismiss(None)
        elif event.button.id == "add-btn":
            text_area = self.query_one("#url-input", TextArea)
            raw = text_area.text.strip()
            if not raw:
                self.query_one("#modal-error", Label).update("Enter at least one URL or ticker")
                return
            urls = [line.strip() for line in raw.splitlines() if line.strip()]
            self.dismiss(urls)


class UnitSizeScreen(ModalScreen[int | None]):
    """Modal for setting the unit size."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, current: int) -> None:
        super().__init__()
        self._current = current

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label("Set Unit Size", classes="modal-title")
            yield Label(f"Current unit size: {self._current}")
            yield Input(
                value=str(self._current),
                id="unit-input",
                type="integer",
            )
            yield Label("", id="modal-error", classes="modal-error")
            with Vertical(id="modal-buttons"):
                yield Button("Cancel", id="cancel-btn", variant="default")
                yield Button("Set", id="set-btn", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.dismiss(None)
        elif event.button.id == "set-btn":
            unit_input = self.query_one("#unit-input", Input)
            try:
                size = int(unit_input.value)
            except ValueError:
                self.query_one("#modal-error", Label).update("Enter a valid number")
                return
            if size < 1:
                self.query_one("#modal-error", Label).update("Unit size must be at least 1")
                return
            self.dismiss(size)


class BidScreen(ModalScreen[BidConfirmation | None]):
    """Confirmation modal for placing NO bids on both legs."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, opportunity: Opportunity) -> None:
        super().__init__()
        self._opp = opportunity

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        opp = self._opp

        with Vertical(id="modal-dialog"):
            yield Label("Place NO Bids", classes="modal-title")
            yield Label(f"{opp.event_ticker} — Edge: {opp.fee_edge:.1f}¢ (raw {opp.raw_edge}¢)")
            yield Label(f"Leg A: BUY NO {opp.ticker_a} @ {opp.no_a}¢")
            yield Label(f"Leg B: BUY NO {opp.ticker_b} @ {opp.no_b}¢")
            default_qty = min(5, opp.tradeable_qty)
            yield Label(f"Qty (max {opp.tradeable_qty}):")
            yield Input(
                value=str(default_qty),
                id="qty-input",
                type="integer",
            )
            total_cost = opp.cost * default_qty
            fee_profit = opp.fee_edge * default_qty
            fee_pct = opp.fee_rate * 100
            yield Label(
                f"Total: ${total_cost / 100:.2f} → "
                f"Profit: ${fee_profit / 100:.2f} (after {fee_pct:.2g}% fee)",
                id="cost-label",
            )
            yield Label("", id="modal-error", classes="modal-error")
            with Vertical(id="modal-buttons"):
                yield Button("Cancel", id="cancel-btn", variant="default")
                yield Button("Confirm", id="confirm-btn", variant="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.dismiss(None)
        elif event.button.id == "confirm-btn":
            qty_input = self.query_one("#qty-input", Input)
            try:
                qty = int(qty_input.value)
            except ValueError:
                self.query_one("#modal-error", Label).update("Invalid quantity")
                return
            if qty <= 0 or qty > self._opp.tradeable_qty:
                self.query_one("#modal-error", Label).update(
                    f"Quantity must be 1-{self._opp.tradeable_qty}"
                )
                return
            self.dismiss(
                BidConfirmation(
                    ticker_a=self._opp.ticker_a,
                    ticker_b=self._opp.ticker_b,
                    no_a=self._opp.no_a,
                    no_b=self._opp.no_b,
                    qty=qty,
                )
            )


class AutoAcceptScreen(ModalScreen[float | None]):
    """Modal for entering auto-accept duration in hours."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label("Auto-Accept Mode", classes="modal-title")
            yield Label("How many hours to auto-accept proposals?")
            yield Input(
                value="2.0",
                id="hours-input",
                type="number",
            )
            yield Label("", id="modal-error", classes="modal-error")
            with Vertical(id="modal-buttons"):
                yield Button("Cancel", id="cancel-btn", variant="default")
                yield Button("Start", id="start-btn", variant="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.dismiss(None)
        elif event.button.id == "start-btn":
            hours_input = self.query_one("#hours-input", Input)
            try:
                hours = float(hours_input.value)
            except ValueError:
                self.query_one("#modal-error", Label).update("Enter a valid number")
                return
            if hours <= 0 or hours > 24:
                self.query_one("#modal-error", Label).update(
                    "Duration must be greater than 0 and at most 24 hours"
                )
                return
            self.dismiss(hours)
