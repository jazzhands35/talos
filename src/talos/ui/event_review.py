"""EventReviewScreen — full-screen modal showing comprehensive event detail."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from talos.ui.reconcile_banner import ReconcileBanner

if TYPE_CHECKING:
    from talos.engine import TradingEngine

_PT = ZoneInfo("America/Los_Angeles")


# ── Timeline entry dataclass ──────────────────────────────────────────


class _TimelineEntry:
    """A single entry in the unified event timeline."""

    __slots__ = ("ts", "kind", "text")

    def __init__(self, ts: str, kind: str, text: str) -> None:
        self.ts = ts
        self.kind = kind
        self.text = text


# ── DB queries ────────────────────────────────────────────────────────


def _query_orders(db: sqlite3.Connection, event_ticker: str) -> list[_TimelineEntry]:
    """Pull order events from the data collector."""
    rows = db.execute(
        "SELECT ts, order_id, ticker, side, action, status, price, "
        "initial_count, fill_count, remaining_count "
        "FROM orders WHERE event_ticker = ? ORDER BY ts",
        (event_ticker,),
    ).fetchall()
    entries = []
    for ts, order_id, ticker, side, action, status, price, initial, filled, _remaining in rows:
        side_label = side.upper() if side else "?"
        short_ticker = ticker.split("-", 1)[-1] if "-" in ticker else ticker
        if action == "place":
            text = f"ORDER PLACED — BUY {side_label} {short_ticker} @ {price}¢ × {initial}"
        elif action == "cancel":
            text = f"ORDER CANCELLED — {side_label} {short_ticker} ({order_id[:8]})"
        elif action == "amend":
            text = (
                f"ORDER AMENDED — {side_label} {short_ticker} → {price}¢ "
                f"× {initial} ({filled} filled)"
            )
        else:
            text = f"ORDER {action.upper()} — {side_label} {short_ticker} @ {price}¢ ({status})"
        entries.append(_TimelineEntry(ts, "order", text))
    return entries


def _query_fills(db: sqlite3.Connection, event_ticker: str) -> list[_TimelineEntry]:
    """Pull fill events from the data collector."""
    rows = db.execute(
        "SELECT ts, ticker, side, price, count, fee_cost, is_taker, "
        "queue_position, time_since_order "
        "FROM fills WHERE event_ticker = ? ORDER BY ts",
        (event_ticker,),
    ).fetchall()
    entries = []
    for ts, ticker, side, price, count, fee_cost, is_taker, _queue_pos, time_since in rows:
        side_label = side.upper() if side else "?"
        short_ticker = ticker.split("-", 1)[-1] if "-" in ticker else ticker
        role = "taker" if is_taker else "maker"
        fee_str = f", fee {fee_cost}¢" if fee_cost else ""
        wait_str = ""
        if time_since is not None and time_since > 0:
            mins = time_since / 60
            wait_str = f", waited {mins:.0f}m" if mins >= 1 else f", waited {time_since:.0f}s"
        text = f"FILL — {count}× {side_label} {short_ticker} @ {price}¢ ({role}{fee_str}{wait_str})"
        entries.append(_TimelineEntry(ts, "fill", text))
    return entries


def _query_settlements(db: sqlite3.Connection, event_ticker: str) -> list[_TimelineEntry]:
    """Pull settlement events."""
    rows = db.execute(
        "SELECT ts, ticker, result, settlement_value, total_pnl "
        "FROM settlements WHERE event_ticker = ? ORDER BY ts",
        (event_ticker,),
    ).fetchall()
    entries = []
    for ts, ticker, result, _value, pnl in rows:
        short_ticker = ticker.split("-", 1)[-1] if "-" in ticker else ticker
        result_str = result.upper() if result else "?"
        pnl_str = f"${pnl / 100:.2f}" if pnl is not None else "?"
        text = f"SETTLED — {short_ticker}: {result_str}, P&L: {pnl_str}"
        entries.append(_TimelineEntry(ts, "settlement", text))
    return entries


def _query_outcomes(db: sqlite3.Connection, event_ticker: str) -> list[_TimelineEntry]:
    """Pull event outcome summary (trap analysis)."""
    rows = db.execute(
        "SELECT ts, filled_a, filled_b, avg_price_a, avg_price_b, "
        "revenue, total_pnl, trapped, trap_side, trap_delta, trap_loss "
        "FROM event_outcomes WHERE event_ticker = ? ORDER BY ts",
        (event_ticker,),
    ).fetchall()
    entries = []
    for row in rows:
        ts, f_a, f_b, avg_a, avg_b, revenue, pnl, trapped, trap_side, trap_d, trap_l = row
        pnl_str = f"${pnl / 100:.2f}" if pnl is not None else "?"
        rev_str = f"${revenue / 100:.2f}" if revenue is not None else "?"
        text = (
            f"OUTCOME — {f_a}×A @ {avg_a:.0f}¢ + {f_b}×B @ {avg_b:.0f}¢ "
            f"→ Revenue {rev_str}, P&L {pnl_str}"
        )
        if trapped:
            text += (
                f"\n         ⚠ TRAPPED on side {trap_side}: "
                f"delta {trap_d}, loss ${trap_l / 100:.2f}"
            )
        entries.append(_TimelineEntry(ts, "outcome", text))
    return entries


def _query_decisions(db: sqlite3.Connection, event_ticker: str) -> list[_TimelineEntry]:
    """Pull Talos evaluation decisions — including silent skips."""
    try:
        rows = db.execute(
            "SELECT ts, ticker, side, trigger, outcome, reason, "
            "book_top, resting_price, new_price, "
            "effective_this, effective_other "
            "FROM decisions WHERE event_ticker = ? ORDER BY ts",
            (event_ticker,),
        ).fetchall()
    except sqlite3.OperationalError:
        # Table doesn't exist yet (pre-migration DB)
        return []
    entries: list[_TimelineEntry] = []
    for (
        ts,
        ticker,
        side,
        trigger,
        outcome,
        reason,
        book_top,
        resting_price,
        new_price,
        eff_this,
        eff_other,
    ) in rows:
        side_label = side.upper() if side else "?"
        short_ticker = ticker.split("-", 1)[-1] if ticker and "-" in ticker else (ticker or "")
        label = (outcome or "?").upper()
        text = f"EVAL {side_label} {short_ticker} [{trigger}] {label}"
        if reason:
            text += f" — {reason}"
        # Append inputs inline so replay is self-contained
        inputs: list[str] = []
        if book_top is not None:
            inputs.append(f"book_top={book_top}")
        if resting_price is not None:
            inputs.append(f"resting={resting_price}")
        if new_price is not None and new_price != book_top:
            inputs.append(f"new={new_price}")
        if eff_this is not None and eff_other is not None:
            inputs.append(
                f"eff={eff_this:.1f}+{eff_other:.1f}={eff_this + eff_other:.1f}"
            )
        if inputs:
            text += f"\n         inputs: {' '.join(inputs)}"
        entries.append(_TimelineEntry(ts, "decision", text))
    return entries


def _query_game_adds(db: sqlite3.Connection, event_ticker: str) -> list[_TimelineEntry]:
    """Pull game add events."""
    rows = db.execute(
        "SELECT ts, source, ticker_a, ticker_b, volume_a, volume_b "
        "FROM game_adds WHERE event_ticker = ? ORDER BY ts",
        (event_ticker,),
    ).fetchall()
    entries = []
    for ts, source, _t_a, _t_b, vol_a, vol_b in rows:
        source_str = source or "manual"
        text = f"ADDED — via {source_str} (vol: {vol_a}/{vol_b})"
        entries.append(_TimelineEntry(ts, "add", text))
    return entries


def _parse_suggestion_log(log_path: Path, event_ticker: str) -> list[_TimelineEntry]:
    """Parse the suggestion log file for entries matching this event."""
    entries: list[_TimelineEntry] = []
    if not log_path.exists():
        return entries

    try:
        text = log_path.read_text(encoding="utf-8")
    except Exception:
        return entries

    # Each entry is separated by blank lines, format:
    # [YYYY-MM-DD HH:MM:SS] ACTION      kind         event_ticker  side X
    #   summary line
    #   detail line
    blocks = text.strip().split("\n\n")
    for block in blocks:
        lines = block.strip().split("\n")
        if not lines:
            continue
        header = lines[0]
        # Check if this block is for our event
        if event_ticker not in header:
            continue
        # Parse timestamp and action from header
        # Format: [2026-03-22 03:25:26] PROPOSED    bid          EVT-TICKER
        if not header.startswith("["):
            continue
        try:
            ts_end = header.index("]")
            ts_str = header[1:ts_end]
            rest = header[ts_end + 2 :].split()
            action = rest[0] if rest else "?"
            kind = rest[1] if len(rest) > 1 else ""
        except (ValueError, IndexError):
            continue

        # Build readable text from action + detail lines
        detail_lines = [ln.strip() for ln in lines[1:] if ln.strip()]
        detail = " — ".join(detail_lines) if detail_lines else ""

        action_map = {
            "PROPOSED": "PROPOSED",
            "APPROVED": "APPROVED",
            "REJECTED": "REJECTED",
            "SUPERSEDED": "SUPERSEDED",
            "EXPIRED": "EXPIRED",
        }
        label = action_map.get(action, action)
        text = f"{label} {kind}"
        if detail:
            text += f" — {detail}"

        entries.append(_TimelineEntry(ts_str, "suggestion", text))

    return entries


def build_timeline(db_path: Path, log_path: Path, event_ticker: str) -> list[_TimelineEntry]:
    """Build a unified, sorted timeline for an event from all data sources."""
    db = sqlite3.connect(str(db_path), timeout=2)
    db.execute("PRAGMA query_only = ON")
    try:
        entries: list[_TimelineEntry] = []
        entries.extend(_query_game_adds(db, event_ticker))
        entries.extend(_query_orders(db, event_ticker))
        entries.extend(_query_fills(db, event_ticker))
        entries.extend(_query_decisions(db, event_ticker))
        entries.extend(_query_settlements(db, event_ticker))
        entries.extend(_query_outcomes(db, event_ticker))
    finally:
        db.close()

    entries.extend(_parse_suggestion_log(log_path, event_ticker))

    # Sort by timestamp (newest first for display)
    entries.sort(key=lambda e: e.ts, reverse=True)
    return entries


# ── Position snapshot renderer ────────────────────────────────────────


def _latest_decisions_by_side(
    db_path: Path, event_ticker: str
) -> dict[str, dict[str, str]]:
    """Return {side_label: {ts, trigger, outcome, reason}} for each side's latest eval."""
    result: dict[str, dict[str, str]] = {}
    try:
        db = sqlite3.connect(str(db_path), timeout=2)
        db.execute("PRAGMA query_only = ON")
    except Exception:
        return result
    try:
        for side_label in ("A", "B"):
            row = db.execute(
                "SELECT ts, trigger, outcome, reason "
                "FROM decisions WHERE event_ticker = ? AND side = ? "
                "ORDER BY ts DESC LIMIT 1",
                (event_ticker, side_label),
            ).fetchone()
            if row is not None:
                result[side_label] = {
                    "ts": row[0],
                    "trigger": row[1] or "",
                    "outcome": row[2] or "",
                    "reason": row[3] or "",
                }
    except sqlite3.OperationalError:
        pass
    finally:
        db.close()
    return result


