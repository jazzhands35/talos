"""Dashboard widgets for Talos TUI."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from rich.segment import Segment
from rich.style import Style as RichStyle
from rich.text import Text as RichText
from textual.widgets import DataTable, RichLog, Static

from talos.cpm import format_cpm, format_eta
from talos.fees import fee_adjusted_cost
from talos.game_status import ESTIMATED_DETAIL, GameStatus
from talos.models.position import EventPositionSummary
from talos.scanner import ArbitrageScanner
from talos.top_of_market import TopOfMarketTracker
from talos.ui.theme import BLUE, GREEN, PEACH, RED, SUBTEXT0, SURFACE0, SURFACE2, YELLOW


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
    "KXNCAAMLAXGAME": ("LAX", "NCAA"),
    "KXPREMDARTS": ("DRT", "PDC"),
    "KXCHESSWORLDCHAMPION": ("CHS", "WCC"),
    "KXCHESSCANDIDATES": ("CHS", "CAND"),
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
    is_estimate = status.detail == ESTIMATED_DETAIL
    prefix = "~" if is_estimate else ""
    now = datetime.now(UTC)
    delta = status.scheduled_start - now
    minutes_left = int(delta.total_seconds() / 60)
    if 0 < minutes_left <= 15:
        return RichText(f"{prefix}in {minutes_left}m", style=YELLOW, justify="right")
    pt = status.scheduled_start.astimezone(_PT)
    time_str = pt.strftime("%I:%M %p").lstrip("0")  # Windows-compatible, no leading zero
    return RichText(f"{prefix}{time_str}", justify="right")


def _fmt_status(status: str) -> RichText:
    """Format status with icon and color for at-a-glance scanning."""
    if not status:
        return DIM_DASH

    status_styles: list[tuple[str, str, str]] = [
        ("EXITING", "\u25f7", PEACH),
        ("EXIT", "\u2716", PEACH),
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


def _fmt_freshness(age_seconds: float | None) -> RichText:
    """Format freshness dot based on seconds since last WS update.

    Uses markup-style spans so the dot color survives cursor highlight.
    """
    if age_seconds is None:
        return RichText("○", style="dim", justify="center")
    if age_seconds < 5.0:
        color = GREEN
    elif age_seconds < 30.0:
        color = YELLOW
    else:
        color = RED
    t = RichText(justify="center")
    t.append("●", style=RichStyle(color=color, bold=True))
    return t


def _fmt_pnl_with_roi(pnl_cents: int, invested_cents: int) -> str:
    """Format P&L with ROI percentage: '$6.40 (4.1%)'."""
    dollars = pnl_cents / 100
    label = f"${dollars:.2f}" if dollars >= 0 else f"-${abs(dollars):.2f}"
    if invested_cents > 0:
        roi = (pnl_cents / invested_cents) * 100
        label += f" ({roi:.1f}%)"
    return label


class OpportunitiesTable(DataTable):
    """Live-updating arbitrage opportunities table with position data."""

    DEFAULT_CSS = """
    OpportunitiesTable {
        height: 1fr;
    }
    """

    # Column index -> sort key extractor from (opp, positions, volumes, resolver, labels)
    _SORT_KEYS: dict[int, str] = {
        1: "label",  # Team name (col 1) — sorts by event label
        2: "league",  # Lg (col 2)
        3: "state",  # Game (col 3)
        4: "no_a",  # NO (col 4) — sorts by leg A price
        5: "vol_a",  # Vol (col 5)
        10: "fee_edge",  # Edge (col 10)
    }

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._positions: dict[str, EventPositionSummary] = {}
        self._labels: dict[str, str] = {}
        self._leg_labels: dict[str, tuple[str, str]] = {}
        self._resolver: Any = None
        self._volumes_24h: dict[str, int] = {}
        self._event_statuses: dict[str, str] = {}
        self._sort_col: int | None = None
        self._sort_reverse: bool = True
        self._needs_resort: bool = False
        self._dirty_events: set[str] = set()  # event tickers with changes since last render
        self._all_dirty: bool = True  # first render rebuilds everything
        self._freshness: dict[str, float | None] = {}  # market_ticker -> age in seconds

    def set_resolver(self, resolver: Any) -> None:
        """Set the game status resolver for Date/Game columns."""
        self._resolver = resolver

    def update_volumes(self, volumes: dict[str, int]) -> None:
        """Store 24h volume data keyed by market ticker."""
        self._volumes_24h = volumes

    def update_statuses(self, statuses: dict[str, str]) -> None:
        """Store event status strings for all monitored events."""
        self._event_statuses = statuses

    def update_freshness(self, ages: dict[str, float | None]) -> None:
        """Store per-market freshness ages for next render."""
        self._freshness = ages

    def update_leg_labels(self, labels: dict[str, tuple[str, str]]) -> None:
        """Store per-event (team_a, team_b) labels."""
        self._leg_labels = labels

    def mark_dirty(self, event_ticker: str) -> None:
        """Mark an event as needing a table row refresh."""
        self._dirty_events.add(event_ticker)

    _SEP_STYLE = RichStyle(color=SURFACE2)
    _PAIR_BG = RichStyle(bgcolor=SURFACE0)

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = False  # We handle pair striping ourselves
        r = "right"
        c = "center"
        self.add_column(RichText("", justify=c), width=2)  # 0: Freshness dot
        self.add_column("Team")  # 1: Team name
        self.add_column("Lg", width=5)  # 2: League
        self.add_column(RichText("Game", justify=r), width=9)  # 3: Game status
        self.add_column(RichText("NO", justify=r), width=5)  # 4: NO price
        self.add_column(RichText("Vol", justify=r), width=6)  # 5: Volume
        self.add_column(RichText("Pos", justify=r), width=14)  # 6: Position
        self.add_column(RichText("Queue", justify=r), width=6)  # 7: Queue position
        self.add_column(RichText("CPM", justify=r), width=8)  # 8: Contracts/min
        self.add_column(RichText("ETA", justify=r), width=7)  # 9: Est. time to fill
        self.add_column(RichText("Edge", justify=r), width=6)  # 10: Fee-adjusted edge
        self.add_column("Status", width=16)  # 11: Event status
        self.add_column(RichText("Locked", justify=r), width=10)  # 12: Locked profit
        self.add_column(RichText("Expos", justify=r), width=10)  # 13: Exposure

    def _get_row_style(self, row_index: int, base_style: RichStyle) -> RichStyle:  # type: ignore[override]
        """Pair striping: alternate background per event pair (every 2 rows)."""
        if row_index < 0:
            return super()._get_row_style(row_index, base_style)
        pair_index = row_index // 2
        if pair_index % 2:
            return base_style + self._PAIR_BG
        return base_style

    _DIVIDER_STYLE = RichStyle(overline=True)

    def _render_line_in_row(  # type: ignore[override]
        self,
        row_key: Any,
        line_no: int,
        base_style: Any,
        cursor_location: Any,
        hover_location: Any,
    ) -> Any:
        """Vertical column dividers + overline separator between event pairs."""
        fixed, scrollable = super()._render_line_in_row(
            row_key, line_no, base_style, cursor_location, hover_location,
        )
        col_count = len(self.ordered_columns)
        if col_count < 2:
            return fixed, scrollable

        # Vertical column separators
        sep = [Segment("\u2502", self._SEP_STYLE)]
        result = [scrollable[0]]
        for i in range(1, col_count):
            result.append(sep)
            result.append(scrollable[i])
        for i in range(col_count, len(scrollable)):
            result.append(scrollable[i])

        # Overline on :a rows (except the very first) to separate event pairs
        key_str = str(row_key.value) if row_key.value is not None else ""
        if key_str.endswith(":a") and line_no == 0:
            row_index = self._row_locations.get(row_key)
            if row_index is None:
                row_index = 0
            if row_index > 0:
                result = [
                    [
                        Segment(
                            seg.text,
                            (seg.style or RichStyle()) + self._DIVIDER_STYLE,
                            seg.control,
                        )
                        for seg in cell
                    ]
                    for cell in result
                ]

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
            labels = self._leg_labels.get(opp.event_ticker)
            if labels:
                return labels[0].lower()
            return (self._labels.get(opp.event_ticker) or opp.event_ticker).lower()
        if key_name == "league":
            prefix = opp.event_ticker.split("-")[0]
            pair = _SPORT_LEAGUE.get(prefix, ("~", "~"))
            return pair[1].lower()
        if key_name == "no_a":
            return opp.no_a
        if key_name == "fee_edge":
            return opp.fee_edge
        if key_name == "vol_a":
            return self._volumes_24h.get(opp.ticker_a, 0)
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
        current_events = {
            str(k.value).rsplit(":", 1)[0]
            for k in self.rows
            if k.value is not None and ":" in str(k.value)
        }
        new_events = set(all_snaps.keys())

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
                    row1, row2 = self._build_row_pair(opp, tracker)
                    self.add_row(*row1, key=f"{opp.event_ticker}:a")
                    self.add_row(*row2, key=f"{opp.event_ticker}:b")
            return

        sorted_opps = sorted(all_snaps.values(), key=lambda o: o.raw_edge, reverse=True)

        dirty = self._dirty_events
        all_dirty = self._all_dirty
        self._dirty_events = set()
        self._all_dirty = False

        with self.app.batch_update():
            # Remove events no longer tracked
            for evt in current_events - new_events:
                self.remove_row(f"{evt}:a")
                self.remove_row(f"{evt}:b")

            for opp in sorted_opps:
                key_a = f"{opp.event_ticker}:a"
                key_b = f"{opp.event_ticker}:b"
                is_new = opp.event_ticker not in current_events

                if is_new:
                    row1, row2 = self._build_row_pair(opp, tracker)
                    self.add_row(*row1, key=key_a)
                    self.add_row(*row2, key=key_b)
                elif all_dirty or opp.event_ticker in dirty:
                    row1, row2 = self._build_row_pair(opp, tracker)
                    # Update row A
                    old_a = self.get_row(key_a)
                    for col_idx, value in enumerate(row1):
                        if col_idx < len(old_a) and str(old_a[col_idx]) == str(value):
                            continue
                        col_key = self.ordered_columns[col_idx].key
                        self.update_cell(key_a, col_key, value)
                    # Update row B
                    old_b = self.get_row(key_b)
                    for col_idx, value in enumerate(row2):
                        if col_idx < len(old_b) and str(old_b[col_idx]) == str(value):
                            continue
                        col_key = self.ordered_columns[col_idx].key
                        self.update_cell(key_b, col_key, value)

    def _build_row_pair(self, opp: Any, tracker: TopOfMarketTracker | None) -> tuple[tuple, tuple]:
        """Build two row tuples (row1=team_a, row2=team_b) for one event."""
        # Team names
        team_a, team_b = self._leg_labels.get(opp.event_ticker, (opp.ticker_a, opp.ticker_b))

        # Freshness dots
        dot_a = _fmt_freshness(self._freshness.get(opp.ticker_a))
        dot_b = _fmt_freshness(self._freshness.get(opp.ticker_b))

        # Edge
        edge_str = _fmt_edge(opp.fee_edge)

        # Per-leg price and volume
        no_a = _fmt_cents(opp.no_a)
        no_b = _fmt_cents(opp.no_b)
        vol_a = _fmt_vol(self._volumes_24h.get(opp.ticker_a, 0))
        vol_b = _fmt_vol(self._volumes_24h.get(opp.ticker_b, 0))

        # Game status (row 1 only)
        prefix = opp.event_ticker.split("-")[0]
        _, league = _SPORT_LEAGUE.get(prefix, ("—", "—"))
        game_status = self._resolver.get(opp.event_ticker) if self._resolver else None
        game_col = _fmt_game_status(game_status)

        # Position data
        pos = self._positions.get(opp.event_ticker)
        if pos is not None:
            total_a = pos.leg_a.filled_count + pos.leg_a.resting_count
            total_b = pos.leg_b.filled_count + pos.leg_b.resting_count
            pos_a = _fmt_pos(pos.leg_a.filled_count, total_a, pos.leg_a.no_price)
            pos_b = _fmt_pos(pos.leg_b.filled_count, total_b, pos.leg_b.no_price)

            # Highlight imbalanced legs
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

            q_a = (
                RichText(str(pos.leg_a.queue_position), justify="right")
                if pos.leg_a.queue_position is not None
                else DIM_DASH
            )
            q_b = (
                RichText(str(pos.leg_b.queue_position), justify="right")
                if pos.leg_b.queue_position is not None
                else DIM_DASH
            )
            cpm_a = RichText(format_cpm(pos.leg_a.cpm, pos.leg_a.cpm_partial), justify="right")
            cpm_b = RichText(format_cpm(pos.leg_b.cpm, pos.leg_b.cpm_partial), justify="right")
            eta_a = RichText(
                format_eta(pos.leg_a.eta_minutes, pos.leg_a.cpm_partial), justify="right"
            )
            eta_b = RichText(
                format_eta(pos.leg_b.eta_minutes, pos.leg_b.cpm_partial), justify="right"
            )

            # Locked and Exposure
            locked = pos.locked_profit_cents
            if locked > 0:
                locked_str = RichText(f"${locked / 100:.2f}", style=GREEN, justify="right")
            elif locked == 0:
                locked_str = DIM_DASH
            else:
                locked_str = RichText(f"-${abs(locked) / 100:.2f}", style=RED, justify="right")

            exposure = pos.exposure_cents
            if exposure > 0:
                exposure_str = RichText(f"${exposure / 100:.2f}", style=RED, justify="right")
            else:
                exposure_str = DIM_DASH

            status = _fmt_status(pos.status)
        else:
            pos_a = pos_b = q_a = q_b = DIM_DASH
            cpm_a = cpm_b = eta_a = eta_b = DIM_DASH
            locked_str = exposure_str = DIM_DASH
            status = _fmt_status(self._event_statuses.get(opp.event_ticker, ""))

        if tracker is not None:
            if tracker.is_at_top(opp.ticker_a) is False:
                q_a = RichText(f"!! {q_a}", style=YELLOW, justify="right")
            if tracker.is_at_top(opp.ticker_b) is False:
                q_b = RichText(f"!! {q_b}", style=YELLOW, justify="right")

        # Row 1: team A + shared event-level info
        row1 = (
            dot_a,
            team_a,
            league,
            game_col,
            no_a,
            vol_a,
            pos_a,
            q_a,
            cpm_a,
            eta_a,
            edge_str,
            status,
            locked_str,
            exposure_str,
        )

        # Row 2: team B only — shared columns blank
        row2 = (
            dot_b,
            team_b,
            "",
            "",
            no_b,
            vol_b,
            pos_b,
            q_b,
            cpm_b,
            eta_b,
            "",
            "",
            "",
            "",
        )

        return row1, row2


class PortfolioPanel(Static):
    """Portfolio summary: cash, locked, exposure, invested, historical P&L."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self._cash: int = 0
        self._portfolio: int = 0
        self._locked: float = 0.0
        self._exposure: int = 0
        self._invested: int = 0
        self._pnl_today: int = 0
        self._pnl_yesterday: int = 0
        self._pnl_7d: int = 0
        self._invested_today: int = 0
        self._invested_yesterday: int = 0
        self._invested_7d: int = 0

    def on_mount(self) -> None:
        self.border_title = "Portfolio"

    def render(self) -> str:
        """Compute content each frame — bypasses Static.update() entirely."""
        cash = f"${self._cash / 100:,.2f}"
        locked = f"${self._locked / 100:,.2f}"
        exposure = f"${self._exposure / 100:,.2f}"
        invested = f"${self._invested / 100:,.2f}"
        today = _fmt_pnl_with_roi(self._pnl_today, self._invested_today)
        yesterday = _fmt_pnl_with_roi(self._pnl_yesterday, self._invested_yesterday)
        last_7d = _fmt_pnl_with_roi(self._pnl_7d, self._invested_7d)
        return (
            f"Cash:      {cash}\n"
            f"Locked In: {locked}\n"
            f"Exposure:  {exposure}\n"
            f"Invested:  {invested}\n"
            f"───────────────────\n"
            f"Today:     {today}\n"
            f"Yesterday: {yesterday}\n"
            f"Last 7d:   {last_7d}"
        )

    def update_balance(self, balance_cents: int, portfolio_cents: int) -> None:
        self._cash = balance_cents
        self._portfolio = portfolio_cents
        self.refresh()

    def update_portfolio_summary(
        self,
        locked: float,
        exposure: int,
        invested: int,
    ) -> None:
        self._locked = locked
        self._exposure = exposure
        self._invested = invested
        self.refresh()

    def update_pnl(
        self,
        today: int,
        yesterday: int,
        last_7d: int,
        invested_today: int = 0,
        invested_yesterday: int = 0,
        invested_7d: int = 0,
    ) -> None:
        self._pnl_today = today
        self._pnl_yesterday = yesterday
        self._pnl_7d = last_7d
        self._invested_today = invested_today
        self._invested_yesterday = invested_yesterday
        self._invested_7d = invested_7d
        self.refresh()


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


