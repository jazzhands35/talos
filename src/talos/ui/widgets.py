"""Dashboard widgets for Talos TUI."""

from __future__ import annotations

from textual.widgets import DataTable, Static

from talos.scanner import ArbitrageScanner


def _fmt_cents(value: int) -> str:
    """Format an integer cents value as 'XX¢'."""
    return f"{value}¢"


def _fmt_dollars(cents: int) -> str:
    """Format cents as dollar string."""
    return f"${cents / 100:.2f}"


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

    def refresh_from_scanner(self, scanner: ArbitrageScanner | None) -> None:
        """Rebuild table rows from current scanner opportunities."""
        if scanner is None:
            return

        opps = scanner.opportunities
        current_keys = {row_key.value for row_key in self.rows}
        new_keys = {opp.event_ticker for opp in opps}

        # Remove vanished rows
        for key in current_keys - new_keys:
            self.remove_row(key)

        # Add or update rows
        for opp in opps:
            cost = opp.no_a + opp.no_b
            profit_cents = opp.raw_edge * opp.tradeable_qty
            row_data = (
                opp.event_ticker,
                _fmt_cents(opp.no_a),
                _fmt_cents(opp.no_b),
                _fmt_cents(cost),
                _fmt_cents(opp.raw_edge),
                str(opp.tradeable_qty),
                _fmt_dollars(profit_cents),
                "▸",
            )
            if opp.event_ticker in current_keys:
                # Update existing row cells
                for col_idx, value in enumerate(row_data):
                    col_key = self.ordered_columns[col_idx].key
                    self.update_cell(opp.event_ticker, col_key, value)
            else:
                self.add_row(*row_data, key=opp.event_ticker)


class AccountPanel(Static):
    """Displays balance and open positions."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._balance_text = "Cash: —\nPortfolio: —"
        self._positions_text = ""

    def on_mount(self) -> None:
        self._render_content()

    def update_balance(self, balance_cents: int, portfolio_cents: int) -> None:
        """Update the balance display."""
        self._balance_text = (
            f"Cash:      ${balance_cents / 100:,.2f}\n"
            f"Portfolio: ${portfolio_cents / 100:,.2f}"
        )
        self._render_content()

    def update_positions(self, positions: list[dict[str, object]]) -> None:
        """Update the positions display.

        Each dict has: ticker, qty, price (cents).
        """
        if not positions:
            self._positions_text = ""
            self._render_content()
            return
        lines = []
        for pos in positions:
            ticker = pos["ticker"]
            qty = pos["qty"]
            price = pos["price"]
            lines.append(f"  {ticker}  {qty} @ {price}¢")
        self._positions_text = "\nPOSITIONS\n" + "\n".join(lines)
        self._render_content()

    def _render_content(self) -> None:
        self.update(f"ACCOUNT\n\n{self._balance_text}{self._positions_text}")


class OrderLog(Static):
    """Scrollable log of recent orders."""

    STATUS_ICONS = {
        "executed": "✓",
        "resting": "◷",
        "cancelled": "✗",
    }

    def on_mount(self) -> None:
        self.update("ORDERS\n\nNo orders yet")

    def update_orders(self, orders: list[dict[str, object]]) -> None:
        """Update the order log display.

        Each dict has: ticker, side, price, count, status, time.
        """
        if not orders:
            self.update("ORDERS\n\nNo orders yet")
            return
        lines = []
        for order in orders:
            icon = self.STATUS_ICONS.get(str(order["status"]), "?")
            side = str(order["side"]).upper()
            lines.append(
                f"  {order['time']}  BUY {side} {order['ticker']}  "
                f"{order['price']}¢ x{order['count']}  {icon}"
            )
        self.update("ORDERS\n\n" + "\n".join(lines))
