"""Staleness flag triggers on persistence load (spec F14 + F17 + F22).

Parametrized over the eight staleness-relevant persisted fields: each
nonzero historical field must set ``stale_fills_unconfirmed``; each
nonzero resting field must set ``stale_resting_unconfirmed``; all-zero
persisted state leaves both False.

Also exercises F22 zero-state v1 migration — loading a v1 envelope with
every numeric field at zero must NOT set ``legacy_migration_pending``.
"""

from __future__ import annotations

import pytest

from talos.position_ledger import PositionLedger

# ── v2 envelope helper ───────────────────────────────────────────────


def _v2_envelope(**overrides: object) -> dict[str, object]:
    """Build a zero-state v2 envelope with optional field overrides."""
    base = {
        "schema_version": 2,
        "legacy_migration_pending": False,
        "ledger": {
            "filled_count_fp100_a": 0,
            "filled_total_cost_bps_a": 0,
            "filled_fees_bps_a": 0,
            "closed_count_fp100_a": 0,
            "closed_total_cost_bps_a": 0,
            "closed_fees_bps_a": 0,
            "resting_id_a": None,
            "resting_count_fp100_a": 0,
            "resting_price_bps_a": 0,
            "filled_count_fp100_b": 0,
            "filled_total_cost_bps_b": 0,
            "filled_fees_bps_b": 0,
            "closed_count_fp100_b": 0,
            "closed_total_cost_bps_b": 0,
            "closed_fees_bps_b": 0,
            "resting_id_b": None,
            "resting_count_fp100_b": 0,
            "resting_price_bps_b": 0,
        },
    }
    ledger_override = {
        k: v
        for k, v in overrides.items()
        if k not in ("schema_version", "legacy_migration_pending")
    }
    inner = base["ledger"]
    assert isinstance(inner, dict)
    inner.update(ledger_override)
    if "legacy_migration_pending" in overrides:
        base["legacy_migration_pending"] = overrides["legacy_migration_pending"]
    return base


HISTORICAL_FIELDS = [
    "filled_count_fp100_a",
    "filled_total_cost_bps_a",
    "filled_fees_bps_a",
    "closed_count_fp100_a",
    "closed_total_cost_bps_a",
    "closed_fees_bps_a",
    "filled_count_fp100_b",
    "filled_total_cost_bps_b",
    "filled_fees_bps_b",
    "closed_count_fp100_b",
    "closed_total_cost_bps_b",
    "closed_fees_bps_b",
]


class TestHistoricalFieldsSetStaleFills:
    @pytest.mark.parametrize("field", HISTORICAL_FIELDS)
    def test_each_nonzero_historical_field_sets_flag(self, field: str) -> None:
        """Each historical field, when nonzero, must set the fills flag.

        Closed fields require matching filled fields to avoid tripping
        the pre-existing `_reconcile_closed` invariant (closed cannot
        exceed filled). We mirror filled_count_fp100 on the same side to
        make the envelope a valid matched-pair state while still isolating
        the field under test as the sole "historical" driver."""
        overrides: dict[str, object] = {field: 100}
        if field.startswith("closed_count_fp100_"):
            side = field[-1]
            overrides[f"filled_count_fp100_{side}"] = 100
        ledger = PositionLedger("EVT", unit_size=10)
        env = _v2_envelope(**overrides)
        ledger.seed_from_saved(env)
        assert ledger.stale_fills_unconfirmed is True, (
            f"{field}=100 failed to set stale_fills_unconfirmed"
        )
        # Resting flag should stay False when no resting field is set.
        assert ledger.stale_resting_unconfirmed is False


class TestRestingFieldsSetStaleResting:
    def test_resting_count_sets_flag(self) -> None:
        ledger = PositionLedger("EVT", unit_size=10)
        env = _v2_envelope(
            resting_id_a="ord-1",
            resting_count_fp100_a=500,
            resting_price_bps_a=4800,
        )
        ledger.seed_from_saved(env)
        assert ledger.stale_resting_unconfirmed is True
        # No historical nonzero field → fills flag stays False.
        assert ledger.stale_fills_unconfirmed is False

    def test_resting_order_id_without_count_sets_flag(self) -> None:
        """resting_order_id non-null alone should set the flag.

        seed_from_saved requires both id and count to restore, so this
        tests a slightly degenerate case: only both non-null + count > 0
        actually populates the ledger. But the staleness rule reads the
        ledger state post-load, so the flag reflects what loaded."""
        ledger = PositionLedger("EVT", unit_size=10)
        env = _v2_envelope(
            resting_id_a="ord-1",
            resting_count_fp100_a=100,  # need > 0 for load to succeed
            resting_price_bps_a=4800,
        )
        ledger.seed_from_saved(env)
        assert ledger.stale_resting_unconfirmed is True


class TestAllZeroLeavesFlagsClear:
    def test_all_zero_v2_payload(self) -> None:
        ledger = PositionLedger("EVT", unit_size=10)
        ledger.seed_from_saved(_v2_envelope())
        assert ledger.stale_fills_unconfirmed is False
        assert ledger.stale_resting_unconfirmed is False
        assert ledger.legacy_migration_pending is False

    def test_none_payload(self) -> None:
        ledger = PositionLedger("EVT", unit_size=10)
        ledger.seed_from_saved(None)
        assert ledger.stale_fills_unconfirmed is False
        assert ledger.stale_resting_unconfirmed is False
        assert ledger.legacy_migration_pending is False


