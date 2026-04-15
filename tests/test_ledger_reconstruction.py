"""Restart-regime tests for the open-unit avg scoping spec.

Covers the 5a (normal restart), 5b (first-boot migration from old saves),
5c (Kalshi-only cold start), and 5d (same-ticker specifics) regimes from
docs/superpowers/specs/2026-04-15-open-unit-avg-scoping-design.md.
"""
from __future__ import annotations

import logging

import pytest

from talos.position_ledger import PositionLedger, Side

_SaveDict = dict[str, int | str | None]


class TestRegime5cColdStart:
    """No save file at all — empty ledger, reconcile-on-fill handles it.

    5c is the trivial regime: seed_from_saved(None) or seed_from_saved({})
    early-returns; subsequent record_fill / sync_from_orders calls invoke
    _reconcile_closed per the invariant from Task 3.
    """

    def test_seed_from_saved_none_early_returns(self):
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(None)
        assert ledger._sides[Side.A].filled_count == 0
        assert ledger._sides[Side.A].closed_count == 0
        assert ledger._sides[Side.B].filled_count == 0
        assert ledger._sides[Side.B].closed_count == 0

    def test_seed_from_saved_empty_dict_early_returns(self):
        ledger = PositionLedger("EVT-X", unit_size=5)
        empty: _SaveDict = {}
        ledger.seed_from_saved(empty)
        assert ledger._sides[Side.A].filled_count == 0
        assert ledger._sides[Side.A].closed_count == 0
        assert ledger._sides[Side.B].filled_count == 0
        assert ledger._sides[Side.B].closed_count == 0

    def test_cold_start_then_fills_reconcile_normally(self):
        """Empty ledger + subsequent fills trigger reconcile via record_fill."""
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(None)
        ledger.record_fill(Side.A, 5, 80)
        ledger.record_fill(Side.B, 5, 20)
        assert ledger._sides[Side.A].closed_count == 5
        assert ledger._sides[Side.B].closed_count == 5


# Note: tests below deliberately overlap with TestSavedDictSchema in
# tests/test_position_ledger.py. That file tests seed_from_saved as a method;
# this file tests the four regimes end-to-end as described in spec section 5.
# Do not dedupe naively — keeping both layers separates "does the method
# work?" from "does the regime produce the right end-state?"
class TestRegime5aNormalRestart:
    """Persisted closed_* restored verbatim; no re-derivation from blend."""

    def test_open_avg_preserved_across_restart(self):
        """The Codex scenario: open B at 23c must come back at 23c,
        not the blended 20.5c."""
        persisted: _SaveDict = {
            "filled_a": 5, "cost_a": 410, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
            "closed_count_a": 5, "closed_total_cost_a": 410, "closed_fees_a": 0,
            "closed_count_b": 5, "closed_total_cost_b": 90, "closed_fees_b": 0,
        }
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(persisted)
        assert ledger.open_count(Side.A) == 0
        assert ledger.open_count(Side.B) == 5
        assert ledger.open_avg_filled_price(Side.B) == 23.0

    def test_reconcile_after_restart_is_noop(self):
        persisted: _SaveDict = {
            "filled_a": 5, "cost_a": 410, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
            "closed_count_a": 5, "closed_total_cost_a": 410, "closed_fees_a": 0,
            "closed_count_b": 5, "closed_total_cost_b": 90, "closed_fees_b": 0,
        }
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(persisted)
        closed_a = ledger._sides[Side.A].closed_count
        closed_b = ledger._sides[Side.B].closed_count
        ledger._reconcile_closed()
        assert ledger._sides[Side.A].closed_count == closed_a
        assert ledger._sides[Side.B].closed_count == closed_b

    def test_restored_log_line_once_per_ledger(self, caplog):
        caplog.set_level(logging.INFO)
        persisted: _SaveDict = {
            "filled_a": 5, "cost_a": 400, "fees_a": 0,
            "filled_b": 5, "cost_b": 100, "fees_b": 0,
            "closed_count_a": 5, "closed_total_cost_a": 400, "closed_fees_a": 0,
            "closed_count_b": 5, "closed_total_cost_b": 100, "closed_fees_b": 0,
        }
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(persisted)
        restored = [r for r in caplog.records if "ledger_restored_with_closed" in r.getMessage()]
        assert len(restored) == 1


