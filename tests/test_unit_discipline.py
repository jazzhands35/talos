"""AST-based discipline tests locking money-unit invariants from the
bps/fp100 migration.

This file currently locks two narrow-scope invariants that hold
regardless of whether legacy cents/contracts paths are still present
in the codebase:

  1. TRUST BOUNDARY: no ``float()`` calls on Kalshi wire payloads
     (identifiers ending ``_dollars`` or ``_fp``). Wire values MUST go
     through the Decimal parsers in :mod:`talos.units` to preserve exact
     bps/fp100 precision — ``float("0.038")`` silently drifts by
     ~3e-18, which compounds across aggregated sums.

  2. NO ASYNC-LOCK REGRESSION: :class:`talos.position_ledger.PositionLedger`
     must not carry a ``_mutation_lock`` attribute, and
     :class:`talos.engine.TradingEngine` must not carry a
     ``_persistence_lock`` attribute. The v11 atomicity argument
     (spec Section 8a) proves these locks are not just unnecessary
     but actively harmful — an async lock introduces an await window
     during the reconcile mutation phase that re-opens the
     interleaving race v11 was designed to close.

The full Section 9 AST test (banning raw ``100``/``10_000`` arithmetic
on money identifiers, banning ``:.2f``/``:.4f`` format specs on money
identifiers) remains a potential follow-up. The ``dollars_to_cents`` /
``fp_to_int`` deprecated helpers referenced in earlier drafts of this
docstring were deleted in Task 13b — no code or ban-list entry is
needed for them anymore.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "talos"

# Wire-identifier suffixes that MUST route through Decimal, not float.
_WIRE_SUFFIXES = ("_dollars", "_fp")


def _wire_identifier(node: ast.AST) -> str | None:
    """Return the identifier name if ``node`` names a wire-suffix var.

    Only matches:
      - ast.Name whose id ends with ``_dollars`` or ``_fp``.
      - ast.Attribute whose attr ends with ``_dollars`` or ``_fp``.

    Subscript access like ``data["yes_price_dollars"]`` is NOT matched
    (too easy to false-positive on unrelated string keys).
    """
    if isinstance(node, ast.Name) and node.id.endswith(_WIRE_SUFFIXES):
        return node.id
    if isinstance(node, ast.Attribute) and node.attr.endswith(_WIRE_SUFFIXES):
        return node.attr
    return None


def test_no_float_on_wire_identifiers() -> None:
    """Ban ``float(<wire_ident>)`` across ``src/talos/``.

    Decimal is the ONLY correct parser for Kalshi ``_dollars`` / ``_fp``
    payloads. ``float('0.038')`` silently drifts; compounds on aggregates.
    """
    violations: list[str] = []
    for py in _SRC_ROOT.rglob("*.py"):
        # units.py is the single source of truth for parsers and is
        # permitted to do whatever it needs internally.
        if py.name == "units.py":
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "float"):
                continue
            if not node.args:
                continue
            ident = _wire_identifier(node.args[0])
            if ident is None:
                continue
            rel = py.relative_to(_SRC_ROOT.parent.parent).as_posix()
            violations.append(
                f"{rel}:{node.lineno}: float({ident}) — wire payloads must "
                f"go through talos.units Decimal parsers, not float()."
            )
    assert not violations, "\n".join(violations)


def test_position_ledger_has_no_mutation_lock() -> None:
    """v11 atomicity regression guard.

    :meth:`PositionLedger.reconcile_from_fills` relies on its mutation
    phase being a single sync block with no ``await``. An ``asyncio.Lock``
    attribute would invite an ``async with self._mutation_lock:`` that
    re-introduces the await and breaks atomicity.
    """
    from talos.position_ledger import PositionLedger

    instance = PositionLedger("AST-DISCIPLINE-TEST")
    forbidden = {"_mutation_lock", "_ledger_lock", "_reconcile_lock"}
    present = {name for name in forbidden if hasattr(instance, name)}
    assert not present, (
        f"PositionLedger has v11-forbidden lock attributes: {present}. "
        f"The reconcile mutation phase MUST be a sync block; see "
        f"docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md "
        f"Section 8a 'Concurrency model (simplified from v10)'."
    )


def test_engine_has_no_persistence_lock() -> None:
    """Parallel invariant for :class:`TradingEngine`.

    ``_persist_games_now`` is explicitly sync (verified separately by
    inspect.iscoroutinefunction in test_engine_startup_gate.py).
    A persistence lock attribute on the engine would signal somebody
    is thinking about adding an async protocol around it.
    """
    from talos.engine import TradingEngine

    forbidden_attr_names = {
        "_persistence_lock",
        "_games_full_lock",
        "_persist_lock",
    }
    # Check both class-level and instance-level (if __init__ sets them).
    class_attrs = set(dir(TradingEngine))
    present = forbidden_attr_names & class_attrs
    assert not present, (
        f"TradingEngine has v11-forbidden lock attributes: {present}. "
        f"Persist callback is sync by contract."
    )


def test_persist_games_now_is_sync_not_async() -> None:
    """Direct regression guard against accidentally ``async def``ing the
    persist callback — would break the v11 atomicity proof.
    """
    import inspect

    from talos.engine import TradingEngine

    assert not inspect.iscoroutinefunction(TradingEngine._persist_games_now), (
        "TradingEngine._persist_games_now must be sync def, not async def. "
        "The v11 atomicity argument (spec Section 8a) requires persist_cb "
        "to run inside the reconcile mutation's single sync block."
    )
