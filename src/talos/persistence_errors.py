"""Persistence error types shared across modules.

Lives in its own module to avoid a circular import: __main__'s
_persist_games callback raises this, engine.add_pairs_from_selection
catches it to trigger rollback, and TreeScreen.commit catches it again
to surface a clear toast and preserve staged_changes for retry.
"""
from __future__ import annotations


class PersistenceError(Exception):
    """A persistence write failed in a way that breaks the durability
    contract — the in-memory engine state was mutated but the on-disk
    snapshot does NOT reflect it. Callers should treat this as a hard
    commit failure, roll back the in-memory mutation if possible, and
    refuse to clear staged changes so the user can retry.

    Specifically: a save_games_full() failure means engine_state for
    winding-down pairs is no longer durable. On a restart from this
    state, those pairs would resurrect as freely tradable — exactly
    the failure mode the safety branch is designed to prevent.
    """


class RemoveBatchPersistenceError(PersistenceError):
    """Persistence failed mid-batch in remove_pairs_from_selection.

    Carries the count of successfully-persisted winding-down transitions
    so the UI can surface "Wind-down committed for N pairs; persistence
    failed at pair N+1" instead of a generic failure toast.

    Subclasses PersistenceError so existing `except PersistenceError`
    handlers catch it; commit() additionally `isinstance` checks to
    extract `persisted_count` for the toast.
    """

    def __init__(
        self,
        persisted_count: int,
        message: str,
        original: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.persisted_count = persisted_count
        self.original = original