class TestRegime5bFirstBootMigration:
    """Old save with filled_* but no closed_* → migration via blend."""

    def test_migration_flushes_balanced_portion(self):
        old_save: _SaveDict = {
            "filled_a": 10, "cost_a": 820, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
        }
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(old_save)
        assert ledger._sides[Side.A].closed_count == 10
        assert ledger._sides[Side.B].closed_count == 10
        assert ledger.open_count(Side.A) == 0
        assert ledger.open_count(Side.B) == 0

    def test_migration_preserves_lifetime_avg(self):
        old_save: _SaveDict = {
            "filled_a": 10, "cost_a": 820, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
        }
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(old_save)
        assert ledger.avg_filled_price(Side.A) == 82.0
        assert ledger.avg_filled_price(Side.B) == 20.5

    def test_migration_emits_migrated_log(self, caplog):
        caplog.set_level(logging.INFO)
        old_save: _SaveDict = {
            "filled_a": 10, "cost_a": 820, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
        }
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(old_save)
        migrated = [r for r in caplog.records if "ledger_migrated_missing_closed" in r.getMessage()]
        assert len(migrated) == 1

    def test_post_migration_save_includes_closed_keys(self):
        old_save: _SaveDict = {
            "filled_a": 10, "cost_a": 820, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
        }
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(old_save)
        new_save = ledger.to_save_dict()
        for k in ("closed_count_a", "closed_total_cost_a", "closed_fees_a",
                  "closed_count_b", "closed_total_cost_b", "closed_fees_b"):
            assert k in new_save, f"expected {k} in new save"


class TestRegime5bPartialKeys:
    """Atomic-group rule: partial closed_* triggers migration."""

    def test_partial_keys_trigger_full_migration(self, caplog):
        caplog.set_level(logging.INFO)
        corrupt: _SaveDict = {
            "filled_a": 10, "cost_a": 820, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
            "closed_count_a": 999, "closed_total_cost_a": 999,
            # missing closed_fees_a, all three B closed keys
        }
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(corrupt)
        # Migration zeroed and reconciled from blend — NOT restored verbatim
        assert ledger._sides[Side.A].closed_count == 10  # reconciled, not 999
        assert ledger._sides[Side.B].closed_count == 10
        migrated = [r for r in caplog.records if "ledger_migrated_missing_closed" in r.getMessage()]
        assert len(migrated) == 1


class TestRegime5bCorruptValues:
    """Corrupt value types trigger migration, not hard-fail."""

    @pytest.mark.parametrize("bad_value", [None, "abc", -5, True, 5.0, "5"])
    def test_corrupt_value_triggers_migration(self, caplog, bad_value):
        caplog.set_level(logging.INFO)
        corrupt: _SaveDict = {
            "filled_a": 10, "cost_a": 820, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
            "closed_count_a": 5, "closed_total_cost_a": 410, "closed_fees_a": 0,
            "closed_count_b": 5, "closed_total_cost_b": 90, "closed_fees_b": 0,
        }
        corrupt["closed_count_a"] = bad_value  # type: ignore[assignment]
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(corrupt)  # must not raise
        migrated = [r for r in caplog.records if "ledger_migrated_missing_closed" in r.getMessage()]
        assert len(migrated) == 1
        # Migration re-ran reconcile from blend; closed_count_a != 5
        assert ledger._sides[Side.A].closed_count == 10


class TestRegime5dSameTicker:
    """Same-ticker ledgers: seed_from_saved is the only reconciliation path."""

    def test_same_ticker_normal_restart_preserves_state(self):
        persisted: _SaveDict = {
            "filled_a": 5, "cost_a": 410, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
            "closed_count_a": 5, "closed_total_cost_a": 410, "closed_fees_a": 0,
            "closed_count_b": 5, "closed_total_cost_b": 90, "closed_fees_b": 0,
        }
        ledger = PositionLedger(
            "EVT-X", unit_size=5,
            ticker_a="TK-X", ticker_b="TK-X",
            is_same_ticker=True,
        )
        ledger.seed_from_saved(persisted)
        # sync_from_positions would early-return; closed must stay as seeded
        ledger.sync_from_positions(
            position_fills={Side.A: 5, Side.B: 10},
            position_costs={Side.A: 410, Side.B: 205},
        )
        assert ledger._sides[Side.A].closed_count == 5
        assert ledger._sides[Side.B].closed_count == 5
        assert ledger.open_avg_filled_price(Side.B) == 23.0

    def test_same_ticker_migration_works_without_sync_from_positions(self):
        """Same-ticker can't rely on sync_from_positions; migration must
        succeed via seed_from_saved alone."""
        old_save: _SaveDict = {
            "filled_a": 10, "cost_a": 820, "fees_a": 0,
            "filled_b": 10, "cost_b": 205, "fees_b": 0,
        }
        ledger = PositionLedger(
            "EVT-X", unit_size=5,
            ticker_a="TK-X", ticker_b="TK-X",
            is_same_ticker=True,
        )
        ledger.seed_from_saved(old_save)
        assert ledger._sides[Side.A].closed_count == 10
        assert ledger._sides[Side.B].closed_count == 10
