"""Modal screens for Talos TUI."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from rich.text import Text as RichText
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, TextArea

from talos.game_manager import extract_leg_labels
from talos.game_status import GameStatus, _extract_date_from_ticker
from talos.models.market import Event, Market
from talos.models.portfolio import Settlement
from talos.models.position import EventPositionSummary
from talos.models.strategy import BidConfirmation, Opportunity
from talos.ui.theme import GREEN, RED, SURFACE2, YELLOW
from talos.units import (
    ONE_DOLLAR_BPS,
    bps_to_cents_round,
    cents_to_bps,
    format_bps_as_dollars_display,
)


# ── Settlement helpers (post-13a-2c: direct passthrough) ─────────────
def _settlement_revenue_bps(s: Settlement) -> int:
    return s.revenue_bps


def _settlement_fee_cost_bps(s: Settlement) -> int:
    return s.fee_cost_bps


def _settlement_yes_total_cost_bps(s: Settlement) -> int:
    return s.yes_total_cost_bps


def _settlement_no_total_cost_bps(s: Settlement) -> int:
    return s.no_total_cost_bps


def _settlement_yes_count_fp100(s: Settlement) -> int:
    return s.yes_count_fp100


def _settlement_no_count_fp100(s: Settlement) -> int:
    return s.no_count_fp100


# Duplicated from widgets.py to avoid circular imports
_SPORT_LEAGUE: dict[str, tuple[str, str]] = {
    "KXNHLGAME": ("HOC", "NHL"),
    "KXNBAGAME": ("BKB", "NBA"),
    "KXMLBGAME": ("BSB", "MLB"),
    "KXNFLGAME": ("FTB", "NFL"),
    "KXWNBAGAME": ("BKB", "WNBA"),
    "KXCFBGAME": ("BKB", "NCAAF"),
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

_PT = ZoneInfo("America/Los_Angeles")


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
            cost_bps_src = opp.cost_bps if opp.cost_bps else cents_to_bps(opp.cost)
            total_cost_bps = cost_bps_src * default_qty
            fee_profit = opp.fee_edge * default_qty  # fee_edge is float cents
            fee_pct = opp.fee_rate * 100
            yield Label(
                f"Total: {format_bps_as_dollars_display(total_cost_bps)} → "
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


class AutoAcceptScreen(ModalScreen[float | Literal["indefinite"] | None]):
    """Modal for entering automatic mode duration in hours."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label("Automatic Mode", classes="modal-title")
            yield Label("Hours until auto-stop (blank = indefinite):")
            yield Input(
                value="",
                placeholder="indefinite",
                id="hours-input",
                type="text",
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
            raw = hours_input.value.strip()
            if not raw:
                self.dismiss("indefinite")
                return
            try:
                hours = float(raw)
            except ValueError:
                self.query_one("#modal-error", Label).update("Enter a valid number or leave blank")
                return
            if hours <= 0:
                self.query_one("#modal-error", Label).update(
                    "Duration must be greater than 0 (or blank for indefinite)"
                )
                return
            self.dismiss(hours)


def _fmt_vol_compact(volume: int) -> str:
    """Format volume as compact string (e.g., '1.2k')."""
    if volume == 0:
        return "—"
    if volume >= 1000:
        return f"{volume / 1000:.1f}k"
    return str(volume)


class ScanScreen(ModalScreen[list[str] | None]):
    """Modal showing scan results for event selection."""

    DEFAULT_CSS = """
    ScanScreen {
        align: center middle;
    }
    #scan-dialog {
        width: 90%;
        height: 85%;
        border: thick $surface;
        background: $surface;
        padding: 1 2;
    }
    #scan-dialog Label {
        width: 100%;
        margin: 0 0 1 0;
    }
    #scan-table {
        height: 1fr;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("space", "toggle_selection", "Toggle"),
        ("enter", "confirm", "Add Selected"),
        ("a", "toggle_all", "Add All"),
    ]

    def __init__(
        self,
        events: list[Event],
        statuses: dict[str, GameStatus] | None = None,
    ) -> None:
        super().__init__()
        self._events = events
        self._statuses = statuses or {}
        self._selected: set[str] = set()
        self._all_selected = False
        # Ordered list of event tickers matching table row order
        self._row_tickers: list[str] = []

    def compose(self) -> ComposeResult:
        count = len(self._events)
        with Vertical(id="scan-dialog"):
            yield Label(
                f"Scan Results — {count} events found  "
                "Space:Toggle  Enter:Add  Esc:Cancel",
                classes="modal-title",
                markup=False,
            )
            yield DataTable(id="scan-table")

    def on_mount(self) -> None:
        table = self.query_one("#scan-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True

        r = "right"
        table.add_column("✓", width=2)
        table.add_column("Spt", width=4)
        table.add_column("Lg", width=5)
        table.add_column(RichText("Date", justify=r), width=6)
        table.add_column(RichText("Time", justify=r), width=8)
        table.add_column("Event")
        table.add_column(RichText("24h A", justify=r), width=7)
        table.add_column(RichText("24h B", justify=r), width=7)

        rows: list[tuple[float, str, tuple[str, ...]]] = []
        for ev in self._events:
            ticker = ev.event_ticker
            prefix = ev.series_ticker or ticker.split("-")[0]
            sport_league = _SPORT_LEAGUE.get(prefix)

            if sport_league:
                sport, league = sport_league
            else:
                # Non-sports: category short label + series prefix
                sport = _CATEGORY_SHORT.get(ev.category, ev.category[:4])
                league = prefix.removeprefix("KX")[:5]

            # Date and time from game status (sports)
            gs = self._statuses.get(ticker)
            sort_ts = 0.0
            date_str = "—"
            time_str = "—"
            if gs is not None and gs.scheduled_start is not None:
                pt = gs.scheduled_start.astimezone(_PT)
                date_str = pt.strftime("%m/%d")
                time_str = pt.strftime("%I:%M %p").lstrip("0")
                sort_ts = gs.scheduled_start.timestamp()
            else:
                raw_date = _extract_date_from_ticker(ticker)
                if raw_date is not None:
                    date_str = f"{raw_date[4:6]}/{raw_date[6:8]}"
                else:
                    # Non-sports fallback: earliest market close_time
                    close_times = [
                        m.close_time for m in ev.markets
                        if m.status == "active" and m.close_time
                    ]
                    if close_times:
                        earliest = min(close_times)
                        try:
                            ct = datetime.fromisoformat(
                                earliest.replace("Z", "+00:00")
                            )
                            pt = ct.astimezone(_PT)
                            date_str = pt.strftime("%m/%d")
                            time_str = pt.strftime("%I:%M %p").lstrip("0")
                            sort_ts = ct.timestamp()
                        except (ValueError, TypeError):
                            pass

            # Event label
            label = ev.sub_title or ev.title
            if "(" in label:
                label = label[: label.rfind("(")].strip()

            # Volume — use active markets only
            active_mkts = [m for m in ev.markets if m.status == "active"]
            vol_a = (
                _fmt_vol_compact((active_mkts[0].volume_24h_fp100 or 0) // 100)
                if active_mkts
                else "—"
            )
            vol_b = (
                _fmt_vol_compact((active_mkts[1].volume_24h_fp100 or 0) // 100)
                if len(active_mkts) > 1
                else "—"
            )

            rows.append(
                (sort_ts, ticker, (sport, league, date_str, time_str, label, vol_a, vol_b))
            )

        rows.sort(key=lambda r: r[0])

        self._row_tickers = []
        for _, ticker, (sport, league, date_str, time_str, label, vol_a, vol_b) in rows:
            self._row_tickers.append(ticker)
            table.add_row(
                "",
                sport,
                league,
                RichText(date_str, justify="right"),
                RichText(time_str, justify="right"),
                label,
                RichText(vol_a, justify="right"),
                RichText(vol_b, justify="right"),
                key=ticker,
            )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_toggle_selection(self) -> None:
        table = self.query_one("#scan-table", DataTable)
        if table.cursor_row is None or table.row_count == 0:
            return
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(self._row_tickers):
            return
        ticker = self._row_tickers[row_idx]
        check_col = table.ordered_columns[0].key
        if ticker in self._selected:
            self._selected.discard(ticker)
            table.update_cell(ticker, check_col, "")
        else:
            self._selected.add(ticker)
            table.update_cell(ticker, check_col, "✓")

    def action_toggle_all(self) -> None:
        """Add ALL events immediately."""
        if self._row_tickers:
            self.dismiss(list(self._row_tickers))

    def action_confirm(self) -> None:
        if self._selected:
            # Preserve the display order for selected tickers
            ordered = [t for t in self._row_tickers if t in self._selected]
            self.dismiss(ordered)
        else:
            self.dismiss(None)


class SettlementHistoryScreen(ModalScreen[None]):
    """Modal showing settled events grouped by day with P&L comparison."""

    DEFAULT_CSS = """
    SettlementHistoryScreen {
        align: center middle;
    }
    #settlement-dialog {
        width: 95%;
        height: 90%;
        border: thick $surface;
        background: $surface;
        padding: 1 2;
    }
    #settlement-table {
        height: 1fr;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("o", "open_kalshi", "Open on Kalshi"),
    ]

    def __init__(
        self,
        settlements: list[Settlement],
        position_summaries: list[EventPositionSummary] | None = None,
        subtitles: dict[str, str] | None = None,
        est_pnl_map: dict[str, int] | None = None,
    ) -> None:
        super().__init__()
        self._settlements = settlements
        self._positions = {s.event_ticker: s for s in (position_summaries or [])}
        self._subtitles = subtitles or {}
        self._est_pnl_map = est_pnl_map or {}

    def compose(self) -> ComposeResult:
        with Vertical(id="settlement-dialog"):
            yield Label(
                f"Settlement History — {len(self._settlements)} markets  Esc:Close",
                classes="modal-title",
                markup=False,
            )
            yield DataTable(id="settlement-table")

    def on_mount(self) -> None:
        table = self.query_one("#settlement-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = False

        r = "right"
        table.add_column("Team")
        table.add_column("Lg", width=5)
        table.add_column("W/L", width=4)
        table.add_column(RichText("NO", justify=r), width=5)
        table.add_column(RichText("Qty", justify=r), width=5)
        table.add_column(RichText("Cost", justify=r), width=8)
        table.add_column(RichText("Est", justify=r), width=9)
        table.add_column(RichText("Actual", justify=r), width=9)
        table.add_column(RichText("Settled", justify=r), width=9)

        # Group settlements by event_ticker
        events: dict[str, list[Settlement]] = {}
        for s in self._settlements:
            events.setdefault(s.event_ticker, []).append(s)

        # Group events by day (PT timezone)
        days: dict[str, list[tuple[str, list[Settlement], datetime]]] = {}
        for evt_ticker, legs in events.items():
            settled_dt = self._parse_time(legs[0].settled_time)
            if settled_dt is None:
                continue
            day_key = settled_dt.strftime("%Y-%m-%d")
            days.setdefault(day_key, []).append((evt_ticker, legs, settled_dt))

        # Sort days descending (newest first)
        sorted_days = sorted(days.items(), key=lambda d: d[0], reverse=True)

        row_idx = 0
        for day_key, day_events in sorted_days:
            # Sort events within day by settled time descending
            day_events.sort(key=lambda e: e[2], reverse=True)

            # Compute day total P&L (revenue - cost - fees)
            # Same-ticker pairs: Kalshi nets YES+NO → revenue=0; add
            # back min(yes,no) × $1 for the implicit settlement payout.
            # Bps arithmetic: fp100 × bps / fp100 = bps (units cancel).
            day_pnl_bps = 0
            for _, legs, _ in day_events:
                for s in legs:
                    implicit_bps = (
                        min(_settlement_yes_count_fp100(s), _settlement_no_count_fp100(s))
                        * ONE_DOLLAR_BPS
                    ) // 100
                    cost_bps = (
                        _settlement_no_total_cost_bps(s) + _settlement_yes_total_cost_bps(s)
                    )
                    day_pnl_bps += (
                        _settlement_revenue_bps(s) + implicit_bps
                        - cost_bps - _settlement_fee_cost_bps(s)
                    )

            # Day separator row
            day_label = day_events[0][2].strftime("%b %d")
            if day_pnl_bps >= 0:
                day_pnl_str = format_bps_as_dollars_display(day_pnl_bps)
            else:
                day_pnl_str = f"-{format_bps_as_dollars_display(abs(day_pnl_bps))}"
            sep_text = f"─── {day_label} ─────────────────── Day P&L: {day_pnl_str} ───"
            table.add_row(
                RichText(sep_text, style=SURFACE2),
                "", "", "", "", "", "", "", "",
                key=f"day:{day_key}",
            )
            row_idx += 1

            for evt_ticker, legs, settled_dt in day_events:
                self._add_event_rows(table, evt_ticker, legs, settled_dt, row_idx)
                row_idx += 2

    def _add_event_rows(
        self,
        table: DataTable,
        evt_ticker: str,
        legs: list[Settlement],
        settled_dt: datetime,
        row_idx: int,
    ) -> None:
        """Add two rows (leg A + leg B) for one settled event."""
        # Extract team names
        sub = self._subtitles.get(evt_ticker, "")
        if sub:
            team_a, team_b = extract_leg_labels(sub)
        elif len(legs) >= 2:
            team_a, team_b = legs[0].ticker, legs[1].ticker
        else:
            team_a = legs[0].ticker
            team_b = ""

        prefix = evt_ticker.split("-")[0]
        _, league = _SPORT_LEAGUE.get(prefix, ("—", "—"))

        # Per-leg data
        leg_a = legs[0] if len(legs) > 0 else None
        leg_b = legs[1] if len(legs) > 1 else None

        # Event-level actual P&L (sum both legs: revenue - cost - fees)
        # Same-ticker pairs: add implicit payout for netted YES+NO pairs.
        # Bps arithmetic: fp100 × bps / fp100 = bps.
        total_revenue_bps = sum(
            _settlement_revenue_bps(s)
            + (
                min(_settlement_yes_count_fp100(s), _settlement_no_count_fp100(s))
                * ONE_DOLLAR_BPS
            ) // 100
            for s in legs
        )
        total_cost_bps = sum(
            _settlement_no_total_cost_bps(s) + _settlement_yes_total_cost_bps(s)
            for s in legs
        )
        total_fees_bps = sum(_settlement_fee_cost_bps(s) for s in legs)
        actual_pnl_bps = total_revenue_bps - total_cost_bps - total_fees_bps
        actual_pnl = bps_to_cents_round(actual_pnl_bps)

        # Event-level estimated P&L: prefer live position, fall back to cache
        pos = self._positions.get(evt_ticker)
        est_pnl_cents: int | None = None
        if pos is not None:
            est_pnl_cents = int(pos.locked_profit_cents)
        elif evt_ticker in self._est_pnl_map:
            est_pnl_cents = self._est_pnl_map[evt_ticker]

        if est_pnl_cents is not None:
            est_str = self._fmt_dollars(est_pnl_cents)
        else:
            est_str = RichText("—", style="dim", justify="right")

        actual_str = self._fmt_dollars_bps(actual_pnl_bps)

        # Highlight discrepancy
        if est_pnl_cents is not None and abs(est_pnl_cents - actual_pnl) > 5:
            actual_str = RichText(str(actual_str), style=YELLOW, justify="right")

        time_str = settled_dt.strftime("%I:%M %p").lstrip("0")

        # Row A (leg A + shared event columns)
        table.add_row(
            team_a,
            league,
            self._fmt_result(leg_a),
            self._fmt_no_price(leg_a),
            self._fmt_qty(leg_a),
            self._fmt_cost(leg_a),
            est_str,
            actual_str,
            RichText(time_str, justify="right"),
            key=f"{evt_ticker}:a",
        )

        # Row B (leg B + blanks for shared columns)
        table.add_row(
            team_b,
            "",
            self._fmt_result(leg_b),
            self._fmt_no_price(leg_b),
            self._fmt_qty(leg_b),
            self._fmt_cost(leg_b),
            "", "", "",
            key=f"{evt_ticker}:b",
        )

    @staticmethod
    def _parse_time(settled_str: str) -> datetime | None:
        if not settled_str:
            return None
        try:
            return datetime.fromisoformat(
                settled_str.replace("Z", "+00:00")
            ).astimezone(_PT)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _fmt_result(leg: Settlement | None) -> RichText:
        if leg is None:
            return RichText("—", style="dim")
        if leg.market_result == "no":
            return RichText("W", style=GREEN)
        if leg.market_result == "yes":
            return RichText("L", style=RED)
        return RichText(leg.market_result or "—", style="dim")

    @staticmethod
    def _fmt_no_price(leg: Settlement | None) -> RichText:
        if leg is None or leg.no_count_fp100 == 0:
            return RichText("—", style="dim", justify="right")
        # Average NO price per contract: (total_cost_bps * fp100) / (count_fp100 * bps)
        # Display in whole cents (rounded).
        no_total_bps = _settlement_no_total_cost_bps(leg)
        no_count_fp100 = _settlement_no_count_fp100(leg)
        if no_count_fp100 == 0:
            return RichText("—", style="dim", justify="right")
        avg_bps = (no_total_bps * 100) // no_count_fp100
        return RichText(f"{bps_to_cents_round(avg_bps)}¢", justify="right")

    @staticmethod
    def _fmt_qty(leg: Settlement | None) -> RichText:
        if leg is None or leg.no_count_fp100 == 0:
            return RichText("—", style="dim", justify="right")
        return RichText(str(leg.no_count_fp100 // 100), justify="right")

    @staticmethod
    def _fmt_cost(leg: Settlement | None) -> RichText:
        if leg is None:
            return RichText("—", style="dim", justify="right")
        cost_bps = (
            _settlement_no_total_cost_bps(leg) + _settlement_yes_total_cost_bps(leg)
        )
        return RichText(format_bps_as_dollars_display(cost_bps), justify="right")

    @staticmethod
    def _fmt_dollars(cents: int) -> RichText:
        bps = cents_to_bps(cents)
        return SettlementHistoryScreen._fmt_dollars_bps(bps)

    @staticmethod
    def _fmt_dollars_bps(bps: int) -> RichText:
        if bps >= 0:
            return RichText(
                format_bps_as_dollars_display(bps), style=GREEN, justify="right",
            )
        return RichText(
            f"-{format_bps_as_dollars_display(abs(bps))}", style=RED, justify="right",
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_open_kalshi(self) -> None:
        """Open the selected event on Kalshi in the default browser."""
        import webbrowser

        table = self.query_one("#settlement-table", DataTable)
        if table.cursor_row is None:
            return
        # Row keys are "evt_ticker:a", "evt_ticker:b", or "day:YYYY-MM-DD"
        key_str = str(table.ordered_rows[table.cursor_row].key.value)
        if key_str.startswith("day:"):
            return
        evt_ticker = key_str.rsplit(":", 1)[0]
        webbrowser.open(f"https://kalshi.com/markets/{evt_ticker}")


class _PickerTable(DataTable):
    """DataTable subclass that forwards Space/Enter to the parent screen."""

    async def _on_key(self, event: Key) -> None:
        if event.key in ("space", "shift+space", "enter"):
            # Let the parent MarketPickerScreen handle these
            event.prevent_default()
            return
        await super()._on_key(event)


class BlacklistScreen(ModalScreen[list[str] | None]):
    """Modal for editing the ticker blacklist."""

    DEFAULT_CSS = """
    BlacklistScreen {
        align: center middle;
    }
    #blacklist-dialog {
        width: 70;
        height: 24;
        border: thick $surface;
        background: $surface;
        padding: 1 2;
    }
    #blacklist-dialog Label {
        width: 100%;
        margin: 0 0 1 0;
    }
    #blacklist-table {
        height: 1fr;
    }
    #blacklist-input {
        margin: 1 0 0 0;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("delete", "delete_entry", "Delete"),
    ]

    def __init__(self, entries: list[str]) -> None:
        super().__init__()
        self._entries = list(entries)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Vertical(id="blacklist-dialog"):
            yield Label(
                f"Ticker Blacklist — {len(self._entries)} entries  "
                "Del:Remove  Enter:Add  Esc:Done",
                classes="modal-title",
                markup=False,
            )
            yield DataTable(id="blacklist-table")
            yield Input(
                placeholder="Add prefix or ticker (e.g. KXSURV)",
                id="blacklist-input",
            )

    def on_mount(self) -> None:
        table = self.query_one("#blacklist-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("Entry", width=40)
        table.add_column("Type", width=10)
        self._rebuild_table()

    def _rebuild_table(self) -> None:
        table = self.query_one("#blacklist-table", DataTable)
        table.clear()
        for entry in sorted(self._entries):
            kind = "prefix" if len(entry.split("-")) == 1 else "ticker"
            table.add_row(entry, kind, key=entry)
        self.query_one("#blacklist-dialog Label", Label).update(
            f"Ticker Blacklist — {len(self._entries)} entries  "
            "Del:Remove  Enter:Add  Esc:Done"
        )

    def action_delete_entry(self) -> None:
        table = self.query_one("#blacklist-table", DataTable)
        if table.cursor_row is None or table.row_count == 0:
            return
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            entry = str(row_key.value)
            if entry in self._entries:
                self._entries.remove(entry)
                self._rebuild_table()
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip().upper()
        if not value:
            return
        if value not in self._entries:
            self._entries.append(value)
            self._rebuild_table()
        event.input.value = ""

    def on_key(self, event: Key) -> None:
        if event.key == "enter" and self.query_one("#blacklist-input", Input).has_focus:
            return  # Let Input.Submitted handle it
        if event.key == "enter" and not self.query_one("#blacklist-input", Input).has_focus:
            # Dismiss with updated list
            self.dismiss(self._entries)


class MarketPickerScreen(ModalScreen[list[Market]]):
    """Select markets from a non-sports event for YES/NO arb monitoring."""

    DEFAULT_CSS = """
    MarketPickerScreen {
        align: center middle;
    }
    #picker-dialog {
        width: 90%;
        height: 70%;
        border: thick $surface;
        background: $surface;
        padding: 1 2;
    }
    #picker-dialog Label {
        width: 100%;
        margin: 0 0 1 0;
    }
    #picker-table {
        height: 1fr;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("a", "select_all", "Select All"),
    ]

    def __init__(self, markets: list[Market], event_title: str = "") -> None:
        super().__init__()
        self._markets = markets
        self._event_title = event_title
        self._selected: set[str] = set()  # market tickers
        # Ordered list of tickers matching table row order
        self._row_tickers: list[str] = []
        self._last_toggled_idx: int | None = None  # for shift-select range

    def compose(self) -> ComposeResult:
        count = len(self._markets)
        with Vertical(id="picker-dialog"):
            title = f"Select Markets — {count} available"
            if self._event_title:
                title += f"  [{self._event_title}]"
            title += "  Space:Toggle  Shift+Space:Range  Enter:Add  Esc:Cancel"
            yield Label(title, classes="modal-title", markup=False)
            yield DataTable(id="picker-table")

    def on_mount(self) -> None:
        table = self.query_one("#picker-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True

        r = "right"
        table.add_column("✓", width=2)
        table.add_column("Market")
        table.add_column("Ticker", width=30)
        table.add_column(RichText("24h Vol", justify=r), width=10)

        # Sort by 24h volume descending (most liquid first)
        sorted_markets = sorted(
            self._markets,
            key=lambda m: (m.volume_24h_fp100 or 0) // 100,
            reverse=True,
        )

        self._row_tickers = []
        for market in sorted_markets:
            self._row_tickers.append(market.ticker)
            vol_str = _fmt_vol_compact((market.volume_24h_fp100 or 0) // 100)
            table.add_row(
                "",  # ✓ column
                market.title or market.ticker,
                market.ticker,
                RichText(vol_str, justify="right"),
                key=market.ticker,
            )

    def on_key(self, event: Key) -> None:
        """Handle Space, Enter, Shift+Space before DataTable consumes them."""
        if event.key == "space":
            event.prevent_default()
            event.stop()
            self._toggle_current()
        elif event.key == "shift+space":
            event.prevent_default()
            event.stop()
            self._toggle_range()
        elif event.key == "enter":
            event.prevent_default()
            event.stop()
            self._confirm()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """DataTable fires RowSelected on Enter — treat as confirm."""
        event.stop()
        self._confirm()

    def action_cancel(self) -> None:
        self.dismiss([])

    def _toggle_at(self, row_idx: int) -> None:
        """Toggle selection state for a single row by index."""
        table = self.query_one("#picker-table", DataTable)
        if row_idx < 0 or row_idx >= len(self._row_tickers):
            return
        ticker = self._row_tickers[row_idx]
        check_col = table.ordered_columns[0].key
        if ticker in self._selected:
            self._selected.discard(ticker)
            table.update_cell(ticker, check_col, "")
        else:
            self._selected.add(ticker)
            table.update_cell(ticker, check_col, "✓")

    def _select_at(self, row_idx: int) -> None:
        """Ensure a row is selected (for range select)."""
        if row_idx < 0 or row_idx >= len(self._row_tickers):
            return
        ticker = self._row_tickers[row_idx]
        if ticker not in self._selected:
            self._selected.add(ticker)
            table = self.query_one("#picker-table", DataTable)
            check_col = table.ordered_columns[0].key
            table.update_cell(ticker, check_col, "✓")

    def _toggle_current(self) -> None:
        """Toggle the row at the cursor."""
        table = self.query_one("#picker-table", DataTable)
        if table.cursor_row is None or table.row_count == 0:
            return
        row_idx = table.cursor_row
        self._toggle_at(row_idx)
        self._last_toggled_idx = row_idx

    def _toggle_range(self) -> None:
        """Select all rows from last toggle to current cursor (shift+space)."""
        table = self.query_one("#picker-table", DataTable)
        if table.cursor_row is None or table.row_count == 0:
            return
        current = table.cursor_row
        anchor = self._last_toggled_idx if self._last_toggled_idx is not None else current
        lo, hi = min(anchor, current), max(anchor, current)
        for idx in range(lo, hi + 1):
            self._select_at(idx)
        self._last_toggled_idx = current

    def action_select_all(self) -> None:
        """Toggle all markets selected/unselected."""
        table = self.query_one("#picker-table", DataTable)
        check_col = table.ordered_columns[0].key
        if len(self._selected) == len(self._markets):
            self._selected.clear()
            for ticker in self._row_tickers:
                table.update_cell(ticker, check_col, "")
        else:
            for market in self._markets:
                self._selected.add(market.ticker)
            for ticker in self._row_tickers:
                table.update_cell(ticker, check_col, "✓")

    def _confirm(self) -> None:
        """Add selected markets and dismiss.

        If nothing was space-toggled, add the market under the cursor.
        """
        if not self._selected:
            # Nothing toggled — add the cursor row
            table = self.query_one("#picker-table", DataTable)
            if table.cursor_row is not None and table.row_count > 0:
                row_idx = table.cursor_row
                if 0 <= row_idx < len(self._row_tickers):
                    self._selected.add(self._row_tickers[row_idx])

        selected = [
            m for t in self._row_tickers
            if t in self._selected
            for m in self._markets
            if m.ticker == t
        ]
        self.dismiss(selected)