def render_position_snapshot(
    engine: TradingEngine, event_ticker: str, db_path: Path | None = None
) -> str:
    """Render a plain-English summary of the current position state."""
    lines: list[str] = []

    # Find the pair config
    pair = None
    for p in engine.scanner.pairs:
        if p.event_ticker == event_ticker:
            pair = p
            break

    if pair is None:
        lines.append("This event is not currently tracked by Talos.")
        return "\n".join(lines)

    # Get ledger state
    try:
        ledger = engine.adjuster.get_ledger(event_ticker)
    except KeyError:
        lines.append("No position ledger exists for this event.")
        return "\n".join(lines)

    from talos.position_ledger import Side

    filled_a = ledger.filled_count(Side.A)
    filled_b = ledger.filled_count(Side.B)
    resting_a = ledger.resting_count(Side.A)
    resting_b = ledger.resting_count(Side.B)
    resting_price_a = ledger.resting_price(Side.A)
    resting_price_b = ledger.resting_price(Side.B)
    unit_size = ledger.unit_size

    # Position summary
    if filled_a == 0 and filled_b == 0 and resting_a == 0 and resting_b == 0:
        lines.append("You hold 0 contracts on either side. No orders are resting.")
    else:
        if filled_a > 0 or filled_b > 0:
            matched = min(filled_a, filled_b)
            avg_a = ledger.avg_filled_price(Side.A)
            avg_b = ledger.avg_filled_price(Side.B)
            if filled_a == filled_b:
                lines.append(
                    f"You hold {filled_a} contracts on each side "
                    f"({filled_a // unit_size} complete unit(s) of {unit_size})."
                )
            else:
                lines.append(
                    f"Fills: {filled_a} on side A, {filled_b} on side B "
                    f"({matched} matched pairs, "
                    f"{abs(filled_a - filled_b)} unmatched on "
                    f"{'A' if filled_a > filled_b else 'B'})."
                )
            if avg_a is not None and avg_b is not None:
                combined = avg_a + avg_b
                gross_edge = 100 - combined
                lines.append(
                    f"Side A avg {avg_a:.0f}¢, Side B avg {avg_b:.0f}¢. "
                    f"Combined cost: {combined:.0f}¢/pair → "
                    f"{gross_edge:.0f}¢ gross edge per pair."
                )

        if resting_a > 0 or resting_b > 0:
            parts = []
            if resting_a > 0:
                parts.append(f"A: {resting_a}× @ {resting_price_a}¢")
            if resting_b > 0:
                parts.append(f"B: {resting_b}× @ {resting_price_b}¢")
            lines.append(f"Resting orders: {', '.join(parts)}.")

    # Position summary from engine
    summary = None
    for s in engine.position_summaries:
        if s.event_ticker == event_ticker:
            summary = s
            break

    if summary is not None:
        if summary.locked_profit_cents != 0:
            profit_str = f"${summary.locked_profit_cents / 100:.2f}"
            lines.append(f"Locked profit (if both sides settle): {profit_str}.")
        if summary.exposure_cents != 0:
            exp_str = f"${summary.exposure_cents / 100:.2f}"
            lines.append(f"Exposure (unmatched risk): {exp_str}.")

    # Queue / CPM / ETA
    if summary is not None:
        for label, leg in [("A", summary.leg_a), ("B", summary.leg_b)]:
            parts = []
            if leg.queue_position is not None:
                parts.append(f"queue #{leg.queue_position}")
            if leg.cpm is not None:
                partial = " (partial)" if leg.cpm_partial else ""
                parts.append(f"CPM {leg.cpm:.1f}{partial}")
            if leg.eta_minutes is not None:
                parts.append(f"ETA ~{leg.eta_minutes:.0f}m")
            if parts:
                lines.append(f"  Side {label}: {', '.join(parts)}")

    lines.append("")

    # Status explanation
    status = engine.event_statuses.get(event_ticker, "")
    if status:
        lines.append(f"Status: {status}")
        explanation = _explain_status(status, ledger, engine, event_ticker)
        if explanation:
            lines.append(explanation)

        # Latest decision per side — surfaces WHY Talos is (or isn't) acting
        latest_by_side = (
            _latest_decisions_by_side(db_path, event_ticker) if db_path else {}
        )
        for side_label in ("A", "B"):
            rec = latest_by_side.get(side_label)
            if rec is None:
                continue
            ts_pretty = _format_timestamp(rec["ts"])
            lines.append(
                f"  Side {side_label} last eval [{ts_pretty} · {rec['trigger']}]: "
                f"{(rec['outcome'] or '').upper()} — {rec['reason'] or ''}"
            )

    # Scanner opportunity
    opp = engine.scanner.get_opportunity(event_ticker)
    if opp is not None:
        lines.append("")
        lines.append(
            f"Current opportunity: NO-A {opp.no_a}¢ + NO-B {opp.no_b}¢ "
            f"= {opp.cost}¢ cost → {opp.fee_edge:.1f}¢ fee-adjusted edge. "
            f"Tradeable qty: {opp.tradeable_qty}."
        )

    # Game status
    if engine.game_status_resolver is not None:
        gs = engine.game_status_resolver.get(event_ticker)
        if gs is not None:
            lines.append("")
            lines.append(_format_game_status(gs))

    return "\n".join(lines)