_SEVERITY_STYLE = {
    "information": RichStyle(color=SUBTEXT0),
    "warning": RichStyle(color=YELLOW),
    "error": RichStyle(color=RED, bold=True),
}


class ActivityLog(RichLog):
    """Scrollable activity log for automated engine notifications.

    Uses RichLog (no widget-per-message overhead) instead of Textual toasts
    to prevent asyncio task accumulation from freezing the event loop.
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._plain_lines: list[str] = []

    def on_mount(self) -> None:
        self.border_title = "Activity"

    def log_activity(self, message: str, severity: str = "information") -> None:
        """Append a timestamped, color-coded message."""
        now = datetime.now(UTC)
        ts = now.strftime("%H:%M:%S")
        style = _SEVERITY_STYLE.get(severity, _SEVERITY_STYLE["information"])
        line = RichText()
        line.append(f"  {ts}  ", style=RichStyle(color=SURFACE2))
        line.append(message, style=style)
        self.write(line)
        self._plain_lines.append(f"{ts}  {message}")
        # Keep buffer bounded
        if len(self._plain_lines) > 500:
            self._plain_lines = self._plain_lines[-500:]

    def get_plain_text(self) -> str:
        """Return all log lines as plain text for clipboard copy."""
        return "\n".join(self._plain_lines)
