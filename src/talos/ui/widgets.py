"""Dashboard widgets for Talos TUI."""

from __future__ import annotations

from typing import Any

from textual.widgets import DataTable, Static

from talos.models.position import EventPositionSummary
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
        self.add_columns("Event", "NO-A", "NO-B", "Cost", "Edge", "Depth", "$/pair", "")

    def refresh_from_scanner(self, scanner: ArbitrageScanner | None) -> None:
        """Rebuild table rows from current scanner state."""
        if scanner is None:
            return

        all_snaps = scanner.all_snapshots
        current_keys = {row_key.value for row_key in self.rows}
        new_keys = set(all_snaps.keys())

        # Remove vanished rows
        for key in current_keys - new_keys:
            if key is not None:
                self.remove_row(key)

        # Add or update rows (positive edge first, then rest)
        sorted_opps = sorted(all_snaps.values(), key=lambda o: o.raw_edge, reverse=True)
        for opp in sorted_opps:
            edge_str = _fmt_cents(opp.raw_edge) if opp.raw_edge > 0 else f"{opp.raw_edge}¢"
            profit_str = _fmt_dollars(opp.raw_edge) if opp.raw_edge > 0 else "—"
            row_data = (
                opp.event_ticker,
                _fmt_cents(opp.no_a),
                _fmt_cents(opp.no_b),
                _fmt_cents(opp.cost),
                edge_str,
                str(opp.tradeable_qty),
                profit_str,
                "▸" if opp.raw_edge > 0 else "",
            )
            if opp.event_ticker in current_keys:
                for col_idx, value in enumerate(row_data):
                    col_key = self.ordered_columns[col_idx].key
                    self.update_cell(opp.event_ticker, col_key, value)
            else:
                self.add_row(*row_data, key=opp.event_ticker)


class AccountPanel(Static):
    """Displays balance and arb-pair position summaries."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._balance_text = "Cash: —\nPortfolio: —"
        self._positions_text = ""

    def on_mount(self) -> None:
        self._render_content()

    def update_balance(self, balance_cents: int, portfolio_cents: int) -> None:
        """Update the balance display."""
        self._balance_text = (
            f"Cash:      ${balance_cents / 100:,.2f}\nPortfolio: ${portfolio_cents / 100:,.2f}"
        )
        self._render_content()

    def update_event_positions(self, summaries: list[EventPositionSummary]) -> None:
        """Update the positions display from computed arb-pair summaries."""
        if not summaries:
            self._positions_text = ""
            self._render_content()
            return
        lines: list[str] = []
        for s in summaries:
            edge = 100 - s.leg_a.no_price - s.leg_b.no_price
            lines.append(f"\n{s.event_ticker} — {edge}¢ edge")
            total_a = s.leg_a.filled_count + s.leg_a.resting_count
            total_b = s.leg_b.filled_count + s.leg_b.resting_count
            lines.append(
                f"  {s.leg_a.ticker}:  {s.leg_a.filled_count}/{total_a} filled"
                f"  {s.leg_a.resting_count} resting @ {s.leg_a.no_price}¢"
            )
            lines.append(
                f"  {s.leg_b.ticker}:  {s.leg_b.filled_count}/{total_b} filled"
                f"  {s.leg_b.resting_count} resting @ {s.leg_b.no_price}¢"
            )
            lines.append(
                f"  Matched: {s.matched_pairs} pairs"
                f"  Locked: ${s.locked_profit_cents / 100:.2f}"
            )
            lines.append(f"  Exposure: ${s.exposure_cents / 100:.2f}")
        self._positions_text = "\n" + "\n".join(lines)
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

        Each dict has: ticker, side, price, filled, total, remaining, status,
        time, and optionally queue_pos.
        """
        if not orders:
            self.update("ORDERS\n\nNo orders yet")
            return
        lines = []
        for order in orders:
            icon = self.STATUS_ICONS.get(str(order["status"]), "?")
            side = str(order["side"]).upper()
            filled = order.get("filled", 0)
            total = order.get("total", 0)
            remaining = order.get("remaining", 0)
            queue_pos = order.get("queue_pos")
            pos_str = f"  #{queue_pos}" if queue_pos is not None else ""
            lines.append(
                f"  {order['time']}  BUY {side} {order['ticker']}  "
                f"{order['price']}¢  {filled}/{total}  {remaining} resting  {icon}{pos_str}"
            )
        self.update("ORDERS\n\n" + "\n".join(lines))
