"""Tests for Drip runtime state — SimpleBook, SideRuntime, RuntimeState."""

from __future__ import annotations

import pytest

from drip.runtime_state import RuntimeState, SimpleBook, SyncState


class TestSimpleBook:
    def test_empty_book_has_no_best_price(self) -> None:
        book = SimpleBook()
        assert book.best_price is None

    def test_snapshot_populates_book(self) -> None:
        book = SimpleBook()
        book.apply_snapshot([[45, 10], [40, 5], [50, 3]])
        assert book.best_price == 50

    def test_snapshot_replaces_previous(self) -> None:
        book = SimpleBook()
        book.apply_snapshot([[90, 1]])
        book.apply_snapshot([[30, 5], [35, 2]])
        assert book.best_price == 35

    def test_snapshot_skips_zero_qty(self) -> None:
        book = SimpleBook()
        book.apply_snapshot([[45, 10], [50, 0]])
        assert book.best_price == 45

    def test_delta_adds_new_level(self) -> None:
        book = SimpleBook()
        book.apply_snapshot([[40, 5]])
        book.apply_delta(45, 3)
        assert book.best_price == 45

    def test_delta_removes_level_at_zero(self) -> None:
        book = SimpleBook()
        book.apply_snapshot([[40, 5], [45, 2]])
        book.apply_delta(45, -2)
        assert book.best_price == 40

    def test_delta_removes_level_below_zero(self) -> None:
        book = SimpleBook()
        book.apply_snapshot([[40, 5], [45, 2]])
        book.apply_delta(45, -10)
        assert book.best_price == 40

    def test_delta_increases_existing_level(self) -> None:
        book = SimpleBook()
        book.apply_snapshot([[40, 5]])
        book.apply_delta(40, 3)
        assert book.best_price == 40

    def test_all_levels_removed_returns_none(self) -> None:
        book = SimpleBook()
        book.apply_snapshot([[40, 5]])
        book.apply_delta(40, -5)
        assert book.best_price is None


class TestRuntimeState:
    def test_initial_sync_state_is_hydrating(self) -> None:
        rt = RuntimeState()
        assert rt.sync_state == SyncState.HYDRATING

    def test_get_side_a(self) -> None:
        rt = RuntimeState()
        assert rt.get_side("A") is rt.side_a

    def test_get_side_b(self) -> None:
        rt = RuntimeState()
        assert rt.get_side("B") is rt.side_b

    def test_get_side_unknown_raises(self) -> None:
        rt = RuntimeState()
        with pytest.raises(ValueError, match="Unknown side"):
            rt.get_side("C")

    def test_touch_ws_sets_timestamp(self) -> None:
        rt = RuntimeState()
        assert rt.last_ws_at is None
        rt.touch_ws()
        assert rt.last_ws_at is not None

    def test_pending_cancel_ids_start_empty(self) -> None:
        rt = RuntimeState()
        assert len(rt.side_a.pending_cancel_ids) == 0
        assert len(rt.side_b.pending_cancel_ids) == 0

    def test_pending_placements_start_empty(self) -> None:
        rt = RuntimeState()
        assert len(rt.side_a.pending_placements) == 0
        assert len(rt.side_b.pending_placements) == 0

    def test_pending_placements_tracks_order_id_to_price(self) -> None:
        rt = RuntimeState()
        rt.side_a.pending_placements["ord-1"] = 35
        rt.side_a.pending_placements["ord-2"] = 40
        assert rt.side_a.pending_placements["ord-1"] == 35
        assert len(rt.side_a.pending_placements) == 2

    def test_side_book_starts_empty(self) -> None:
        rt = RuntimeState()
        assert rt.side_a.book.best_price is None
        assert rt.side_b.book.best_price is None
