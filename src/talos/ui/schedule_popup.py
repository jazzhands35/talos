"""SchedulePopup — modal prompt for manual event-start times.

Shown at commit time when staged events have no milestone, no manual
override, and no sports GSR coverage. The user must enter an event-start
time (or explicitly select "No exit-only") for each listed event before
commit proceeds.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

if TYPE_CHECKING:
    from talos.models.tree import ArbPairRecord


class SchedulePopup(ModalScreen[dict[str, str] | None]):
    """Modal popup that collects manual event-start times.

    Returns a ``dict[kalshi_event_ticker -> ISO datetime string or "none"]``
    on "Save all" / ``None`` on "Cancel".
    """

    CSS = """
    SchedulePopup {
        align: center middle;
    }
    SchedulePopup > Vertical {
        width: 80;
        height: auto;
        border: thick $primary;
        padding: 1 2;
        background: $surface;
    }
    SchedulePopup .event-row {
        height: auto;
        margin-bottom: 1;
    }
    SchedulePopup Input {
        width: 40;
    }
    SchedulePopup .buttons {
        align-horizontal: right;
        margin-top: 1;
    }
    """

    def __init__(self, records: list[ArbPairRecord]) -> None:
        super().__init__()
        # Deduplicate by kalshi_event_ticker.
        seen: set[str] = set()
        self._records: list[ArbPairRecord] = []
        for r in records:
            if r.kalshi_event_ticker in seen:
                continue
            seen.add(r.kalshi_event_ticker)
            self._records.append(r)
        self._inputs: dict[str, Input] = {}
        self._opt_outs: set[str] = set()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"[b]{len(self._records)} events need an event-start time[/b]\n"
                "These events have no Kalshi milestone and no manual override. "
                "Enter a time (ISO 8601, e.g. 2026-04-22T20:00:00-04:00) or "
                "click 'No exit-only' to opt this event out of exit-only "
                "scheduling.",
                id="schedule-header",
            )
            for r in self._records:
                with Horizontal(classes="event-row"):
                    yield Label(
                        f"{r.kalshi_event_ticker}  ({r.sub_title or r.series_ticker})",
                        classes="event-label",
                    )
                    inp = Input(
                        value=self._prefill_value(r),
                        placeholder="YYYY-MM-DDTHH:MM:SS±HH:MM",
                        id=f"input-{r.kalshi_event_ticker}",
                    )
                    self._inputs[r.kalshi_event_ticker] = inp
                    yield inp
                    yield Button(
                        "No exit-only",
                        id=f"optout-{r.kalshi_event_ticker}",
                        variant="warning",
                    )
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel", variant="default")
                yield Button("Save all", id="save", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id == "cancel":
            self.dismiss(None)
            return
        if btn_id == "save":
            result = self._collect()
            if result is None:
                # Validation failed; leave the popup open.
                return
            self.dismiss(result)
            return
        if btn_id.startswith("optout-"):
            kalshi_et = btn_id[len("optout-") :]
            self._opt_outs.add(kalshi_et)
            # Clear the input and mark visually.
            inp = self._inputs.get(kalshi_et)
            if inp is not None:
                inp.value = "(no exit-only)"
                inp.disabled = True

    @staticmethod
    def _prefill_value(record: ArbPairRecord) -> str:
        """Return the initial Input value for `record`'s row.

        Pre-fills from expected_expiration_time. Operator explicitly
        accepted the round-2 risk that for continuous events (hurricane
        counts, commodity panels) this value is the SETTLEMENT time —
        hours to days AFTER the actual rules-window closes — so leaving
        the default would let trading run past the real resolution
        moment. Mitigation: the popup is still shown for confirmation;
        nothing commits to Talos until the operator clicks "Save all"
        with the visible value. Empty string when the record has no
        expected_expiration_time (preserves the placeholder hint and
        forces explicit input or opt-out).
        """
        return record.expected_expiration_time or ""

    @staticmethod
    def _parse_aware_datetime(raw: str) -> datetime:
        """Parse a manual event-start input and require timezone awareness."""
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone offset required")
        return parsed

    def _collect(self) -> dict[str, str] | None:
        """Validate all inputs. Return dict on success, None on failure."""
        result: dict[str, str] = {}
        for r in self._records:
            et = r.kalshi_event_ticker
            if et in self._opt_outs:
                result[et] = "none"
                continue
            inp = self._inputs.get(et)
            if inp is None:
                return None
            raw = inp.value.strip()
            if not raw:
                self.app.notify(
                    f"{et}: enter a time or click 'No exit-only'",
                    severity="error",
                )
                return None
            try:
                self._parse_aware_datetime(raw)
            except ValueError:
                self.app.notify(
                    f"{et}: invalid ISO 8601 with timezone offset; "
                    "example: 2026-04-22T20:00:00-04:00",
                    severity="error",
                )
                return None
            result[et] = raw
        return result
