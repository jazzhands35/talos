"""Modal screens for Talos TUI."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, TextArea

from talos.models.strategy import Opportunity


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


class BidScreen(ModalScreen[dict[str, object] | None]):
    """Confirmation modal for placing NO bids on both legs."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, opportunity: Opportunity) -> None:
        super().__init__()
        self._opp = opportunity

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        opp = self._opp
        cost = opp.no_a + opp.no_b
        max_profit_cents = opp.raw_edge * opp.tradeable_qty

        with Vertical(id="modal-dialog"):
            yield Label("Place NO Bids", classes="modal-title")
            yield Label(f"{opp.event_ticker} — Edge: {opp.raw_edge}¢")
            yield Label(f"Leg A: BUY NO {opp.ticker_a} @ {opp.no_a}¢")
            yield Label(f"Leg B: BUY NO {opp.ticker_b} @ {opp.no_b}¢")
            yield Label(f"Qty (max {opp.tradeable_qty}):")
            yield Input(
                value=str(opp.tradeable_qty),
                id="qty-input",
                type="integer",
            )
            yield Label(
                f"Total: ${cost * opp.tradeable_qty / 100:.2f} → "
                f"Profit: ${max_profit_cents / 100:.2f}",
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
            self.dismiss({
                "ticker_a": self._opp.ticker_a,
                "ticker_b": self._opp.ticker_b,
                "no_a": self._opp.no_a,
                "no_b": self._opp.no_b,
                "qty": qty,
            })
