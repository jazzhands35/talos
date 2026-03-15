"""Dashboard widgets for Talos TUI."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from rich.segment import Segment
from rich.style import Style as RichStyle
from rich.text import Text as RichText
from textual.widgets import DataTable, Static

from talos.cpm import format_cpm, format_eta
from talos.fees import fee_adjusted_cost
from talos.game_status import GameStatus
from talos.models.position import EventPositionSummary
from talos.scanner import ArbitrageScanner
from talos.top_of_market import TopOfMarketTracker
from talos.ui.theme import BLUE, GREEN, PEACH, RED, SURFACE2, YELLOW


def _fmt_cents(value: int) -> RichText:
    """Format an integer cents value as 'XX¢', right-aligned."""
    return RichText(f"{value}¢", justify="right")


def _fmt_edge(fee_edge: float) -> RichText:
    """Format fee-adjusted edge: green if positive, dim otherwise."""
    label = f"{fee_edge:.1f}¢"
    if fee_edge > 0:
        return RichText(label, style=GREEN, justify="right")
    return RichText(label, style="dim", justify="right")


def _fmt_pnl(net_cents: float, kalshi_pnl: int | None = None) -> RichText:
    """Format P&L in dollars: green positive, red negative.

    When Kalshi's realized_pnl is non-zero, append it as 'k$X.XX' suffix.
    """
    dollars = net_cents / 100
    if dollars >= 0:
        label = f"${dollars:.2f}"
        style = GREEN
    else:
        label = f"-${abs(dollars):.2f}"
        style = RED
    if kalshi_pnl is not None and kalshi_pnl != 0:
        k_dollars = kalshi_pnl / 100
        k_str = f"${k_dollars:.2f}" if k_dollars >= 0 else f"-${abs(k_dollars):.2f}"
        label = f"{label} k{k_str}"
    return RichText(label, style=style, justify="right")


def _fmt_pos(filled: int, total: int, avg_no_price: int) -> RichText:
    """Format position as 'filled/total avg¢' with fee-adjusted cost."""
    if total == 0:
        return DIM_DASH
    if filled == 0:
        return RichText(f"0/{total}", justify="right")
    fee_avg = fee_adjusted_cost(avg_no_price)
    return RichText(f"{filled}/{total} {fee_avg:.1f}¢", justify="right")


DIM_DASH = RichText("—", style="dim", justify="right")

# Series prefix -> (Sport abbr, League abbr)
_SPORT_LEAGUE: dict[str, tuple[str, str]] = {
    "KXNHLGAME": ("HOC", "NHL"),
    "KXNBAGAME": ("BKB", "NBA"),
    "KXMLBGAME": ("BSB", "MLB"),
    "KXNFLGAME": ("FTB", "NFL"),
    "KXWNBAGAME": ("BKB", "WNBA"),
    "KXCFBGAME": ("FTB", "NCAAF"),
    "KXCBBGAME": ("BKB", "NCAAB"),
    "KXMLSGAME": ("SOC", "MLS"),
    "KXEPLGAME": ("SOC", "EPL"),
    "KXAHLGAME": ("HOC", "AHL"),
    "KXLOLGAME": ("ESP", "LoL"),
    "KXCS2GAME": ("ESP", "CS2"),
    "KXVALGAME": ("ESP", "VAL"),
    "KXDOTA2GAME": ("ESP", "DOTA"),
    "KXCODGAME": ("ESP", "COD"),
    "KXATPMATCH": ("TEN", "ATP"),
    "KXATPDOUBLES": ("TEN", "ATP"),
    "KXATPCHALLENGERMATCH": ("TEN", "ATPC"),
    "KXWTACHALLENGERMATCH": ("TEN", "WTAC"),
    "KXWTAMATCH": ("TEN", "WTA"),
    "KXLALIGAGAME": ("SOC", "LIGA"),
    "KXBUNDESLIGAGAME": ("SOC", "BUN"),
    "KXSERIEAGAME": ("SOC", "SA"),
    "KXLIGUE1GAME": ("SOC", "L1"),
    "KXUCLGAME": ("SOC", "UCL"),
    "KXLIGAMXGAME": ("SOC", "LMX"),
    "KXSHLGAME": ("HOC", "SHL"),
    "KXKHLGAME": ("HOC", "KHL"),
    "KXEUROLEAGUEGAME": ("BKB", "EURO"),
    "KXNBLGAME": ("BKB", "NBL"),
    "KXBBLGAME": ("BKB", "BBL"),
    "KXCBAGAME": ("BKB", "CBA"),
    "KXKBLGAME": ("BKB", "KBL"),
    "KXKLEAGUEGAME": ("SOC", "KLG"),
    "KXUFCFIGHT": ("MMA", "UFC"),
    "KXBOXING": ("BOX", "BOX"),
    "KXT20MATCH": ("CRK", "T20"),
    "KXIPL": ("CRK", "IPL"),
    "KXCRICKETODIMATCH": ("CRK", "ODI"),
    "KXRUGBYNRLMATCH": ("RUG", "NRL"),
    "KXAFLGAME": ("AFL", "AFL"),
    "KXF1": ("MOT", "F1"),
    "KXNASCARRACE": ("MOT", "NASC"),
    "KXINDYCARRACE": ("MOT", "INDY"),
    "KXPGATOUR": ("GLF", "PGA"),
    "KXIWMEN": ("TEN", "IW-M"),
    "KXIWWMN": ("TEN", "IW-W"),
}


def _fmt_vol(volume: int) -> RichText:
    """Format 24h volume as compact number."""
    if volume == 0:
        return DIM_DASH
    if volume >= 1000:
        label = f"{volume / 1000:.1f}k"
    else:
        label = str(volume)
    return RichText(label, justify="right")


_PT = ZoneInfo("America/Los_Angeles")


def _fmt_game_date(scheduled_start: datetime | None) -> RichText:
    """Format game date as MM/DD in Pacific Time."""
    if scheduled_start is None:
        return DIM_DASH
    pt = scheduled_start.astimezone(_PT)
    return RichText(pt.strftime("%m/%d"), justify="right")


def _fmt_game_status(status: GameStatus | None) -> RichText:
    """Format game state for the Game column."""
    if status is None or status.state == "unknown":
        return DIM_DASH
    if status.state == "post":
        return RichText("FINAL", style="dim", justify="right")
    if status.state == "live":
        label = f"LIVE {status.detail}".strip()
        return RichText(label, style=GREEN, justify="right")
    # state == "pre"
    if status.scheduled_start is None:
        return DIM_DASH
    now = datetime.now(UTC)
    delta = status.scheduled_start - now
    minutes_left = int(delta.total_seconds() / 60)
    if minutes_left <= 15:
        return RichText(f"in {minutes_left}m", style=YELLOW, justify="right")
    pt = status.scheduled_start.astimezone(_PT)
    time_str = pt.strftime("%I:%M %p").lstrip("0")  # Windows-compatible, no leading zero
    return RichText(time_str, justify="right")


def _fmt_status(status: str) -> RichText:
    """Format status with icon and color for at-a-glance scanning."""
    if not status:
        return DIM_DASH

    status_styles: list[tuple[str, str, str]] = [
        ("Low edge", "\u25cb", "dim"),
        ("Unstable", "\u25cb", "dim"),
        ("Sug. off", "\u25cb", "dim"),
        ("Ready", "\u25cb", "dim"),
        ("Stable", "\u25cb", "dim"),
        ("Cooldown", "\u25cb", "dim"),
        ("Proposed", "\u25ce", BLUE),
        ("Resting", "\u25f7", YELLOW),
        ("Bidding", "\u25f7", YELLOW),
        ("Jumped", "\u25f7", PEACH),
        ("Filling", "\u25d0", BLUE),
        ("Waiting", "\u25d0", BLUE),
        ("Need bid", "\u25d0", BLUE),
        ("Locked", "\u2713", GREEN),
        ("Imbalanced", "\u26a0", YELLOW),
        ("Discrepancy", "\u26a0", RED),
    ]

    for prefix, icon, color in status_styles:
        if status.startswith(prefix):
            return RichText(f"{icon} {status}", style=color)

    return RichText(status)


class OpportunitiesTable(DataTable):
    """Live-updating arbitrage opportunities table with position data."""

    DEFAULT_CSS = """
    OpportunitiesTable {
        height: 1fr;
    }
    """

    # Column index -> sort key extractor from (opp, positions, volumes, resolver, labels)
    _SORT_KEYS: dict[int, str] = {
        0: "label",      # Event name
        1: "sport",      # Sport
        2: "league",     # League
        3: "no_a",       # NO-A price
        4: "no_b",       # NO-B price
        5: "fee_edge",   # Edge
        6: "vol_a",      # V-A (24h volume)
        7: "vol_b",      # V-B (24h volume)
        8: "start",      # Date
        9: "state",      # Game status
    }

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._positions: dict[str, EventPositionSummary] = {}
        self._labels: dict[str, str] = {}
        self._resolver: Any = None
        self._volumes_24h: dict[str, int] = {}
        self._event_statuses: dict[str, str] = {}
        self._sort_col: int | None = None
        self._sort_reverse: bool = True
        self._needs_resort: bool = False
        self._dirty_events: set[str] = set()  # event tickers with changes since last render
        self._all_dirty: bool = True  # first render rebuilds everything

    def set_resolver(self, resolver: Any) -> None:
        """Set the game status resolver for Date/Game columns."""
        self._resolver = resolver

    def update_volumes(self, volumes: dict[str, int]) -> None:
        """Store 24h volume data keyed by market ticker."""
        self._volumes_24h = volumes

    def update_statuses(self, statuses: dict[str, str]) -> None:
        """Store event status strings for all monitored events."""
        self._event_statuses = statuses

    def mark_dirty(self, event_ticker: str) -> None:
        """Mark an event as needing a table row refresh."""
        self._dirty_events.add(event_ticker)

    _SEP_STYLE = RichStyle(color=SURFACE2)

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        r = "right"
        self.add_column("Event")
        self.add_column("Spt", width=4)
        self.add_column("Lg", width=5)
        self.add_column(RichText("NO-A", justify=r))
        self.add_column(RichText("NO-B", justify=r))
        self.add_column(RichText("Edge", justify=r))
        self.add_column(RichText("V-A", justify=r), width=6)
        self.add_column(RichText("V-B", justify=r), width=6)
        self.add_column(RichText("Date", justify=r), width=6)
        self.add_column(RichText("Game", justify=r), width=9)
        self.add_column(RichText("Pos-A", justify=r), width=14)
        self.add_column(RichText("Pos-B", justify=r), width=14)
        self.add_column(RichText("Q-A", justify=r), width=10)
        self.add_column(RichText("CPM-A", justify=r), width=8)
        self.add_column(RichText("ETA-A", justify=r), width=7)
        self.add_column(RichText("Q-B", justify=r), width=10)
        self.add_column(RichText("CPM-B", justify=r), width=8)
        self.add_column(RichText("ETA-B", justify=r), width=7)
        self.add_column("Status", width=16)
        self.add_column(RichText("P&L", justify=r), width=16)

    def _render_line_in_row(  # type: ignore[override]
        self, *args: Any, **kwargs: Any
    ) -> Any:
        """Insert faint vertical dividers between columns."""
        fixed, scrollable = super()._render_line_in_row(*args, **kwargs)
        col_count = len(self.ordered_columns)
        if col_count < 2:
            return fixed, scrollable
        sep = [Segment("\u2502", self._SEP_STYLE)]
        result = [scrollable[0]]
        for i in range(1, col_count):
            result.append(sep)
            result.append(scrollable[i])
        for i in range(col_count, len(scrollable)):
            result.append(scrollable[i])
        return fixed, result

    def toggle_sort(self, col_idx: int) -> None:
        """Sort rows by column — one-time reorder on click."""
        if col_idx == self._sort_col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col_idx
            self._sort_reverse = True
        self._needs_resort = True

    def _sort_key(self, opp: Any) -> Any:
        """Return a comparable sort value for the current sort column."""
        col = self._sort_col
        if col is None:
            return opp.raw_edge  # default sort; reverse=True gives descending
        key_name = self._SORT_KEYS.get(col)
        if key_name == "label":
            return (self._labels.get(opp.event_ticker) or opp.event_ticker).lower()
        if key_name == "sport" or key_name == "league":
            prefix = opp.event_ticker.split("-")[0]
            pair = _SPORT_LEAGUE.get(prefix, ("~", "~"))
            return pair[0].lower() if key_name == "sport" else pair[1].lower()
        if key_name == "no_a":
            return opp.no_a
        if key_name == "no_b":
            return opp.no_b
        if key_name == "fee_edge":
            return opp.fee_edge
        if key_name == "vol_a":
            return self._volumes_24h.get(opp.ticker_a, 0)
        if key_name == "vol_b":
            return self._volumes_24h.get(opp.ticker_b, 0)
        if key_name == "start":
            gs = self._resolver.get(opp.event_ticker) if self._resolver else None
            if gs and gs.scheduled_start:
                return gs.scheduled_start.timestamp()
            return 0.0
        if key_name == "state":
            gs = self._resolver.get(opp.event_ticker) if self._resolver else None
            order = {"live": 0, "pre": 1, "post": 2, "unknown": 3}
            return order.get(gs.state, 3) if gs else 3
        # Unsupported column — fall back to edge
        return opp.raw_edge

    def update_positions(self, summaries: list[EventPositionSummary]) -> None:
        """Store latest position summaries for next table refresh."""
        self._positions = {s.event_ticker: s for s in summaries}

    def update_labels(self, labels: dict[str, str]) -> None:
        """Store event ticker -> short display label mapping."""
        self._labels = labels

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

        # On sort click: clear and re-add in sorted order
        if self._needs_resort:
            self._needs_resort = False
            sorted_opps = sorted(
                all_snaps.values(),
                key=self._sort_key,
                reverse=self._sort_reverse,
            )
            with self.app.batch_update():
                self.clear()
                for opp in sorted_opps:
                    self.add_row(*self._build_row(opp, tracker), key=opp.event_ticker)
            return

        # Normal refresh: update in place, preserving row order and highlight
        sorted_opps = sorted(all_snaps.values(), key=lambda o: o.raw_edge, reverse=True)

        dirty = self._dirty_events
        all_dirty = self._all_dirty
        self._dirty_events = set()
        self._all_dirty = False

        with self.app.batch_update():
            for key in current_keys - new_keys:
                if key is not None:
                    self.remove_row(key)
            for opp in sorted_opps:
                is_new = opp.event_ticker not in current_keys
                if is_new:
                    self.add_row(*self._build_row(opp, tracker), key=opp.event_ticker)
                elif all_dirty or opp.event_ticker in dirty:
                    # Only rebuild rows that changed — skip unchanged cells
                    row_data = self._build_row(opp, tracker)
                    old_row = self.get_row(opp.event_ticker)
                    for col_idx, value in enumerate(row_data):
                        if col_idx < len(old_row) and str(old_row[col_idx]) == str(value):
                            continue
                        col_key = self.ordered_columns[col_idx].key
                        self.update_cell(opp.event_ticker, col_key, value)


    def _build_row(
        self, opp: Any, tracker: TopOfMarketTracker | None
    ) -> tuple:
        """Build the full row_data tuple for one opportunity."""
        edge_str = _fmt_edge(opp.fee_edge)

        pos = self._positions.get(opp.event_ticker)
        if pos is not None:
            total_a = pos.leg_a.filled_count + pos.leg_a.resting_count
            total_b = pos.leg_b.filled_count + pos.leg_b.resting_count
            pos_a = _fmt_pos(pos.leg_a.filled_count, total_a, pos.leg_a.no_price)
            pos_b = _fmt_pos(pos.leg_b.filled_count, total_b, pos.leg_b.no_price)

            fa, fb = pos.leg_a.filled_count, pos.leg_b.filled_count
            ra, rb = pos.leg_a.resting_count, pos.leg_b.resting_count
            ta, tb = fa + ra, fb + rb
            if fa != fb:
                if fa < fb:
                    pos_a = RichText(str(pos_a), style=YELLOW, justify="right")
                else:
                    pos_b = RichText(str(pos_b), style=YELLOW, justify="right")
            elif ta != tb:
                if ta < tb:
                    pos_a = RichText(str(pos_a), style=YELLOW, justify="right")
                else:
                    pos_b = RichText(str(pos_b), style=YELLOW, justify="right")

            q_a = RichText(str(pos.leg_a.queue_position), justify="right") if pos.leg_a.queue_position else DIM_DASH
            q_b = RichText(str(pos.leg_b.queue_position), justify="right") if pos.leg_b.queue_position else DIM_DASH
            cpm_a = RichText(format_cpm(pos.leg_a.cpm, pos.leg_a.cpm_partial), justify="right")
            cpm_b = RichText(format_cpm(pos.leg_b.cpm, pos.leg_b.cpm_partial), justify="right")
            eta_a = RichText(format_eta(pos.leg_a.eta_minutes, pos.leg_a.cpm_partial), justify="right")
            eta_b = RichText(format_eta(pos.leg_b.eta_minutes, pos.leg_b.cpm_partial), justify="right")
            net = pos.locked_profit_cents - pos.exposure_cents
            pnl = _fmt_pnl(net, pos.kalshi_pnl)
            status = _fmt_status(pos.status)
        else:
            pos_a = pos_b = q_a = q_b = DIM_DASH
            cpm_a = cpm_b = eta_a = eta_b = DIM_DASH
            pnl = DIM_DASH
            status = _fmt_status(self._event_statuses.get(opp.event_ticker, ""))

        if tracker is not None:
            if tracker.is_at_top(opp.ticker_a) is False:
                q_a = RichText(f"!! {q_a}", style=YELLOW, justify="right")
            if tracker.is_at_top(opp.ticker_b) is False:
                q_b = RichText(f"!! {q_b}", style=YELLOW, justify="right")

        display_name = self._labels.get(opp.event_ticker, opp.event_ticker)
        prefix = opp.event_ticker.split("-")[0]
        sport, league = _SPORT_LEAGUE.get(prefix, ("—", "—"))
        vol_a = _fmt_vol(self._volumes_24h.get(opp.ticker_a, 0))
        vol_b = _fmt_vol(self._volumes_24h.get(opp.ticker_b, 0))
        game_status = self._resolver.get(opp.event_ticker) if self._resolver else None
        game_date = _fmt_game_date(game_status.scheduled_start if game_status else None)
        game_col = _fmt_game_status(game_status)
        return (
            display_name, sport, league,
            _fmt_cents(opp.no_a), _fmt_cents(opp.no_b), edge_str,
            vol_a, vol_b, game_date, game_col,
            pos_a, pos_b, q_a, cpm_a, eta_a, q_b, cpm_b, eta_b,
            status, pnl,
        )


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
