"""ReconcileBanner — operator-facing banner for ledger staleness / reconcile states.

Surfaces the per-pair ledger flags introduced by the bps/fp100 migration
(Section 8 of the migration spec, lines 1100-1109) and provides buttons for
the operator actions that clear them.

Priority resolution (highest severity first):

1. ``legacy_migration_pending``    → warning — "Reconcile now" /
                                               "View what will change"
2. ``stale_*_unconfirmed`` < 30s   → info    — "Confirming state with Kalshi..."
3. ``stale_*_unconfirmed`` >= 30s  → warning — "Retry sync" /
                                               "Manual reconcile"

Mismatches between the local ledger and Kalshi's authoritative fills are
resolved automatically by ``reconcile_from_fills`` (Principle 7 — Kalshi
wins, unconditionally). No banner state exists for that case — a single
``reconcile_auto_adopted`` warning log captures what was overwritten.

The banner hides itself entirely when ``ledger.ready()`` returns True.

Design notes
------------
The ledger does not carry a "stale_since" timestamp — the flags are set by
persist-load and cleared by sync/reconcile paths, but the operator-facing
30s timeout is a UI concern only. The widget tracks a monotonic timestamp of
when it first observed a stale flag and flips to the warning rendering once
30 seconds elapse without a sync clearing it.

The banner is imperative (no Textual reactive vars), matching the codebase
convention seen in ``ProposalPanel`` and the modal screens. ``refresh_state``
is the single entry point — callers pump it on whatever cadence they choose
(typically every second from ``set_interval`` on the host screen).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from talos.position_ledger import PositionLedger, ReconcileOutcome
from talos.ui.theme import BLUE, RED, SUBTEXT0, SURFACE0, SURFACE1, YELLOW

if TYPE_CHECKING:
    from talos.engine import TradingEngine
    from talos.models.strategy import ArbPair


STALE_WARNING_TIMEOUT_SECONDS: float = 30.0
"""How long a ``stale_*_unconfirmed`` flag may stay set before the banner
escalates from info ("Confirming state with Kalshi...") to warning
("Retry sync" / "Manual reconcile"). Per spec Section 8.
"""


# ── Banner CSS ──────────────────────────────────────────────────────────

BANNER_CSS = f"""
ReconcileBanner {{
    display: none;
    height: auto;
    min-height: 3;
    padding: 0 1;
    margin: 0 0 1 0;
    border: solid {SURFACE1};
    background: {SURFACE0};
}}

ReconcileBanner.visible {{
    display: block;
}}

ReconcileBanner.severity-info {{
    border: solid {BLUE};
    color: {SUBTEXT0};
}}

ReconcileBanner.severity-warning {{
    border: solid {YELLOW};
    color: {YELLOW};
}}

ReconcileBanner.severity-error {{
    border: solid {RED};
    color: {RED};
    text-style: bold;
}}

ReconcileBanner .reconcile-banner-msg {{
    height: auto;
    padding: 0 1;
}}

ReconcileBanner .reconcile-banner-buttons {{
    layout: horizontal;
    height: auto;
    align: left middle;
    padding: 0 1;
}}