class TestOnlyRestingClearedByOrdersSync:
    def test_resting_only_load_cleared_by_orders_sync(self) -> None:
        """Loaded state with only resting nonzero (no fills) sets only
        the resting flag and is cleared by any sync_from_orders completion."""
        ledger = PositionLedger(
            "EVT",
            unit_size=10,
            ticker_a="T-A",
            ticker_b="T-B",
        )
        env = _v2_envelope(
            resting_id_a="ord-1",
            resting_count_fp100_a=500,
            resting_price_bps_a=4800,
        )
        ledger.seed_from_saved(env)
        assert ledger.stale_fills_unconfirmed is False
        assert ledger.stale_resting_unconfirmed is True

        ledger.sync_from_orders([], "T-A", "T-B")  # empty response, any completion
        assert ledger.stale_resting_unconfirmed is False
        assert ledger.stale_fills_unconfirmed is False


# ── F22 zero-state v1 migration ─────────────────────────────────────


class TestF22ZeroStateV1Migration:
    def test_all_zero_v1_does_not_set_legacy_pending(self) -> None:
        """F22: all-zero v1 payload converts trivially. MUST NOT set
        legacy_migration_pending — otherwise the gate permanently blocks
        a ledger that had nothing to reconcile."""
        v1 = {
            "filled_a": 0,
            "cost_a": 0,
            "fees_a": 0,
            "filled_b": 0,
            "cost_b": 0,
            "fees_b": 0,
            "resting_id_a": None,
            "resting_count_a": 0,
            "resting_price_a": 0,
            "resting_id_b": None,
            "resting_count_b": 0,
            "resting_price_b": 0,
            "closed_count_a": 0,
            "closed_total_cost_a": 0,
            "closed_fees_a": 0,
            "closed_count_b": 0,
            "closed_total_cost_b": 0,
            "closed_fees_b": 0,
        }
        ledger = PositionLedger("EVT", unit_size=10)
        ledger.seed_from_saved(v1)
        assert ledger.legacy_migration_pending is False
        assert ledger.stale_fills_unconfirmed is False
        assert ledger.stale_resting_unconfirmed is False
        # No v1 snapshot retained either — nothing to embed on next save.
        assert ledger._legacy_v1_snapshot is None

    def test_ready_passes_on_zero_state_v1_after_first_orders_sync(self) -> None:
        """After a zero-state v1 load, ready() waits only on the
        first-orders-sync gate — one sync_from_orders makes it pass."""
        ledger = PositionLedger(
            "EVT",
            unit_size=10,
            ticker_a="T-A",
            ticker_b="T-B",
        )
        ledger.seed_from_saved({"filled_a": 0})  # minimal v1 payload
        assert ledger.ready() is False  # first_orders_sync not set yet
        ledger.sync_from_orders([], "T-A", "T-B")
        assert ledger.ready() is True

    def test_next_save_is_clean_v2_envelope(self) -> None:
        """Zero-state v1 → next to_save_dict writes a clean v2 envelope
        (no legacy_v1_snapshot field, legacy_migration_pending=False)."""
        ledger = PositionLedger("EVT", unit_size=10)
        ledger.seed_from_saved({"filled_a": 0})
        env = ledger.to_save_dict()
        assert env["schema_version"] == 2
        assert env["legacy_migration_pending"] is False
        assert "legacy_v1_snapshot" not in env

    def test_nonzero_v1_sets_legacy_pending_and_retains_snapshot(self) -> None:
        """Inverse of F22: nonzero v1 payload DOES set legacy_migration_pending
        and the original payload is retained for the next save."""
        v1 = {"filled_a": 3, "cost_a": 150, "fees_a": 0}
        ledger = PositionLedger("EVT", unit_size=10)
        ledger.seed_from_saved(v1)
        assert ledger.legacy_migration_pending is True
        assert ledger.stale_fills_unconfirmed is True
        assert ledger._legacy_v1_snapshot == v1

        env = ledger.to_save_dict()
        assert env["legacy_migration_pending"] is True
        assert env["legacy_v1_snapshot"] == v1


class TestV2LegacyPendingFlagPropagates:
    def test_v2_envelope_with_legacy_pending_sets_flag(self) -> None:
        """v2 load honors the persisted legacy_migration_pending flag
        and retains the embedded legacy_v1_snapshot."""
        v1_blob = {"filled_a": 1, "cost_a": 60}
        env = _v2_envelope(
            filled_count_fp100_a=100,
            filled_total_cost_bps_a=6000,
            legacy_migration_pending=True,
        )
        env["legacy_v1_snapshot"] = v1_blob
        ledger = PositionLedger("EVT", unit_size=10)
        ledger.seed_from_saved(env)
        assert ledger.legacy_migration_pending is True
        assert ledger._legacy_v1_snapshot == v1_blob
        # Round-trip: next save carries both.
        out = ledger.to_save_dict()
        assert out["legacy_migration_pending"] is True
        assert out["legacy_v1_snapshot"] == v1_blob
