"""Dashboard widgets for Talos TUI."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

from rich.segment import Segment
from rich.style import Style as RichStyle
from rich.text import Text as RichText
from textual.binding import Binding
from textual.widgets import DataTable, RichLog, Static

from talos.cpm import format_cpm, format_eta
from talos.fees import fee_adjusted_cost_bps
from talos.game_status import ESTIMATED_DETAIL, GameStatus
from talos.models.position import EventPositionSummary
from talos.scanner import ArbitrageScanner
from talos.top_of_market import TopOfMarketTracker
from talos.ui.theme import BLUE, GREEN, PEACH, RED, SUBTEXT0, SURFACE0, SURFACE2, YELLOW
from talos.units import ONE_CENT_BPS, bps_to_cents_round


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


def _fmt_pos(
    filled: int, total: int, avg_no_price: int, resting_no_price: int | None = None
) -> RichText:
    """Format position as 'filled/total avg¢' with fee-adjusted cost.

    When a resting order exists, appends '@{price}¢' to show the queued price.
    """
    if total == 0:
        return DIM_DASH
    resting_suffix = ""
    if resting_no_price is not None:
        resting_fee_bps = fee_adjusted_cost_bps(resting_no_price * ONE_CENT_BPS)
        resting_suffix = f" @{resting_fee_bps / ONE_CENT_BPS:.0f}¢"
    if filled == 0:
        return RichText(f"0/{total}{resting_suffix}", justify="right")
    fee_avg_bps = fee_adjusted_cost_bps(avg_no_price * ONE_CENT_BPS)
    return RichText(
        f"{filled}/{total} {fee_avg_bps / ONE_CENT_BPS:.1f}¢{resting_suffix}",
        justify="right",
    )


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

# API category -> short label for non-sports display
_CATEGORY_SHORT: dict[str, str] = {
    "Climate and Weather": "Clim",
    "Crypto": "Cryp",
    "Companies": "Comp",
    "Politics": "Pol",
    "Science and Technology": "Sci",
    "Mentions": "Ment",
    "Entertainment": "Ent",
    "World": "Wrld",
    "Elections": "Elec",
    "Health": "Hlth",
}


def _fmt_vol(volume: int) -> RichText:
    """Format 24h volume as compact number. Shows 0 explicitly (not dash)."""
    if volume == 0:
        return RichText("0", style="dim", justify="right")
    label = f"{volume / 1000:.1f}k" if volume >= 1000 else str(volume)
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
    """Format freshness dot based on age of orderbook information.

    Uses markup-style spans so the dot color survives cursor highlight.

    The stale-book recovery cycle resubscribes every ~120s, triggering
    a fresh snapshot that confirms the book state. Thresholds reflect
    information age, not trading activity:
    - Green: confirmed within the last recovery cycle (~150s)
    - Yellow: missed one recovery cycle (150-360s) — possibly stale
    - Red: missed multiple cycles (>360s) — likely disconnected
    """
    if age_seconds is None:
        return RichText("○", style="dim", justify="center")
    if age_seconds < 150.0:
        color = GREEN
    elif age_seconds < 360.0:
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


class _ColSpec(NamedTuple):
    """Column definition for the opportunities table."""

    key: str  # unique identifier
    label: str  # header text
    width: int | None  # None = auto
    justify: str  # "left", "right", "center"
    compact: bool  # visible in compact mode?
    sort_key: str | None = None  # for sortable columns


# Full table layout — compact mode hides columns with compact=False.
# Widths are tightened vs. the original to save ~14 chars total.
_COL_SPECS: tuple[_ColSpec, ...] = (
    _ColSpec("id", "#", 3, "right", False, "talos_id"),
    _ColSpec("dot", "", 2, "center", False),
    _ColSpec("team", "Team", 14, "left", True, "label"),
    _ColSpec("sport", "Sport", 5, "left", False, "sport"),
    _ColSpec("lg", "Lg", 5, "left", False, "league"),
    _ColSpec("date", "Date", 5, "right", False, "date"),
    _ColSpec("game", "Game", 8, "right", True, "state"),
    _ColSpec("price", "Price", 5, "right", True, "no_a"),
    _ColSpec("vol", "Vol", 6, "right", True, "vol_a"),
    _ColSpec("pos", "Pos", 14, "right", True, "pos"),
    _ColSpec("queue", "Queue", 6, "right", True, "queue"),
    _ColSpec("cpm", "CPM", 7, "right", True, "cpm"),
    _ColSpec("eta", "ETA", 5, "right", True, "eta"),
    _ColSpec("edge", "Edge", 5, "right", True, "fee_edge"),
    _ColSpec("eval", "Eval", 4, "right", True, "eval"),
    _ColSpec("status", "Status", 16, "left", True, "status"),
    _ColSpec("locked", "Locked", 8, "right", True, "locked"),
    _ColSpec("exposure", "Expos", 8, "right", False, "exposure"),
)

_ALL_INDICES = tuple(range(len(_COL_SPECS)))
_COMPACT_INDICES = tuple(i for i, s in enumerate(_COL_SPECS) if s.compact)


class OpportunitiesTable(DataTable):
    """Live-updating arbitrage opportunities table with position data."""

    DEFAULT_CSS = """
    OpportunitiesTable {
        height: 1fr;
    }
    """

    # Horizontal scroll bindings. DataTable's cursor_type="row" disables the
    # arrow-key horizontal navigation path, and Textual shows a 1-cell
    # scrollbar that's hard to hit with a mouse, so on narrow terminals the
    # rightmost columns (status, locked, exposure) become unreachable without
    # these. Kept hidden (show=False) to avoid adding to the already-crowded
    # app footer. Bracket pair = page-scroll, shift+arrow = single-cell.
    BINDINGS = [
        Binding("[", "page_left", "Page Left", show=False),
        Binding("]", "page_right", "Page Right", show=False),
        Binding("shift+left", "scroll_left", show=False),
        Binding("shift+right", "scroll_right", show=False),
    ]

    # How many leading columns to pin in full and compact modes. Full pins
    # ID + dot + Team so the ticker stays visible while scrolling right;
    # compact already hides ID + dot, so we pin Team only.
    _FIXED_COLS_FULL = 3
    _FIXED_COLS_COMPACT = 1

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._positions: dict[str, EventPositionSummary] = {}
        self._talos_ids: dict[str, int] = {}  # event_ticker -> talos_id
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
        self._compact: bool = False
        self._vis_idx: tuple[int, ...] = _ALL_INDICES

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
        self._setup_columns()
        self._apply_fixed_columns()

    def _apply_fixed_columns(self) -> None:
        """Pin the leading identifier columns so they stay visible during
        horizontal scroll. Count depends on the current view mode.
        """
        self.fixed_columns = (
            self._FIXED_COLS_COMPACT if self._compact else self._FIXED_COLS_FULL
        )

    # ── View mode helpers ────────────────────────────────────────────

    def _setup_columns(self) -> None:
        """Add table columns for the current view mode."""
        for i in self._vis_idx:
            spec = _COL_SPECS[i]
            label: str | RichText = (
                RichText(spec.label, justify=spec.justify)  # type: ignore[arg-type]
                if spec.justify != "left"
                else spec.label
            )
            if spec.width is not None:
                self.add_column(label, width=spec.width)
            else:
                self.add_column(label)

    def _build_sort_keys(self) -> dict[int, str]:
        """Map visible column index -> sort key name."""
        result: dict[int, str] = {}
        for vis_idx, full_idx in enumerate(self._vis_idx):
            sk = _COL_SPECS[full_idx].sort_key
            if sk is not None:
                result[vis_idx] = sk
        return result

    def _filter_row(self, full_row: tuple[Any, ...]) -> tuple[Any, ...]:
        """Select only visible columns from a full 18-element row tuple."""
        if not self._compact:
            return full_row
        return tuple(full_row[i] for i in self._vis_idx)

    def set_compact(self, compact: bool) -> None:
        """Switch between full and compact column layouts."""
        if compact == self._compact:
            return
        self._compact = compact
        self._vis_idx = _COMPACT_INDICES if compact else _ALL_INDICES
        self._sort_col = None  # Reset sort on mode change
        self._needs_resort = False
        self.clear(columns=True)
        self._setup_columns()
        self._apply_fixed_columns()
        self._all_dirty = True  # Force full rebuild on next refresh

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
        """Vertical column dividers + overline separator between event pairs.

        Scroll-geometry invariant: Textual crops the ``scrollable`` band at
        ``fixed_width = sum(ordered_columns[:fixed_columns].render_width)``
        and draws everything from that offset onward to the right of the
        pinned band. If we insert separator cells into the first
        ``fixed_columns`` positions of ``scrollable``, the first non-fixed
        cell ends up shifted to the right of the crop boundary, so the
        first horizontal-scroll steps consume duplicated padding instead of
        real content. To avoid that we:

        * leave the leading ``fixed_columns`` cells of ``scrollable``
          unseparated (their sum equals ``fixed_width``),
        * emit no separator at the fixed/scrollable seam (that boundary
          is naturally indicated by the pinned band's right edge), and
        * only insert ``│`` dividers between the remaining scrollable
          cells.

        The ``fixed`` band is returned at its native width for the same
        reason — adding cells to it would widen the pinned render beyond
        the ``fixed_width`` Textual expects, desyncing the crop. The
        trailing extend-style filler segment(s) Textual appends to
        ``scrollable`` are preserved verbatim. The pair-separator overline
        is applied to both bands because it's a style-only modifier and
        does not change segment widths.
        """
        fixed, scrollable = super()._render_line_in_row(
            row_key,
            line_no,
            base_style,
            cursor_location,
            hover_location,
        )
        col_count = len(self.ordered_columns)
        if col_count < 2:
            return fixed, scrollable

        sep = [Segment("\u2502", self._SEP_STYLE)]
        fc = self.fixed_columns or 0

        # Rebuild the scrollable band without shifting the crop boundary.
        scrollable_out: list[Any] = [scrollable[i] for i in range(fc)]
        if fc < col_count:
            scrollable_out.append(scrollable[fc])
            for i in range(fc + 1, col_count):
                scrollable_out.append(sep)
                scrollable_out.append(scrollable[i])
        # Preserve any trailing extend-style filler segments.
        for i in range(col_count, len(scrollable)):
            scrollable_out.append(scrollable[i])

        # Overline on :a rows (except the very first) to separate event
        # pairs. Style-only; preserves the width of both bands.
        fixed_out: list[Any] = list(fixed)
        key_str = str(row_key.value) if row_key.value is not None else ""
        if key_str.endswith(":a") and line_no == 0:
            row_index = self._row_locations.get(row_key)
            if row_index is None:
                row_index = 0
            if row_index > 0:

                def _with_overline(cells: list[Any]) -> list[Any]:
                    return [
                        [
                            Segment(
                                seg.text,
                                (seg.style or RichStyle()) + self._DIVIDER_STYLE,
                                seg.control,
                            )
                            for seg in cell
                        ]
                        for cell in cells
                    ]

                scrollable_out = _with_overline(scrollable_out)
                fixed_out = _with_overline(fixed_out)

        return fixed_out, scrollable_out

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
        key_name = self._build_sort_keys().get(col)
        if key_name == "talos_id":
            return self._talos_ids.get(opp.event_ticker, 0)
        if key_name == "label":
            labels = self._leg_labels.get(opp.event_ticker)
            if labels:
                return labels[0].lower()
            return (self._labels.get(opp.event_ticker) or opp.event_ticker).lower()
        if key_name == "sport":
            prefix = opp.event_ticker.split("-")[0]
            sl = _SPORT_LEAGUE.get(prefix)
            return (sl[0] if sl else prefix).lower()
        if key_name == "league":
            prefix = opp.event_ticker.split("-")[0]
            sl = _SPORT_LEAGUE.get(prefix)
            if sl:
                return sl[1].lower()
            # Non-sports: submarket suffix (e.g., "80" from "KXRT-REA-80")
            parts = opp.event_ticker.split("-")
            return parts[-1].lower() if len(parts) > 1 else ""
        if key_name == "no_a":
            return opp.no_a
        if key_name == "fee_edge":
            return opp.fee_edge
        if key_name == "status":
            return (self._event_statuses.get(opp.event_ticker) or "").lower()
        if key_name == "vol_a":
            return self._volumes_24h.get(opp.ticker_a, 0)
        if key_name == "date":
            gs = self._resolver.get(opp.event_ticker) if self._resolver else None
            if gs and gs.scheduled_start:
                return gs.scheduled_start.timestamp()
            if opp.close_time:
                try:
                    return datetime.fromisoformat(opp.close_time.replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError):
                    pass
            return 0.0
        if key_name == "state":
            gs = self._resolver.get(opp.event_ticker) if self._resolver else None
            order = {"live": 0, "pre": 1, "post": 2, "unknown": 3}
            return order.get(gs.state, 3) if gs else 3
        if key_name == "locked":
            pos = self._positions.get(opp.event_ticker)
            return pos.locked_profit_bps if pos else 0
        if key_name == "exposure":
            pos = self._positions.get(opp.event_ticker)
            return pos.exposure_bps if pos else 0
        if key_name == "pos":
            pos = self._positions.get(opp.event_ticker)
            if pos is None:
                return 0
            return pos.leg_a.filled_count + pos.leg_a.resting_count
        if key_name == "queue":
            pos = self._positions.get(opp.event_ticker)
            if pos and pos.leg_a.queue_position is not None:
                return pos.leg_a.queue_position
            return 999_999  # No queue → sort last
        if key_name == "cpm":
            pos = self._positions.get(opp.event_ticker)
            return pos.leg_a.cpm if pos and pos.leg_a.cpm is not None else 0.0
        if key_name == "eta":
            pos = self._positions.get(opp.event_ticker)
            if pos and pos.leg_a.eta_minutes is not None:
                return pos.leg_a.eta_minutes
            return 999_999.0  # No ETA → sort last
        if key_name == "eval":
            if opp.timestamp:
                try:
                    ts = datetime.fromisoformat(opp.timestamp)
                    return (datetime.now(UTC) - ts).total_seconds()
                except (ValueError, TypeError):
                    pass
            return 999_999.0
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

        self._talos_ids = {p.event_ticker: p.talos_id for p in scanner.pairs}
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
        # Talos ID
        tid = self._talos_ids.get(opp.event_ticker, 0)
        id_cell = RichText(str(tid), justify="right") if tid else ""

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

        # Sport + League (row 1 only)
        prefix = opp.event_ticker.split("-")[0]
        sport_league = _SPORT_LEAGUE.get(prefix)
        if sport_league:
            sport, league = sport_league
        else:
            # Non-sports: sport=prefix, league=submarket suffix
            sport = prefix
            parts = opp.event_ticker.split("-")
            league = parts[-1] if len(parts) > 1 else ""
        game_status = self._resolver.get(opp.event_ticker) if self._resolver else None
        game_col = _fmt_game_status(game_status)

        # Date column: prefer GameStatus scheduled_start, fall back to close_time
        if game_status is not None and game_status.scheduled_start is not None:
            date_col = _fmt_game_date(game_status.scheduled_start)
        elif opp.close_time:
            try:
                ct = datetime.fromisoformat(opp.close_time.replace("Z", "+00:00"))
                date_col = RichText(ct.astimezone(_PT).strftime("%m/%d"), justify="right")
            except (ValueError, TypeError):
                date_col = DIM_DASH
        else:
            date_col = DIM_DASH

        # Position data
        pos = self._positions.get(opp.event_ticker)
        if pos is not None:
            total_a = pos.leg_a.filled_count + pos.leg_a.resting_count
            total_b = pos.leg_b.filled_count + pos.leg_b.resting_count
            resting_cents_a = (
                bps_to_cents_round(pos.leg_a.resting_no_price_bps)
                if pos.leg_a.resting_no_price_bps is not None
                else None
            )
            resting_cents_b = (
                bps_to_cents_round(pos.leg_b.resting_no_price_bps)
                if pos.leg_b.resting_no_price_bps is not None
                else None
            )
            pos_a = _fmt_pos(
                pos.leg_a.filled_count,
                total_a,
                bps_to_cents_round(pos.leg_a.no_price_bps),
                resting_cents_a,
            )
            pos_b = _fmt_pos(
                pos.leg_b.filled_count,
                total_b,
                bps_to_cents_round(pos.leg_b.no_price_bps),
                resting_cents_b,
            )

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

            # Locked and Exposure — convert bps to cents for display.
            locked = bps_to_cents_round(int(pos.locked_profit_bps))
            if locked > 0:
                locked_str = RichText(f"${locked / 100:.2f}", style=GREEN, justify="right")
            elif locked == 0:
                locked_str = DIM_DASH
            else:
                locked_str = RichText(f"-${abs(locked) / 100:.2f}", style=RED, justify="right")

            exposure = bps_to_cents_round(pos.exposure_bps)
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
            # For same-ticker YES/NO pairs, check the correct side
            side_a = "yes" if opp.ticker_a == opp.ticker_b else "no"
            side_b = "no"
            if tracker.is_at_top(opp.ticker_a, side_a) is False:
                q_a = RichText(f"!! {q_a}", style=YELLOW, justify="right")
            if tracker.is_at_top(opp.ticker_b, side_b) is False:
                q_b = RichText(f"!! {q_b}", style=YELLOW, justify="right")

        # Eval age: seconds since scanner last evaluated this pair
        eval_str: str | RichText = DIM_DASH
        if opp.timestamp:
            try:
                ts = datetime.fromisoformat(opp.timestamp)
                age = (datetime.now(UTC) - ts).total_seconds()
                if age < 60:
                    eval_str = RichText(f"{int(age)}s", justify="right")
                elif age < 3600:
                    eval_str = RichText(f"{int(age // 60)}m", style="yellow", justify="right")
                else:
                    eval_str = RichText(f"{int(age // 3600)}h", style="red", justify="right")
            except (ValueError, TypeError):
                pass

        # Row 1: team A + shared event-level info
        row1 = (
            id_cell,
            dot_a,
            team_a,
            sport,
            league,
            date_col,
            game_col,
            no_a,
            vol_a,
            pos_a,
            q_a,
            cpm_a,
            eta_a,
            edge_str,
            eval_str,
            status,
            locked_str,
            exposure_str,
        )

        # Row 2: team B only — shared columns blank
        row2 = (
            "",
            dot_b,
            team_b,
            "",
            "",
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
            "",
        )

        return self._filter_row(row1), self._filter_row(row2)


class PortfolioPanel(Static):
    """Portfolio summary: account state and event coverage."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self._cash: int = 0
        self._portfolio: int = 0
        self._matched: int = 0
        self._partial: int = 0
        self._locked: float = 0.0
        self._exposure: int = 0
        self._events: int = 0
        self._with_positions: int = 0
        self._bidding: int = 0
        self._unentered: int = 0

    def on_mount(self) -> None:
        self.border_title = "Portfolio"

    def render(self) -> str:
        """Compute content each frame — bypasses Static.update() entirely."""
        cash = f"${self._cash / 100:,.2f}"
        locked = f"${self._locked / 100:,.2f}"
        exposure = f"${self._exposure / 100:,.2f}"
        return (
            f"Cash:       {cash}\n"
            f"Matched:    {self._matched} pairs\n"
            f"Partial:    {self._partial} events\n"
            f"Locked In:  {locked}\n"
            f"Exposure:   {exposure}\n"
            f"───────────────────\n"
            f"Events:       {self._events}\n"
            f"w/ Positions: {self._with_positions}\n"
            f"Bidding:      {self._bidding}\n"
            f"Unentered:    {self._unentered}"
        )

    def update_balance(self, balance_cents: int, portfolio_cents: int) -> None:
        self._cash = balance_cents
        self._portfolio = portfolio_cents
        self.refresh()

    def update_account(
        self,
        matched: int,
        partial: int,
        locked: float,
        exposure: int,
    ) -> None:
        self._matched = matched
        self._partial = partial
        self._locked = locked
        self._exposure = exposure
        self.refresh()

    def update_coverage(
        self,
        events: int,
        with_positions: int,
        bidding: int,
        unentered: int,
    ) -> None:
        self._events = events
        self._with_positions = with_positions
        self._bidding = bidding
        self._unentered = unentered
        self.refresh()


class PerformancePanel(Static):
    """Historical performance: settled events and P&L by time window."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self._d24h_events: int = 0
        self._d7d_events: int = 0
        self._d30d_events: int = 0
        self._d24h_pnl: int = 0
        self._d7d_pnl: int = 0
        self._d30d_pnl: int = 0

    def on_mount(self) -> None:
        self.border_title = "Performance"

    def _fmt_pnl(self, cents: int) -> str:
        if cents >= 0:
            return f"${cents / 100:,.2f}"
        return f"-${abs(cents) / 100:,.2f}"

    def render(self) -> str:
        h24 = self._fmt_pnl(self._d24h_pnl)
        d7 = self._fmt_pnl(self._d7d_pnl)
        d30 = self._fmt_pnl(self._d30d_pnl)
        e24, e7, e30 = self._d24h_events, self._d7d_events, self._d30d_events
        # Vertical layout: fits in ~24-char content area
        return (
            f"24h: {e24:>4d} settled {h24:>8s}\n"
            f"7d:  {e7:>4d} settled {d7:>8s}\n"
            f"30d: {e30:>4d} settled {d30:>8s}"
        )

    def update_performance(self, agg: dict[str, int]) -> None:
        self._d24h_events = agg.get("d24h_events", 0)
        self._d7d_events = agg.get("d7d_events", 0)
        self._d30d_events = agg.get("d30d_events", 0)
        self._d24h_pnl = agg.get("d24h_pnl", 0)
        self._d7d_pnl = agg.get("d7d_pnl", 0)
        self._d30d_pnl = agg.get("d30d_pnl", 0)
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
