"""Tests for the bps/fp100 internal state and exact-precision API of PositionLedger.

Companion to tests/test_position_ledger.py. That file exercises the legacy
cents/contracts public API (record_fill, filled_count, ...) and verifies the
accessors still return cents/contracts after the migration. This file exercises:

  1. Legacy-API ↔ _bps/_fp100 accessor parity (cents/contracts × 100 = bps/fp100).
  2. Exact-precision record_fill_bps — the 1.89-contract fractional-fill case that
     pre-migration code silently truncated to 1.
  3. Persistence v1 (cents/contracts) → v2 (bps/fp100) load conversion.
  4. Persistence v2 → v2 round-trip via to_save_dict / seed_from_saved.
  5. Mixed round-trip — save v2, reload, verify legacy accessors return original
     cents/contracts.
"""
from __future__ import annotations

from talos.position_ledger import PositionLedger, Side


# ── 1. Legacy API ↔ _bps/_fp100 accessor parity ──────────────────────
class TestLegacyAndExactAccessorParity:
    def test_cents_fill_exposes_matching_bps_fp100(self):
        ledger = PositionLedger("EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=5, price=48, fees=1)

        # Legacy accessors (cents/contracts)
        assert ledger.filled_count(Side.A) == 5
        assert ledger.filled_total_cost(Side.A) == 240  # 5 * 48
        assert ledger.filled_fees(Side.A) == 1

        # Exact-precision accessors (bps/fp100) — should be legacy ×100
        assert ledger.filled_count_fp100(Side.A) == 500
        assert ledger.filled_total_cost_bps(Side.A) == 24_000
        assert ledger.filled_fees_bps(Side.A) == 100

    def test_resting_cents_exposes_matching_bps(self):
        ledger = PositionLedger("EVT-1", unit_size=10)
        ledger.record_resting(Side.A, order_id="ord-1", count=10, price=47)
        assert ledger.resting_count(Side.A) == 10
        assert ledger.resting_price(Side.A) == 47
        assert ledger.resting_count_fp100(Side.A) == 1000
        assert ledger.resting_price_bps(Side.A) == 4700

    def test_avg_filled_price_both_accessors(self):
        ledger = PositionLedger("EVT-1", unit_size=10)
        ledger.record_fill(Side.A, count=4, price=50)
        ledger.record_fill(Side.A, count=6, price=45)
        # Cost: 4*50 + 6*45 = 470 cents over 10 contracts → 47.0c
        assert ledger.avg_filled_price(Side.A) == 47.0
        # Exact-precision bps-per-contract: 470*100 bps / 10 contracts = 4700 bps
        assert ledger.avg_filled_price_bps(Side.A) == 4700.0


# ── 2. Exact-precision fractional fill (the 1.89-contract MARJ case) ──
class TestRecordFillBps:
    def test_fractional_fill_at_sub_cent_price_preserved(self):
        """1.89 contracts (189 fp100) at 48.88¢ (4888 bps) — no silent truncation."""
        ledger = PositionLedger("EVT-1", unit_size=10)
        ledger.record_fill_bps(Side.A, count_fp100=189, price_bps=4888)

        assert ledger.filled_count_fp100(Side.A) == 189
        # Cost: 189 * 4888 / 100 = 9238 bps (integer division)
        assert ledger.filled_total_cost_bps(Side.A) == 9238
        # Legacy accessors floor/round to cents/contracts
        assert ledger.filled_count(Side.A) == 1  # floor of 1.89
        # 9238 bps = 92.38¢ → half-even round → 92
        assert ledger.filled_total_cost(Side.A) == 92

    def test_record_fill_bps_reduces_resting_fp100(self):
        """Partial fill shrinks resting by the fp100 fill count."""
        ledger = PositionLedger("EVT-1", unit_size=10)
        ledger.record_resting_bps(
            Side.A, order_id="ord-1", count_fp100=1000, price_bps=4500
        )
        # Fractional fill against the resting order
        ledger.record_fill_bps(Side.A, count_fp100=189, price_bps=4500)
        assert ledger.resting_count_fp100(Side.A) == 811  # 1000 - 189
        assert ledger.resting_order_id(Side.A) == "ord-1"

    def test_record_fill_bps_fees_accumulate(self):
        ledger = PositionLedger("EVT-1", unit_size=10)
        ledger.record_fill_bps(Side.A, count_fp100=500, price_bps=4800, fees_bps=25)
        ledger.record_fill_bps(Side.A, count_fp100=500, price_bps=4800, fees_bps=30)
        assert ledger.filled_fees_bps(Side.A) == 55


# ── 3. v1 (cents/contracts) → v2 (bps/fp100) load conversion ─────────
class TestPersistenceV1Load:
    def test_v1_payload_scales_x100_into_bps_fp100(self):
        """Legacy v1 save (pre-migration shape) loads and ×100 scales cleanly."""
        data: dict[str, object] = {
            "filled_a": 5, "cost_a": 250, "fees_a": 3,
            "filled_b": 5, "cost_b": 90, "fees_b": 1,
            "closed_count_a": 5, "closed_total_cost_a": 250, "closed_fees_a": 3,
            "closed_count_b": 5, "closed_total_cost_b": 90, "closed_fees_b": 1,
            "resting_id_a": "ord-a", "resting_count_a": 2, "resting_price_a": 48,
            "resting_id_b": None, "resting_count_b": 0, "resting_price_b": 0,
        }
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved(data)

        # Filled/closed scaled ×100
        assert ledger.filled_count_fp100(Side.A) == 500
        assert ledger.filled_total_cost_bps(Side.A) == 25_000
        assert ledger.filled_fees_bps(Side.A) == 300
        assert ledger.closed_count_fp100(Side.A) == 500
        assert ledger.closed_total_cost_bps(Side.A) == 25_000
        assert ledger.closed_fees_bps(Side.A) == 300

        # Resting side-A scaled ×100; side-B has no resting order
        assert ledger.resting_order_id(Side.A) == "ord-a"
        assert ledger.resting_count_fp100(Side.A) == 200
        assert ledger.resting_price_bps(Side.A) == 4800
        assert ledger.resting_order_id(Side.B) is None
        assert ledger.resting_count_fp100(Side.B) == 0

        # Legacy accessors return original cents/contracts values
        assert ledger.filled_count(Side.A) == 5
        assert ledger.filled_total_cost(Side.A) == 250
        assert ledger.filled_fees(Side.A) == 3


# ── 4. v2 → v2 round-trip ────────────────────────────────────────────
class TestPersistenceV2RoundTrip:
    def test_save_v2_envelope_reload_equality(self):
        """to_save_dict emits v2; reload produces a state-equivalent ledger."""
        src = PositionLedger("EVT-X", unit_size=5)
        src.record_fill(Side.A, count=5, price=50)
        src.record_fill(Side.B, count=5, price=45)
        # Side A now has an open fractional fill (tests precision preservation)
        src.record_fill_bps(Side.A, count_fp100=189, price_bps=4888)
        src.record_resting(Side.A, order_id="ord-a", count=3, price=49)

        saved = src.to_save_dict()
        assert saved["schema_version"] == 2
        assert isinstance(saved["ledger"], dict)

        dest = PositionLedger("EVT-X", unit_size=5)
        dest.seed_from_saved(saved)

        for side in (Side.A, Side.B):
            tag = f"side {side}"
            assert dest.filled_count_fp100(side) == src.filled_count_fp100(side), tag
            assert dest.filled_total_cost_bps(side) == src.filled_total_cost_bps(side), tag
            assert dest.filled_fees_bps(side) == src.filled_fees_bps(side), tag
            assert dest.closed_count_fp100(side) == src.closed_count_fp100(side), tag
            assert dest.closed_total_cost_bps(side) == src.closed_total_cost_bps(side), tag
            assert dest.closed_fees_bps(side) == src.closed_fees_bps(side), tag
            assert dest.resting_order_id(side) == src.resting_order_id(side), tag
            assert dest.resting_count_fp100(side) == src.resting_count_fp100(side), tag
            assert dest.resting_price_bps(side) == src.resting_price_bps(side), tag

    def test_v2_payload_missing_ledger_key_is_no_op(self):
        """Defensive: a malformed v2 envelope (missing inner 'ledger') leaves state empty."""
        ledger = PositionLedger("EVT-X", unit_size=5)
        ledger.seed_from_saved({"schema_version": 2})
        assert ledger.filled_count_fp100(Side.A) == 0
        assert ledger.filled_count_fp100(Side.B) == 0


# ── 5. Mixed round-trip — legacy accessors return original cents/contracts ──
class TestPersistenceMixedRoundTrip:
    def test_save_v2_reload_legacy_accessors_match_original(self):
        """v2 save round-trip preserves cents/contracts semantics for legacy callers."""
        src = PositionLedger("EVT-X", unit_size=10)
        src.record_fill(Side.A, count=10, price=50, fees=2)
        src.record_fill(Side.B, count=10, price=47, fees=1)
        src.record_resting(Side.B, order_id="ord-b", count=5, price=46)

        dest = PositionLedger("EVT-X", unit_size=10)
        dest.seed_from_saved(src.to_save_dict())

        assert dest.filled_count(Side.A) == 10
        assert dest.filled_total_cost(Side.A) == 500
        assert dest.filled_fees(Side.A) == 2
        assert dest.filled_count(Side.B) == 10
        assert dest.filled_total_cost(Side.B) == 470
        assert dest.filled_fees(Side.B) == 1
        assert dest.resting_order_id(Side.B) == "ord-b"
        assert dest.resting_count(Side.B) == 5
        assert dest.resting_price(Side.B) == 46


# ── 6. Task 13d defensive bug fixes ──────────────────────────────────
class TestResetPairBumpsMutationGeneration:
    """13d-2: reset_pair() must bump _mutation_generation so any pending
    reconcile mismatch captured before the reset is flagged stale on
    accept_pending_mismatch (F19 generation guard).
    """

    def test_reset_pair_increments_generation(self) -> None:
        ledger = PositionLedger("EVT-RESET", unit_size=5)
        ledger.record_fill(Side.A, count=5, price=50)
        gen_before = ledger._mutation_generation
        ledger.reset_pair()
        assert ledger._mutation_generation == gen_before + 1

    def test_reset_pair_invalidates_pending_mismatch_gen(self) -> None:
        """Before 13d-2, reset_pair left _mutation_generation unchanged, so a
        pending mismatch captured at gen G could still be accepted after a
        reset (which should have invalidated any pre-reset rebuild).
        """
        ledger = PositionLedger("EVT-RESET-GEN", unit_size=5)
        ledger.record_fill(Side.A, count=5, price=50)
        # Simulate a captured pending mismatch at the current gen.
        ledger._pending_mismatch_gen = ledger._mutation_generation
        ledger.reset_pair()
        # After reset, the captured gen is now stale relative to current.
        assert ledger._pending_mismatch_gen != ledger._mutation_generation


class TestReconcileClosedNegativeStateGuard:
    """13d-1: _reconcile_closed must defensively skip when closed > filled
    on either side (corruption state) instead of dividing by a negative
    open_count_fp100. Before the guard, the divide-by-zero / negative-rebuild
    path could silently produce wrong closed-bucket values.
    """

    def test_closed_exceeds_filled_skips_without_crash(self, caplog) -> None:
        import logging
        caplog.set_level(logging.WARNING)
        ledger = PositionLedger("EVT-NEG", unit_size=5)
        # Manually induce a corruption state: closed > filled on Side A.
        # (Normally unreachable via public API; tests the defensive path.)
        s = ledger._sides[Side.A]
        s.filled_count_fp100 = 500   # 5 contracts
        s.closed_count_fp100 = 600   # 6 contracts — impossible state
        s.filled_total_cost_bps = 25000
        s.closed_total_cost_bps = 30000
        # Give Side B a normal state so unit-close logic would otherwise fire.
        b = ledger._sides[Side.B]
        b.filled_count_fp100 = 500
        b.filled_total_cost_bps = 23500
        # Must NOT raise ZeroDivisionError or silently apply a negative rebuild.
        ledger._reconcile_closed(path="test")
        # Closed-bucket values unchanged (defensive early-return).
        assert s.closed_count_fp100 == 600
        assert s.closed_total_cost_bps == 30000
        # Warning logged for forensic visibility.
        assert any("closed_exceeds_filled" in r.getMessage() for r in caplog.records)
