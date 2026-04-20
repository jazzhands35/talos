"""TreeMetadataStore — typed wrapper around tree_metadata.json.

Owns event-level state:
- Manual event-start overrides (resolver-cascade priority 1)
- NEW indicator bookkeeping (first_seen, reviewed_at)
- Deliberately-unticked flags (applied and pending)

All mutations go through the typed API. Persistence is automatic when
`autosave=True` (default for production); set `autosave=False` in tests
that want to batch mutations before asserting disk state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import structlog

from talos.persistence import load_tree_metadata, save_tree_metadata

logger = structlog.get_logger()


class TreeMetadataStore:
    """Read/write interface for tree_metadata.json."""

    def __init__(self, path: Path | None = None, *, autosave: bool = True) -> None:
        self._path = path
        self._autosave = autosave
        self._data: dict[str, object] = {}
        self._loaded = False

    # ── Lifecycle ────────────────────────────────────────────────────

    def load(self) -> None:
        self._data = load_tree_metadata(self._path)
        self._loaded = True

    def save(self) -> None:
        """Persist the in-memory state to tree_metadata.json.

        Raises PersistenceError if the underlying write fails. The store
        holds safety-relevant state (manual_event_start overrides,
        deliberately_unticked_pending flags) — silent persistence
        failures would let the UI proceed as if a manual schedule
        override had been recorded while a restart would actually lose
        it. Callers must catch and surface this.
        """
        from talos.persistence_errors import PersistenceError

        ok = save_tree_metadata(self._data, self._path)
        if not ok:
            raise PersistenceError(
                "save_tree_metadata() returned failure (see warning log)"
            )

    def _touch(self) -> None:
        if self._autosave:
            self.save()

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise RuntimeError("TreeMetadataStore.load() must be called before use")

    # ── Manual event-start overrides ─────────────────────────────────

    def manual_event_start(self, kalshi_event_ticker: str) -> datetime | Literal["none"] | None:
        """Return the user's manual override for this event, or None.

        Return value:
          - datetime — explicit event-start set by user
          - "none"   — user explicitly opted out of exit-only for this event
          - None     — no override set; resolver cascade should fall through
        """
        self._require_loaded()
        raw = self._manual_dict().get(kalshi_event_ticker)
        if raw is None:
            return None
        if raw == "none":
            return "none"
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            logger.warning(
                "manual_event_start_invalid",
                event=kalshi_event_ticker,
                raw=raw,
            )
            return None

    def set_manual_event_start(self, kalshi_event_ticker: str, value: str) -> None:
        """Set a manual override. `value` is ISO 8601 datetime or 'none'."""
        self._require_loaded()
        self._manual_dict()[kalshi_event_ticker] = value
        self._touch()

    def clear_manual_event_start(self, kalshi_event_ticker: str) -> None:
        self._require_loaded()
        self._manual_dict().pop(kalshi_event_ticker, None)
        self._touch()

    # ── NEW indicator ─────────────────────────────────────────────────

    def is_new(self, kalshi_event_ticker: str) -> bool:
        """An event is NEW iff it's been seen but not reviewed."""
        self._require_loaded()
        seen = kalshi_event_ticker in self._first_seen_dict()
        reviewed = kalshi_event_ticker in self._reviewed_dict()
        return seen and not reviewed

    def mark_first_seen(self, kalshi_event_ticker: str) -> None:
        """Idempotent: only sets first_seen if not already present."""
        self._require_loaded()
        d = self._first_seen_dict()
        if kalshi_event_ticker not in d:
            d[kalshi_event_ticker] = datetime.now(UTC).replace(microsecond=0).isoformat()
            self._touch()

    def mark_reviewed(self, kalshi_event_ticker: str) -> None:
        """Clear the NEW flag by marking as reviewed."""
        self._require_loaded()
        d = self._reviewed_dict()
        d[kalshi_event_ticker] = datetime.now(UTC).replace(microsecond=0).isoformat()
        self._touch()

    # ── Deliberately unticked ─────────────────────────────────────────

    def is_deliberately_unticked(self, kalshi_event_ticker: str) -> bool:
        self._require_loaded()
        return kalshi_event_ticker in self._unticked_applied()

    def set_deliberately_unticked(self, kalshi_event_ticker: str) -> None:
        """Mutate in memory + save; on PersistenceError, rollback the
        in-memory mutation so memory and disk stay in sync. Round-7
        plan Fix #4: prevents UI from claiming "deliberately unticked"
        for an event whose tag is only in memory and not on disk."""
        from talos.persistence_errors import PersistenceError

        self._require_loaded()
        lst = self._unticked_applied()
        if kalshi_event_ticker in lst:
            return
        lst.append(kalshi_event_ticker)
        try:
            self._touch()
        except PersistenceError:
            lst.remove(kalshi_event_ticker)
            raise

    def clear_deliberately_unticked(self, kalshi_event_ticker: str) -> None:
        from talos.persistence_errors import PersistenceError

        self._require_loaded()
        lst = self._unticked_applied()
        if kalshi_event_ticker not in lst:
            return
        lst.remove(kalshi_event_ticker)
        try:
            self._touch()
        except PersistenceError:
            lst.append(kalshi_event_ticker)
            raise

    # ── Deliberately unticked (pending) ───────────────────────────────

    def is_deliberately_unticked_pending(self, kalshi_event_ticker: str) -> bool:
        self._require_loaded()
        return kalshi_event_ticker in self._unticked_pending()

    def pending_unticked(self) -> list[str]:
        """Return a copy of the persisted deferred-untick set."""
        self._require_loaded()
        return list(self._unticked_pending())

    def set_deliberately_unticked_pending(self, kalshi_event_ticker: str) -> None:
        from talos.persistence_errors import PersistenceError

        self._require_loaded()
        lst = self._unticked_pending()
        if kalshi_event_ticker in lst:
            return
        lst.append(kalshi_event_ticker)
        try:
            self._touch()
        except PersistenceError:
            lst.remove(kalshi_event_ticker)
            raise

    def clear_deliberately_unticked_pending(self, kalshi_event_ticker: str) -> None:
        from talos.persistence_errors import PersistenceError

        self._require_loaded()
        lst = self._unticked_pending()
        if kalshi_event_ticker not in lst:
            return
        lst.remove(kalshi_event_ticker)
        try:
            self._touch()
        except PersistenceError:
            lst.append(kalshi_event_ticker)
            raise

    def promote_pending_to_applied(self, kalshi_event_ticker: str) -> None:
        """Called when engine emits event_fully_removed for a pending event.

        Atomic: both mutations happen in memory, then ONE save fires.
        On save failure, both are rolled back so memory matches disk
        (round-7 plan Fix #3 — without this, the listener's "pending
        state preserved" message would lie because memory would say
        "applied" while disk said "pending").
        """
        from talos.persistence_errors import PersistenceError

        self._require_loaded()
        pending = self._unticked_pending()
        applied = self._unticked_applied()
        pending_had = kalshi_event_ticker in pending
        applied_had = kalshi_event_ticker in applied
        if pending_had:
            pending.remove(kalshi_event_ticker)
        if not applied_had:
            applied.append(kalshi_event_ticker)
        try:
            self._touch()
        except PersistenceError:
            # Restore exactly so memory ↔ disk consistent.
            if pending_had and kalshi_event_ticker not in pending:
                pending.append(kalshi_event_ticker)
            if not applied_had and kalshi_event_ticker in applied:
                applied.remove(kalshi_event_ticker)
            raise

    # ── Internal typed accessors ─────────────────────────────────────

    def _manual_dict(self) -> dict[str, str]:
        return cast("dict[str, str]", self._data["manual_event_start"])

    def _first_seen_dict(self) -> dict[str, str]:
        return cast("dict[str, str]", self._data["event_first_seen"])

    def _reviewed_dict(self) -> dict[str, str]:
        return cast("dict[str, str]", self._data["event_reviewed_at"])

    def _unticked_applied(self) -> list[str]:
        return cast("list[str]", self._data["deliberately_unticked"])

    def _unticked_pending(self) -> list[str]:
        return cast("list[str]", self._data["deliberately_unticked_pending"])
