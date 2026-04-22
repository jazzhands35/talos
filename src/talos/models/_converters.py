"""Wire → internal parsers for Pydantic model validators.

This module is a thin seam between Kalshi's wire format and Talos's
internal representation. Pre-migration, it returned integer cents
(prices) and integer whole contracts (counts). Post-migration, the
canonical representation is bps and fp100 — see ``talos.units``.

During the Phase 1+2 bps/fp100 migration, both names coexist:

- ``dollars_to_cents`` / ``fp_to_int`` — LEGACY names, deprecated. They
  still return integer cents / whole contracts so callers that haven't
  been migrated yet keep working. Their implementations now go through
  the fail-closed Decimal parser in ``units.py`` and then reduce back
  to the legacy scale, so sub-cent / fractional inputs are silently
  truncated (which was also the pre-migration behavior).

- ``dollars_to_bps`` / ``fp_to_fp100`` — NEW names, preferred. Return
  bps / fp100 directly. Use these in new validators + any validator
  being migrated in Phase 1+2. Exact precision — sub-cent / fractional
  inputs are preserved.

The deprecated names are removed entirely in the final task of the
migration (see
docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md
Task 13), at which point every caller has been migrated.

``log_unknown_fields`` is unrelated to the unit migration and stays
here as the shared Kalshi schema-drift surfacing helper.
"""

from __future__ import annotations

from typing import Any

import structlog

from talos.units import (
    ONE_CONTRACT_FP100,
    bps_to_cents_round,
    dollars_str_to_bps,
    fp_str_to_fp100,
)

logger = structlog.get_logger()

# Track which unknown fields we've already logged to avoid per-message spam.
_seen_unknown: dict[str, set[str]] = {}  # model_name -> {field_names}


# ── New preferred names (return bps / fp100) ──────────────────────
def dollars_to_bps(val: Any) -> int:
    """Convert a Kalshi ``_dollars`` wire payload to internal bps.

    ``'0.0488'`` -> ``488``, ``None`` -> ``0``. Raises ``ValueError`` on
    sub-bps precision — fail-closed at the trust boundary.

    Thin alias to :func:`talos.units.dollars_str_to_bps`; re-exported
    here so Pydantic validators can import from the same module as the
    legacy names while we migrate incrementally.
    """
    return dollars_str_to_bps(val)


def fp_to_fp100(val: Any) -> int:
    """Convert a Kalshi ``_fp`` wire payload to internal fp100.

    ``'1.89'`` -> ``189``, ``None`` -> ``0``. Raises on sub-fp100
    precision — fail-closed.

    Thin alias to :func:`talos.units.fp_str_to_fp100`.
    """
    return fp_str_to_fp100(val)


# ── Deprecated legacy names (return cents / whole contracts) ──────
def dollars_to_cents(val: Any) -> int:
    """DEPRECATED. Convert a ``_dollars`` payload to integer cents.

    Retained so callers not yet migrated to bps keep working. Goes
    through the exact Decimal parser, then rounds (half-even) to cents
    for legacy-scale output. Sub-cent precision is therefore silently
    lost on the output — which was the pre-migration behavior, and is
    only reachable by markets that Phase 0 admission rejects.

    Migrate callers to :func:`dollars_to_bps` as part of Phase 1+2.
    Removed entirely in the final migration task.
    """
    return bps_to_cents_round(dollars_str_to_bps(val))


def fp_to_int(val: Any) -> int:
    """DEPRECATED. Convert an ``_fp`` payload to integer whole contracts.

    Fractional counts (e.g. ``'1.89'``) are floored — which was the
    pre-migration behavior, and is the silent-truncation bug this
    migration exists to eliminate. New callers MUST use
    :func:`fp_to_fp100` to retain fractional precision.

    Removed in the final migration task.
    """
    return fp_str_to_fp100(val) // ONE_CONTRACT_FP100


# ── Unchanged: schema-drift surfacing ─────────────────────────────
def log_unknown_fields(model_name: str, data: dict[str, Any], known: set[str]) -> None:
    """Log fields in *data* not in *known*, once per field per session.

    Surfaces Kalshi API schema drift at DEBUG level without per-message spam.
    """
    unknown = data.keys() - known
    if not unknown:
        return
    seen = _seen_unknown.get(model_name)
    if seen is None:
        seen = _seen_unknown[model_name] = set()
    new = unknown - seen
    if not new:
        return
    seen.update(new)
    logger.debug("unknown_api_fields", model=model_name, fields=sorted(new))
