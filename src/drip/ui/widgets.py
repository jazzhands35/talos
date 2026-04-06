"""Drip TUI widgets — side panels, balance panel, action log."""

from __future__ import annotations

from datetime import UTC, datetime

from rich.style import Style as RichStyle
from rich.text import Text as RichText
from textual.widgets import RichLog, Static

from drip.ui.theme import RED, SUBTEXT0, SURFACE2, YELLOW  # noqa: F401

# ---------------------------------------------------------------------------
# Severity → Rich style mapping
# ---------------------------------------------------------------------------

_SEVERITY_STYLE = {
    "information": RichStyle(color=SUBTEXT0),
    "warning": RichStyle(color=YELLOW),
    "error": RichStyle(color=RED, bold=True),
}


# ---------------------------------------------------------------------------
# SidePanel — displays one side's state
# ---------------------------------------------------------------------------


class SidePanel(Static):
    """Displays the state of one side (A or B) of the Drip run."""

    def __init__(
        self,
        label: str = "Side",
        *,
        name: str | None = None,
        id: str | None = None,  # noqa: A002
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._label = label

    def on_mount(self) -> None:
        self.border_title = self._label
        self.update(self._render_empty())

    def _render_empty(self) -> str:
        return f"{self._label}\nPrice: --\nFilled: 0\nResting: 0"

    def update_from_side(self, label: str, side: object) -> None:
        """Refresh display from a DripSide instance.

        Uses duck-typing to avoid importing DripSide at module level
        (keeps the import graph clean for Textual).
        """
        # DripSide has: target_price, filled_count, resting_count, deploying
        target_price: int = getattr(side, "target_price", 0)
        filled_count: int = getattr(side, "filled_count", 0)
        resting_count: int = getattr(side, "resting_count", 0)
        deploying: bool = getattr(side, "deploying", False)

        deploy_str = " (deploying)" if deploying else ""
        self.update(
            f"{label}\n"
            f"Price: {target_price}\u00a2\n"
            f"Filled: {filled_count}\n"
            f"Resting: {resting_count}{deploy_str}"
        )


# ---------------------------------------------------------------------------
# BalancePanel — displays balance / delta info
# ---------------------------------------------------------------------------


class BalancePanel(Static):
    """Displays the delta and matched-pair summary for the Drip run."""

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,  # noqa: A002
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)

    def on_mount(self) -> None:
        self.border_title = "Balance"
        self.update(self._render_empty())

    def _render_empty(self) -> str:
        return "Delta = 0 \u2713\nMatched: 0\nTotal filled: 0"

    def update_from_controller(self, ctrl: object) -> None:
        """Refresh display from a DripController instance.

        Uses duck-typing to avoid importing DripController at module level.
        """
        delta: int = getattr(ctrl, "delta", 0)
        matched: int = getattr(ctrl, "matched_pairs", 0)
        total: int = getattr(ctrl, "total_filled", 0)
        profitable: bool = True
        if hasattr(ctrl, "is_profitable"):
            profitable = ctrl.is_profitable()  # type: ignore[union-attr]

        # Delta indicator
        if delta == 0:
            delta_str = "Delta = 0 \u2713"
            delta_class = "delta-ok"
        elif delta == 1:
            delta_str = f"Delta = {delta} \u26a0"
            delta_class = "delta-warn"
        else:
            delta_str = f"Delta = {delta} \u2716"
            delta_class = "delta-danger"

        profit_str = "Profitable" if profitable else "UNPROFITABLE"

        lines = [
            delta_str,
            f"Matched: {matched}",
            f"Total filled: {total}",
            profit_str,
        ]

        self.update("\n".join(lines))

        # Apply delta styling to the widget border
        for cls in ("delta-ok", "delta-warn", "delta-danger"):
            self.remove_class(cls)
        self.add_class(delta_class)


# ---------------------------------------------------------------------------
# ActionLog — timestamped, color-coded log (mirrors Talos ActivityLog)
# ---------------------------------------------------------------------------


class ActionLog(RichLog):
    """Scrollable action log with timestamped, color-coded messages."""

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,  # noqa: A002
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._plain_lines: list[str] = []

    def on_mount(self) -> None:
        self.border_title = "Actions"

    def log_action(self, message: str, severity: str = "information") -> None:
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