def _explain_status(
    status: str,
    _ledger: object,
    engine: TradingEngine,
    _event_ticker: str,
) -> str:
    """Translate a status code to a plain-English explanation."""
    cfg = engine.automation_config
    s = status.strip()

    if s == "Ready":
        return (
            f"Talos is ready to propose a bid. Edge exceeds the "
            f"{cfg.edge_threshold_cents:.1f}¢ threshold and has been stable "
            f"for at least {cfg.stability_seconds:.0f}s."
        )
    if s == "Low edge":
        return (
            f"Edge is below the {cfg.edge_threshold_cents:.1f}¢ threshold. "
            f"Talos will not propose a bid until edge improves."
        )
    if s == "Sug. off":
        return "Suggestions are turned off. Press 's' to enable."
    if s.startswith("Stable "):
        return (
            f"Edge meets the threshold but hasn't been stable long enough. "
            f"Talos waits {cfg.stability_seconds:.0f}s to filter transient spikes."
        )
    if s.startswith("Cooldown "):
        return (
            f"You recently rejected a proposal for this event. Talos waits "
            f"{cfg.rejection_cooldown_seconds:.0f}s before re-proposing."
        )
    if s == "Bidding":
        return "Both sides have resting orders. Waiting for fills."
    if s == "Proposed":
        return "A bid proposal is pending your approval. Press 'p' to view."
    if s.startswith("Filling"):
        return (
            "One side has more fills than the other. Catch-up logic will "
            "close the gap when possible."
        )
    if s.startswith("Waiting"):
        return (
            "Fill imbalance with no resting orders. Talos will propose a "
            "catch-up order when conditions allow."
        )
    if s.startswith("Jumped"):
        return (
            "Resting orders are no longer at top of book. Talos may propose "
            "an adjustment to follow the new price."
        )
    if s == "Imbalanced":
        return (
            "Position is imbalanced between sides. A rebalance proposal "
            "is pending to cancel the ahead side."
        )
    if s == "Balanced":
        return "Both sides filled equally. No resting orders remain."
    if s.startswith("Need bid"):
        return "One side has a resting order but the other doesn't."
    if s == "EXIT":
        return "Exit-only mode. No new bids. Balanced and idle."
    if s == "EXITING":
        return "Exit-only mode. Resting orders remain — letting them fill."
    if s.startswith("EXIT -"):
        return "Exit-only mode with fill imbalance. Catch-up in progress."
    return ""


