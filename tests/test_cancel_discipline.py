"""AST guard: every ``rest.cancel_order()`` call must route through
:meth:`talos.engine.TradingEngine.cancel_order_with_verify` (F36 + F33).

If a future commit adds a raw ``cancel_order()`` call anywhere outside
the verify wrapper or the rest_client.py definition site, this test
fails and blocks the commit — protecting the bps/fp100 migration's
cancel-discipline invariant.

F33 rationale: a 404 on a single stored ``order_id`` does NOT prove the
side has zero resting exposure. :class:`PositionLedger` tracks only the
first resting order per side; Kalshi supports multiple live orders on
a side. Only a full ``sync_from_orders`` resync gives ground truth.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Allowed ``cancel_order`` call sites. Keyed by (relative path, enclosing
# function name). Add entries here ONLY after confirming the caller is
# a verify-wrapped path or a definition/mock site.
ALLOWED_CALLERS: set[tuple[str, str | None]] = {
    # The verify wrapper itself — only place allowed to call the raw REST.
    ("src/talos/engine.py", "cancel_order_with_verify"),
    # rest_client.py — definition site (the attribute name matches but
    # the call is actually ``self._request``, not ``.cancel_order(...)``).
    # Still allowed as a safety net.
    ("src/talos/rest_client.py", "cancel_order"),
}

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ROOT = _REPO_ROOT / "src" / "talos"


def _enclosing_func(tree: ast.AST, target: ast.Call) -> str | None:
    """Return the name of the function/method containing ``target``.

    Walks the AST top-down; innermost enclosing function wins.
    """
    best: str | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if child is target:
                best = node.name
                break
    return best


def test_no_raw_rest_cancel_order_calls() -> None:
    """Every ``*.cancel_order(...)`` call must be in ``ALLOWED_CALLERS``."""
    offenders: list[str] = []
    for py in sorted(_SRC_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            offenders.append(f"{py}: parse error {exc}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr != "cancel_order":
                continue
            enclosing = _enclosing_func(tree, node)
            rel = py.relative_to(_REPO_ROOT).as_posix()
            key = (rel, enclosing)
            if key in ALLOWED_CALLERS:
                continue
            offenders.append(
                f"{rel}:{node.lineno}: direct rest.cancel_order() in "
                f"{enclosing!r} — use engine.cancel_order_with_verify() "
                f"instead (F36)"
            )
    assert not offenders, "F36 cancel-discipline violation(s):\n" + "\n".join(
        offenders
    )