ReconcileBanner .reconcile-banner-buttons Button {{
    margin: 0 1 0 0;
}}
"""


# ── Banner state resolution ─────────────────────────────────────────────


def _resolve_banner_state(
    ledger: PositionLedger,
    *,
    stale_elapsed_seconds: float,
    stale_timeout: float = STALE_WARNING_TIMEOUT_SECONDS,
) -> tuple[str, str, str, str | None] | None:
    """Pick the banner mode for the current ledger flags.

    Returns ``(mode, severity, message, secondary_label_or_none)`` or
    ``None`` when no banner should be shown (``ledger.ready()`` is True).

    ``mode`` is an internal key used by the widget to decide which buttons
    to mount and which click handler to run.
    """
    if ledger.ready():
        return None

    # Legacy migration: pre-migration save that must be reconciled (warning).
    if ledger.legacy_migration_pending:
        return (
            "legacy",
            "warning",
            "Legacy position state loaded — reconcile with Kalshi fills before trading.",
            "View what will change",
        )

    # Stale confirmation: info while auto-reconcile can still land (<30s),
    # warning once the timeout is exceeded.
    if ledger.stale_fills_unconfirmed or ledger.stale_resting_unconfirmed:
        if stale_elapsed_seconds < stale_timeout:
            return (
                "stale_info",
                "info",
                "Confirming state with Kalshi...",
                None,
            )
        return (
            "stale_warning",
            "warning",
            "Sync has not confirmed yet — Kalshi reply is delayed or lost.",
            "Manual reconcile",
        )

    # Ready() returned False but no flags are set — must be _first_orders_sync.
    # No actionable buttons here; show an info banner so the operator sees why
    # trading is gated.
    return (
        "awaiting_first_sync",
        "info",
        "Awaiting first orders sync from Kalshi...",
        None,
    )


# ── "View what will change" modal ───────────────────────────────────────


class LegacyDiffModal(ModalScreen[None]):
    """Show the operator the legacy v1 snapshot vs current live ledger state.

    Used by the ``legacy_migration_pending`` banner's secondary action. Plain
    text dump — the live reconcile in ``reconcile_from_fills`` replaces it
    with authoritative Kalshi fills anyway, so no active interaction is
    needed here. Dismissed with Escape or the Close button.
    """

    DEFAULT_CSS = f"""
    LegacyDiffModal {{
        align: center middle;
    }}
    #legacy-diff-dialog {{
        width: 80;
        height: auto;
        max-height: 80%;
        border: thick {SURFACE1};
        background: {SURFACE0};
        padding: 1 2;
    }}
    #legacy-diff-title {{
        color: {BLUE};
        text-style: bold;
        margin: 0 0 1 0;
    }}
    #legacy-diff-body {{
        height: auto;
        margin: 0 0 1 0;
    }}
    #legacy-diff-buttons {{
        layout: horizontal;
        height: auto;
        align: right middle;
    }}
    """

    BINDINGS = [("escape", "dismiss_modal", "Close")]

    def __init__(self, event_ticker: str, diff_text: str) -> None:
        super().__init__()
        self._event_ticker = event_ticker
        self._diff_text = diff_text

    def compose(self) -> ComposeResult:
        with Vertical(id="legacy-diff-dialog"):
            yield Label(
                f"Legacy snapshot vs live ledger — {self._event_ticker}",
                id="legacy-diff-title",
            )
            yield Static(self._diff_text, id="legacy-diff-body")
            with Horizontal(id="legacy-diff-buttons"):
                yield Button("Close", id="close-btn", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-btn":
            self.dismiss(None)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


# ── Banner widget ───────────────────────────────────────────────────────


class ReconcileBanner(Static):
    """Per-pair ledger-state banner with action buttons.

    The widget binds to a single ``ArbPair``'s ledger and reflects the four
    flag-driven states defined by the bps/fp100 migration (spec Section 8).
    It is rendered nowhere when the ledger reports ``ready()``.

    The owning screen is responsible for calling :meth:`refresh_state`
    periodically (typically once per second via ``set_interval``), so that
    the 30s stale-timeout transition fires even when no event activity
    occurs.
    """

    DEFAULT_CSS = BANNER_CSS

    def __init__(
        self,
        pair: ArbPair,
        ledger: PositionLedger,
        engine: TradingEngine | None,
        **kwargs: Any,
    ) -> None:
        super().__init__("", **kwargs)
        self._pair = pair
        self._ledger = ledger
        self._engine = engine
        # Populated the first time a stale_* flag is seen. Cleared when all
        # stale flags drop. Drives the 30s info → warning transition.
        self._stale_observed_at: float | None = None
        self._current_mode: str | None = None
        self._action_in_flight: bool = False
        self._severity_classes = (
            "severity-info",
            "severity-warning",
            "severity-error",
        )

    # ── Lifecycle ──────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.refresh_state()

    # ── Public API ─────────────────────────────────────────────────────

    def refresh_state(self) -> None:
        """Recompute the banner state from the ledger and re-render.

        Safe to call from a timer — cheap when nothing has changed.
        """
        now = time.monotonic()
        stale = self._ledger.stale_fills_unconfirmed or self._ledger.stale_resting_unconfirmed
        if stale:
            if self._stale_observed_at is None:
                self._stale_observed_at = now
        else:
            self._stale_observed_at = None

        stale_elapsed = 0.0
        if self._stale_observed_at is not None:
            stale_elapsed = now - self._stale_observed_at

        resolved = _resolve_banner_state(self._ledger, stale_elapsed_seconds=stale_elapsed)
        self._render_resolved(resolved)

    @property
    def current_mode(self) -> str | None:
        """Expose the resolved banner mode for tests + introspection.

        Returns ``None`` when the banner is hidden.
        """
        return self._current_mode

    # ── Rendering ──────────────────────────────────────────────────────

    def _render_resolved(
        self,
        resolved: tuple[str, str, str, str | None] | None,
    ) -> None:
        if resolved is None:
            self._current_mode = None
            self.remove_class("visible")
            # Textual refuses to mutate children before the widget is mounted;
            # guard so unit tests can construct the widget off-app.
            if self.is_mounted:
                self._clear_body()
            return

        mode, severity, message, secondary_label = resolved
        self._current_mode = mode
        self.add_class("visible")

        # Replace severity class set atomically.
        for cls in self._severity_classes:
            self.remove_class(cls)
        self.add_class(f"severity-{severity}")

        if self.is_mounted:
            self._mount_body(mode, message, secondary_label)

    def _clear_body(self) -> None:
        """Remove any previously mounted message/buttons."""
        for child in list(self.children):
            child.remove()

    def _mount_body(
        self,
        mode: str,
        message: str,
        secondary_label: str | None,
    ) -> None:
        """Rebuild the message + buttons for the given mode."""
        self._clear_body()
        msg = Static(message, classes="reconcile-banner-msg")
        self.mount(msg)

        buttons: list[Button] = []
        if mode == "legacy":
            buttons.append(
                Button(
                    "Reconcile now",
                    id="reconcile-primary",
                    variant="warning",
                )
            )
            if secondary_label is not None:
                buttons.append(
                    Button(
                        secondary_label,
                        id="reconcile-secondary",
                        variant="default",
                    )
                )
        elif mode == "stale_warning":
            buttons.append(
                Button(
                    "Retry sync",
                    id="reconcile-primary",
                    variant="warning",
                )
            )
            if secondary_label is not None:
                buttons.append(
                    Button(
                        secondary_label,
                        id="reconcile-secondary",
                        variant="default",
                    )
                )
        # stale_info / awaiting_first_sync: no buttons (info-only).

        if buttons:
            row = Horizontal(classes="reconcile-banner-buttons")
            self.mount(row)
            for b in buttons:
                row.mount(b)

    # ── Button handlers ────────────────────────────────────────────────

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Dispatch primary/secondary clicks by the widget's current mode."""
        if self._action_in_flight:
            return  # Debounce — avoid double-fires during an await.
        button_id = event.button.id
        mode = self._current_mode
        if button_id not in {"reconcile-primary", "reconcile-secondary"} or mode is None:
            return
        event.stop()

        self._action_in_flight = True
        try:
            if button_id == "reconcile-primary":
                await self._handle_primary(mode)
            else:
                await self._handle_secondary(mode)
        finally:
            self._action_in_flight = False
            self.refresh_state()

    async def _handle_primary(self, mode: str) -> None:
        if mode in {"legacy", "stale_warning"}:
            # Identical action for both — a reconcile_from_fills kick.
            await self._run_reconcile()

    async def _handle_secondary(self, mode: str) -> None:
        if mode == "legacy":
            self._show_legacy_diff()
        elif mode == "stale_warning":
            # "Manual reconcile" — same underlying action as primary on legacy.
            await self._run_reconcile()

    # ── Action implementations ─────────────────────────────────────────

    async def _run_reconcile(self) -> None:
        """Call ``ledger.reconcile_from_fills`` and surface the outcome.

        Mismatches are auto-adopted inside ``reconcile_from_fills`` (the
        ledger takes Kalshi's view unconditionally), so only OK/ERROR are
        surfaced here.
        """
        if self._engine is None:
            self._toast("Engine unavailable — cannot reconcile.", "error")
            return
        try:
            result = await self._ledger.reconcile_from_fills(
                self._engine._rest,
                self._engine._persist_games_now,
            )
        except Exception as exc:  # defensive — reconcile swallows most errors
            self._toast(f"Reconcile failed: {exc}", "error")
            return

        if result.outcome is ReconcileOutcome.OK:
            self._toast("Reconcile complete — ledger matches Kalshi fills.", "information")
        else:  # ERROR
            self._toast(
                f"Reconcile failed: {result.error or 'unknown error'}",
                "error",
            )

    def _show_legacy_diff(self) -> None:
        """Render the stored legacy v1 snapshot vs the current live ledger."""
        snapshot = self._ledger._legacy_v1_snapshot
        if not snapshot:
            self._toast(
                "No legacy snapshot stored — nothing to compare.",
                "warning",
            )
            return
        diff_text = self._format_legacy_diff(snapshot)
        self.app.push_screen(LegacyDiffModal(self._pair.event_ticker, diff_text))

    def _format_legacy_diff(self, snapshot: dict[str, object]) -> str:
        """Format the legacy v1 snapshot alongside the current live ledger.

        Renders a simple two-column text block — the reconcile itself is
        what actually fixes the ledger, so this is just an operator preview.
        """
        from talos.position_ledger import Side

        lines: list[str] = [
            "Legacy (pre-migration) → Current live ledger",
            "─" * 60,
        ]

        def _fmt_legacy(key: str) -> str:
            val = snapshot.get(key)
            return "—" if val is None else str(val)

        filled_a = self._ledger.filled_count(Side.A)
        filled_b = self._ledger.filled_count(Side.B)
        resting_a = self._ledger.resting_count(Side.A)
        resting_b = self._ledger.resting_count(Side.B)

        lines.append(f"Filled A: {_fmt_legacy('filled_a'):<20}→  {filled_a}")
        lines.append(f"Filled B: {_fmt_legacy('filled_b'):<20}→  {filled_b}")
        lines.append(f"Resting A count: {_fmt_legacy('resting_count_a'):<13}→  {resting_a}")
        lines.append(f"Resting B count: {_fmt_legacy('resting_count_b'):<13}→  {resting_b}")
        lines.append(
            f"Cost A (¢):       {_fmt_legacy('cost_a'):<12}→  "
            f"{self._ledger.filled_total_cost(Side.A)}"
        )
        lines.append(
            f"Cost B (¢):       {_fmt_legacy('cost_b'):<12}→  "
            f"{self._ledger.filled_total_cost(Side.B)}"
        )
        lines.append("")
        lines.append(
            "Clicking 'Reconcile now' rebuilds the live ledger from Kalshi "
            "fills — the legacy snapshot is only retained so you can inspect "
            "what was loaded from disk."
        )
        return "\n".join(lines)

    # ── Engine notification helper ─────────────────────────────────────

    def _toast(self, message: str, severity: str) -> None:
        """Surface an operator message via the engine's notification channel
        or, if unavailable, Textual's ``notify``.
        """
        engine = self._engine
        if engine is not None:
            notifier = getattr(engine, "on_notification", None)
            if callable(notifier):
                try:
                    notifier(message, severity, True)
                    return
                except Exception:  # fall through to app.notify
                    pass
        try:
            from textual.notifications import SeverityLevel

            self.app.notify(message, severity=severity)  # type: ignore[arg-type]
            _ = SeverityLevel  # silence unused-import on older Textual
        except Exception:
            pass
