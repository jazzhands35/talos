"""Dashboard widgets for Talos TUI."""

from __future__ import annotations

from typing import Any

from textual.widgets import DataTable, Static

from talos.cpm import format_cpm, format_eta
from talos.fees import american_from_win_risk, american_odds, fee_adjusted_cost, scenario_pnl
from talos.models.position import EventPositionSummary
from talos.scanner import ArbitrageScanner
from talos.top_of_market import TopOfMarketTracker


def _fmt_cents(value: int) -> str:
    """Format an integer cents value as 'XX¢'."""
    return f"{value}¢"


def _fmt_pos(filled: int, total: int, avg_no_price: int) -> str:
    """Format position as 'filled/total avg¢' with fee-adjusted cost."""
    if total == 0:
        return "—"
    if filled == 0:
        return f"0/{total}"
    fee_avg = fee_adjusted_cost(avg_no_price)
    return f"{filled}/{total} {fee_avg:.1f}¢"


def _fmt_odds(no_price: int) -> str:
    """Format fee-adjusted American odds for a NO price."""
    odds = american_odds(no_price)
    if odds is None:
        return "—"
    r = round(odds)
    return f"+{r}" if r > 0 else str(r)


def _fmt_net_odds(
    no_a: int,
    no_b: int,
    filled_a: int = 0,
    filled_b: int = 0,
    total_cost_a: int = 0,
    total_cost_b: int = 0,
) -> str:
    """Format net position as wager amount, side, and American odds.

    With no fills: shows per-leg odds only.
    With fills: computes scenario P&Ls, shows "$X on [side] [odds]".
    Base wager = loss if positive odds, win if negative odds.
    """
    odds_a = _fmt_odds(no_a)
    odds_b = _fmt_odds(no_b)
    odds_str = f"{odds_a}/{odds_b}"
    if filled_a + filled_b == 0:
        return odds_str
    net_a, net_b = scenario_pnl(filled_a, total_cost_a, filled_b, total_cost_b)
    worse = min(net_a, net_b)
    better = max(net_a, net_b)
    # Both scenarios profitable → guaranteed profit, no risk
    if worse > 0:
        return f"GTD ${worse / 100:.2f}"
    # Both scenarios negative → underwater, no meaningful odds
    if better <= 0:
        return f"-${abs(better) / 100:.2f}"
    # Mixed: one wins, one loses → directional bet
    eff = american_from_win_risk(better, abs(worse))
    if eff is None:
        return f"— {odds_str}"
    side = "A" if net_a > net_b else "B"
    eff_r = round(eff)
    eff_str = f"+{eff_r}" if eff_r > 0 else str(eff_r)
    # Base wager: loss if positive odds (underdog), win if negative (favorite)
    base = abs(worse) if eff_r > 0 else better
    return f"${base / 100:.0f} {side} {eff_str}"