def _format_game_status(gs: object) -> str:
    """Format game status for display."""
    # GameStatus has .state, .detail, .scheduled_start
    state = getattr(gs, "state", "unknown")
    detail = getattr(gs, "detail", "")
    scheduled = getattr(gs, "scheduled_start", None)

    if state == "live":
        return f"Game status: LIVE{' — ' + detail if detail else ''}"
    if state == "post":
        return "Game status: FINAL"
    if state == "pre" and scheduled is not None:
        try:
            dt = datetime.fromisoformat(scheduled) if isinstance(scheduled, str) else scheduled
            local = dt.astimezone(_PT)
            return f"Game status: Starts {local.strftime('%I:%M %p PT')}"
        except Exception:
            return "Game status: Pre-game"
    return f"Game status: {state}"


def _format_timestamp(ts: str) -> str:
    """Format an ISO timestamp to a readable local time."""
    try:
        dt = datetime.fromisoformat(ts)
        local = dt.astimezone(_PT)
        return local.strftime("%m/%d %I:%M:%S %p")
    except Exception:
        return ts[:19] if len(ts) >= 19 else ts


# ── Screen ────────────────────────────────────────────────────────────


class EventReviewScreen(ModalScreen[None]):
    """Full-screen modal showing comprehensive event review."""

    DEFAULT_CSS = """
    EventReviewScreen {
        align: center middle;
    }
    #review-container {
        width: 98%;
        height: 96%;
        border: thick $surface;
        background: $surface;
        padding: 1 2;
    }
    #review-header {
        height: auto;
        max-height: 4;
        color: #89b4fa;
        text-style: bold;
        margin: 0 0 1 0;
    }
    #review-position {
        height: auto;
        max-height: 16;
        color: #cdd6f4;
        margin: 0 0 1 0;
        padding: 0 1;
    }
    #review-divider {
        height: 1;
        color: #45475a;
        margin: 0 0 0 0;
    }
    #review-timeline-header {
        height: 1;
        color: #89b4fa;
        text-style: bold;
        margin: 0 0 0 0;
    }
    #review-timeline {
        height: 1fr;
        padding: 0 1;
    }
    .tl-add { color: #a6adc8; }
    .tl-order { color: #f9e2af; }
    .tl-fill { color: #a6e3a1; }
    .tl-settlement { color: #cba6f7; }
    .tl-outcome { color: #fab387; }
    .tl-suggestion { color: #89b4fa; }
    .tl-empty { color: #6c7086; }
    """

    BINDINGS = [
        ("escape", "cancel", "Close"),
        ("r", "cancel", "Close"),
    ]

    def __init__(
        self,
        event_ticker: str,
        engine: TradingEngine,
        db_path: Path,
        log_path: Path,
    ) -> None:
        super().__init__()
        self._event_ticker = event_ticker
        self._engine = engine
        self._db_path = db_path
        self._log_path = log_path
        self._reconcile_banner: ReconcileBanner | None = None

    def _build_reconcile_banner(self) -> ReconcileBanner | None:
        """Construct a banner bound to this event's pair+ledger, or None.

        Returns None when the event is not currently tracked (no ArbPair)
        or has no ledger yet — in both cases there is nothing to reconcile.
        """
        pair = None
        for p in self._engine.scanner.pairs:
            if p.event_ticker == self._event_ticker:
                pair = p
                break
        if pair is None:
            return None
        try:
            ledger = self._engine.adjuster.get_ledger(self._event_ticker)
        except KeyError:
            return None
        self._reconcile_banner = ReconcileBanner(
            pair=pair,
            ledger=ledger,
            engine=self._engine,
            id="reconcile-banner",
        )
        return self._reconcile_banner

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="review-container"):
            yield Static(id="review-header")
            # Banner slot — populated on_mount if a ledger exists for the pair.
            # Rendered nothing when the ledger is ready() (no reconcile action
            # needed). Placed between header and position so the operator sees
            # it before reading the live state.
            banner_host = self._build_reconcile_banner()
            if banner_host is not None:
                yield banner_host
            yield Static(id="review-position")
            yield Static("─" * 90, id="review-divider")
            yield Static("HISTORY", id="review-timeline-header")
            yield Static(id="review-timeline")

    def on_mount(self) -> None:
        """Populate the screen with event data."""
        evt = self._event_ticker
        label = self._engine.game_manager.labels.get(evt, evt)

        # Re-poll banner state every second so the stale->warning (30s)
        # transition fires even when no event activity occurs on the ledger.
        if self._reconcile_banner is not None:
            self.set_interval(1.0, self._reconcile_banner.refresh_state)

        # Header
        status = self._engine.event_statuses.get(evt, "")
        opp = self._engine.scanner.get_opportunity(evt)
        edge_str = f"{opp.fee_edge:.1f}¢" if opp else "—"

        header = self.query_one("#review-header", Static)
        header.update(
            f"{label}  •  {evt}  •  Edge: {edge_str}  •  Status: {status}  •  [Esc/r] Close"
        )

        # Position snapshot
        pos_text = render_position_snapshot(self._engine, evt, self._db_path)
        position = self.query_one("#review-position", Static)
        position.update(pos_text)

        # Timeline
        entries = build_timeline(self._db_path, self._log_path, evt)
        timeline = self.query_one("#review-timeline", Static)

        if not entries:
            timeline.update("No recorded history for this event.")
            timeline.add_class("tl-empty")
        else:
            lines: list[str] = []
            for entry in entries:
                ts_display = _format_timestamp(entry.ts)
                lines.append(f"  {ts_display}  {entry.text}")
            timeline.update("\n".join(lines))

    def action_cancel(self) -> None:
        self.dismiss(None)
