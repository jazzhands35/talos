"""AST-based discipline tests locking money-unit invariants from the
bps/fp100 migration.

Three invariants enforced on every import of :mod:`talos`:

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

  3. UNIT DISCIPLINE (spec Section 9): no raw integer literal ``100``
     or ``10_000`` as an arithmetic operand on a money-matching
     identifier, and no ``:.2f`` / ``:.4f`` format specs on a
     money-matching identifier. Use named constants from
     :mod:`talos.units` (``ONE_CENT_BPS``, ``ONE_DOLLAR_BPS``) and the
     display-format helpers instead. Allowlist entries below carry a
     one-line rationale.
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


# ── Section 9 — Unit Discipline ────────────────────────────────────
#
# Identifiers whose names contain any of these substrings (case-insensitive)
# are considered MONEY identifiers. Literal ``100`` / ``10_000`` arithmetic
# on them, and lossy ``:.2f`` / ``:.4f`` format specs on them, are banned
# outside the allowlist. Use named constants (``ONE_CENT_BPS``,
# ``ONE_DOLLAR_BPS``, ``ONE_CONTRACT_FP100``) and display helpers
# (``format_bps_as_dollars_display``, ``format_bps_as_cents``,
# ``format_fp100_as_contracts``) from :mod:`talos.units` instead.
_MONEY_IDENT_SUBSTRINGS = (
    "price",
    "cost",
    "bps",
    "fp100",
    "fees",
    "edge",
    "pnl",
    "exposure",
    "revenue",
    "balance",
    "traded",
    "resting",
    "filled",
    "closed",
)

_BANNED_LITERALS = {100, 10_000}
_BANNED_FORMAT_SUFFIXES = (".2f", ".4f")

# Allowlist entries: (posix_path, lineno). Keep small. Each entry
# documents a legitimate exception. Two legit categories at migration
# landing time:
#
# 1. ``fees.py`` still exposes the pre-migration cents-scale API as a
#    convenience layer alongside the bps-aware _bps siblings. Inside
#    those legacy functions, literal ``100`` IS cents-per-dollar /
#    cents-per-unit — the real thing, not a bps proxy. These functions
#    will be deleted when the last caller migrates, but that's post-PR.
# 2. ``ui/event_review.py`` and ``ui/widgets.py`` display cents-valued
#    fields from models that are NOT part of the bps migration
#    (EventPositionSummary.locked_profit_cents / exposure_cents,
#    PortfolioPanel._cash / _exposure / _locked internal cents storage,
#    LegSummary.total_fill_cost cents). These are display-only `/100` to
#    convert whole cents into dollars for render. Using ``ONE_CENT_BPS``
#    would be semantically wrong (that constant is "bps per cent", not
#    "cents per dollar") and would mislead a reader about what layer
#    the value is in.
_ALLOWLIST: frozenset[tuple[str, int]] = frozenset(
    {
        # Category 1: fees.py legacy cents-scale convenience API.
        ("src/talos/fees.py", 66),   # quadratic_fee: cents × (100 - cents) × rate / 100
        ("src/talos/fees.py", 109),  # max_profitable_price: other_cost budget in cents
        ("src/talos/fees.py", 180),  # scenario_pnl: filled * 100 cents-per-contract payout
        ("src/talos/fees.py", 181),  # scenario_pnl: mirror
        # Category 2: legacy cents display on non-migrated sqlite columns / cents stores.
        ("src/talos/ui/event_review.py", 103),  # pnl (cents, sqlite col) → $N.NN display
        ("src/talos/ui/event_review.py", 120),  # pnl (cents, sqlite col) → $N.NN display
        ("src/talos/ui/event_review.py", 121),  # revenue (cents, sqlite col) → $N.NN display
        ("src/talos/ui/widgets.py", 51),        # kalshi_pnl param (cents) → $N.NN display
        ("src/talos/ui/widgets.py", 254),       # pnl_cents → $N.NN display
        ("src/talos/ui/widgets.py", 821),       # exposure (cents post bps→cents round) → display
        ("src/talos/ui/widgets.py", 926),       # _exposure (cents internal store) → display
    }
)


def _is_money_identifier(node: ast.AST) -> str | None:
    """Return lowercased identifier name if ``node`` names a money field."""
    name: str | None = None
    if isinstance(node, ast.Name):
        name = node.id
    elif isinstance(node, ast.Attribute):
        name = node.attr
    if name is None:
        return None
    lower = name.lower()
    if any(sub in lower for sub in _MONEY_IDENT_SUBSTRINGS):
        return name
    return None


def _skip_file(py: Path) -> bool:
    """units.py is the single source of truth for unit arithmetic.
    _converters.py re-exports units.py parsers (thin aliases, allowed).
    fees.py uses units constants throughout — all arithmetic is in bps
    already, but its signatures take ``rate`` multipliers like ``0.07`` and
    the formula has literal ``100`` that is LEGIT cents-math on
    whole-cent prices (not a money-unit bug — operator-facing API).
    """
    return py.name in {"units.py", "_converters.py"}


def test_no_raw_unit_arithmetic_on_money_identifiers() -> None:
    """Section 9 ban #1: no ``money_ident * 100`` or ``money_ident / 100``.

    Use :data:`talos.units.ONE_CENT_BPS` / :data:`ONE_DOLLAR_BPS` /
    :data:`ONE_CONTRACT_FP100` as the scale constants instead. The
    literal ``100`` is indistinguishable from cents-scaled math at the
    AST level, and the whole migration exists because cents-scaled
    math silently drifts on sub-cent inputs.
    """
    violations: list[str] = []
    for py in _SRC_ROOT.rglob("*.py"):
        if _skip_file(py):
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp):
                continue
            # Check each operand pair (literal, identifier) in either order.
            for literal_side, other_side in (
                (node.left, node.right),
                (node.right, node.left),
            ):
                if not (
                    isinstance(literal_side, ast.Constant)
                    and literal_side.value in _BANNED_LITERALS
                ):
                    continue
                ident = _is_money_identifier(other_side)
                if ident is None:
                    continue
                rel = py.relative_to(_SRC_ROOT.parent.parent).as_posix()
                if (rel, node.lineno) in _ALLOWLIST:
                    continue
                violations.append(
                    f"{rel}:{node.lineno}: {ident} * / / {literal_side.value} — use "
                    f"named constants from talos.units (ONE_CENT_BPS=100, "
                    f"ONE_DOLLAR_BPS=10_000, ONE_CONTRACT_FP100=100) instead."
                )
    assert not violations, "\n".join(violations)


def test_no_lossy_format_spec_on_money_identifiers() -> None:
    """Section 9 ban #2: no ``f"{money_ident:.2f}"`` / ``:.4f``.

    ``:.2f`` on a bps value displays 100.00× the actual dollar amount.
    ``:.4f`` on a cents value displays the wrong precision. Both are
    foot-guns. Use the display helpers from :mod:`talos.units`.
    """
    violations: list[str] = []
    for py in _SRC_ROOT.rglob("*.py"):
        if _skip_file(py):
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FormattedValue):
                continue
            if node.format_spec is None:
                continue
            # format_spec is a JoinedStr whose Constant values carry the
            # literal format suffix like ".2f".
            spec_text = ""
            if isinstance(node.format_spec, ast.JoinedStr):
                for piece in node.format_spec.values:
                    if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                        spec_text += piece.value
            if not any(spec_text.endswith(s) for s in _BANNED_FORMAT_SUFFIXES):
                continue
            ident = _is_money_identifier(node.value)
            if ident is None:
                continue
            rel = py.relative_to(_SRC_ROOT.parent.parent).as_posix()
            if (rel, node.lineno) in _ALLOWLIST:
                continue
            violations.append(
                f"{rel}:{node.lineno}: f\"{{{ident}:{spec_text}}}\" — use "
                f"talos.units.format_bps_as_dollars_display / format_bps_as_cents "
                f"/ format_fp100_as_contracts instead."
            )
    assert not violations, "\n".join(violations)