class OpportunitiesTable(DataTable):
    """Live-updating arbitrage opportunities table with position data."""

    DEFAULT_CSS = """
    OpportunitiesTable {
        height: 1fr;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._positions: dict[str, EventPositionSummary] = {}

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_column("Event")
        self.add_column("NO-A")
        self.add_column("NO-B")
        self.add_column("Edge")
        self.add_column("Pos-A", width=14)
        self.add_column("Pos-B", width=14)
        self.add_column("Q-A", width=10)
        self.add_column("CPM-A", width=8)
        self.add_column("ETA-A", width=7)
        self.add_column("Q-B", width=10)
        self.add_column("CPM-B", width=8)
        self.add_column("ETA-B", width=7)
        self.add_column("P&L   ")
        self.add_column("Net/Odds", width=20)

    def update_positions(self, summaries: list[EventPositionSummary]) -> None:
        """Store latest position summaries for next table refresh."""
        self._positions = {s.event_ticker: s for s in summaries}

    def refresh_from_scanner(
        self,
        scanner: ArbitrageScanner | None,
        tracker: TopOfMarketTracker | None = None,
    ) -> None:
        """Rebuild table rows from current scanner state + position data."""
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
            edge_str = f"{opp.fee_edge:.1f}¢"

            # Position columns
            pos = self._positions.get(opp.event_ticker)
            if pos is not None:
                total_a = pos.leg_a.filled_count + pos.leg_a.resting_count
                total_b = pos.leg_b.filled_count + pos.leg_b.resting_count
                pos_a = _fmt_pos(pos.leg_a.filled_count, total_a, pos.leg_a.no_price)
                pos_b = _fmt_pos(pos.leg_b.filled_count, total_b, pos.leg_b.no_price)
                q_a = str(pos.leg_a.queue_position) if pos.leg_a.queue_position else "—"
                q_b = str(pos.leg_b.queue_position) if pos.leg_b.queue_position else "—"
                cpm_a = format_cpm(pos.leg_a.cpm, pos.leg_a.cpm_partial)
                cpm_b = format_cpm(pos.leg_b.cpm, pos.leg_b.cpm_partial)
                eta_a = format_eta(pos.leg_a.eta_minutes, pos.leg_a.cpm_partial)
                eta_b = format_eta(pos.leg_b.eta_minutes, pos.leg_b.cpm_partial)
                net = pos.locked_profit_cents - pos.exposure_cents
                pnl = f"{net / 100:.2f}"
                net_odds = _fmt_net_odds(
                    opp.no_a, opp.no_b,
                    pos.leg_a.filled_count, pos.leg_b.filled_count,
                    pos.leg_a.total_fill_cost, pos.leg_b.total_fill_cost,
                )
            else:
                pos_a = "—"
                pos_b = "—"
                q_a = "—"
                q_b = "—"
                cpm_a = "—"
                cpm_b = "—"
                eta_a = "—"
                eta_b = "—"
                pnl = "—"
                net_odds = _fmt_net_odds(opp.no_a, opp.no_b)

            # Top-of-market warning (applies regardless of position data)
            if tracker is not None:
                if tracker.is_at_top(opp.ticker_a) is False:
                    q_a = f"!! {q_a}"
                if tracker.is_at_top(opp.ticker_b) is False:
                    q_b = f"!! {q_b}"

            row_data = (
                opp.event_ticker,
                _fmt_cents(opp.no_a),
                _fmt_cents(opp.no_b),
                edge_str,
                pos_a,
                pos_b,
                q_a,
                cpm_a,
                eta_a,
                q_b,
                cpm_b,
                eta_b,
                pnl,
                net_odds,
            )
            if opp.event_ticker in current_keys:
                for col_idx, value in enumerate(row_data):
                    col_key = self.ordered_columns[col_idx].key
                    self.update_cell(opp.event_ticker, col_key, value)
            else:
                self.add_row(*row_data, key=opp.event_ticker)


class AccountPanel(Static):
    """Displays account balance."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._balance_text = "Cash: —\nPortfolio: —"

    def on_mount(self) -> None:
        self._render_content()

    def update_balance(self, balance_cents: int, portfolio_cents: int) -> None:
        """Update the balance display."""
        self._balance_text = (
            f"Cash:      ${balance_cents / 100:,.2f}\nPortfolio: ${portfolio_cents / 100:,.2f}"
        )
        self._render_content()

    def _render_content(self) -> None:
        self.update(f"ACCOUNT\n\n{self._balance_text}")


class OrderLog(Static):
    """Scrollable log of recent orders."""

    STATUS_ICONS = {
        "executed": "✓",
        "resting": "◷",
        "canceled": "✗",
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
            pos_str = f"  #{queue_pos}" if queue_pos else ""
            lines.append(
                f"  {order['time']}  BUY {side} {order['ticker']}  "
                f"{order['price']}¢  {filled}/{total}  {remaining} resting  {icon}{pos_str}"
            )
        self.update("ORDERS\n\n" + "\n".join(lines))
