"""Modal screens for Talos TUI."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from rich.text import Text as RichText
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, TextArea

from datetime import datetime

from talos.game_manager import extract_leg_labels
from talos.game_status import GameStatus, _extract_date_from_ticker
from talos.models.market import Event
from talos.models.portfolio import Settlement
from talos.models.position import EventPositionSummary
from talos.models.strategy import BidConfirmation, Opportunity
from talos.ui.theme import GREEN, RED, SURFACE2, YELLOW

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
        table.add_column(RichText("V-A", justify=r), width=7)
        table.add_column(RichText("V-B", justify=r), width=7)

        # Build sortable row data
        rows: list[tuple[float, str, tuple[str, ...]]] = []
        for ev in self._events:
            ticker = ev.event_ticker
            prefix = ev.series_ticker or ticker.split("-")[0]
            sport, league = _SPORT_LEAGUE.get(prefix, ("—", "—"))

            # Date and time from game status
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

            # Event label
            label = ev.sub_title or ev.title
            if "(" in label:
                label = label[: label.rfind("(")].strip()

            # Volume
            vol_a = _fmt_vol_compact(ev.markets[0].volume_24h or 0) if ev.markets else "—"
            vol_b = _fmt_vol_compact(ev.markets[1].volume_24h or 0) if len(ev.markets) > 1 else "—"

            rows.append((sort_ts, ticker, (sport, league, date_str, time_str, label, vol_a, vol_b)))

        # Sort by date/time ascending (soonest first)
        rows.sort(key=lambda r: r[0])

        self._row_tickers = []
        for _, ticker, (sport, league, date_str, time_str, label, vol_a, vol_b) in rows:
            self._row_tickers.append(ticker)
            table.add_row(
                "",  # ✓ column
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

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        settlements: list[Settlement],
        position_summaries: list[EventPositionSummary] | None = None,
        subtitles: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self._settlements = settlements
        self._positions = {s.event_ticker: s for s in (position_summaries or [])}
        self._subtitles = subtitles or {}

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
        table.add_column(RichText("Revenue", justify=r), width=8)
        table.add_column(RichText("Profit", justify=r), width=8)
        table.add_column(RichText("Est P&L", justify=r), width=9)
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

            # Compute day total P&L
            day_pnl = 0
            for _, legs, _ in day_events:
                for s in legs:
                    cost = s.no_total_cost + s.yes_total_cost
                    day_pnl += s.revenue - cost

            # Day separator row
            day_label = day_events[0][2].strftime("%b %d")
            day_pnl_str = f"${day_pnl / 100:.2f}" if day_pnl >= 0 else f"-${abs(day_pnl) / 100:.2f}"
            sep_text = f"─── {day_label} ─────────────────── Day P&L: {day_pnl_str} ───"
            table.add_row(
                RichText(sep_text, style=SURFACE2),
                "", "", "", "", "", "", "", "", "", "",
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

        # Event-level actual P&L (sum both legs)
        total_revenue = sum(s.revenue for s in legs)
        total_cost = sum(s.no_total_cost + s.yes_total_cost for s in legs)
        actual_pnl = total_revenue - total_cost

        # Event-level estimated P&L from position tracker
        pos = self._positions.get(evt_ticker)
        if pos is not None:
            est_pnl = pos.locked_profit_cents
            est_str = self._fmt_dollars(int(est_pnl))
        else:
            est_str = RichText("—", style="dim", justify="right")

        actual_str = self._fmt_dollars(actual_pnl)

        # Highlight discrepancy
        if pos is not None and abs(int(pos.locked_profit_cents) - actual_pnl) > 5:
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
            self._fmt_dollars(total_revenue) if total_revenue else RichText("—", style="dim", justify="right"),
            self._fmt_dollars(actual_pnl),
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
            "", "", "", "", "",
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
        if leg is None or leg.no_count == 0:
            return RichText("—", style="dim", justify="right")
        avg = leg.no_total_cost // leg.no_count
        return RichText(f"{avg}¢", justify="right")

    @staticmethod
    def _fmt_qty(leg: Settlement | None) -> RichText:
        if leg is None or leg.no_count == 0:
            return RichText("—", style="dim", justify="right")
        return RichText(str(leg.no_count), justify="right")

    @staticmethod
    def _fmt_cost(leg: Settlement | None) -> RichText:
        if leg is None:
            return RichText("—", style="dim", justify="right")
        cost = leg.no_total_cost + leg.yes_total_cost
        return RichText(f"${cost / 100:.2f}", justify="right")

    @staticmethod
    def _fmt_dollars(cents: int) -> RichText:
        if cents >= 0:
            return RichText(f"${cents / 100:.2f}", style=GREEN, justify="right")
        return RichText(f"-${abs(cents) / 100:.2f}", style=RED, justify="right")

    def action_cancel(self) -> None:
        self.dismiss(None)
