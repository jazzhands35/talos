"""Wire -> internal parsers for Pydantic model validators.

Thin seam between Kalshi's wire format and Talos's internal representation.
Canonical representation is bps (``$1 = 10,000 bps``) and fp100
(``1 contract = 100 fp100``). See :mod:`talos.units` for conversion helpers.

- :func:`dollars_to_bps` — strict. Use for per-contract prices where a
  1-bps silent drift would be a real price error (raises on sub-bps
  precision at the trust boundary).
- :func:`dollars_to_bps_round` — aggregate-safe. Use for SUMS like
  ``event_exposure_dollars`` where Kalshi legitimately emits sub-bps
  precision (6-decimal values) as a byproduct of summing fractional-fill
  contributions. Half-even rounds to the nearest bps.
- :func:`fp_to_fp100` — strict. Use for all count fields.

:func:`log_unknown_fields` is unrelated to unit handling and stays here
as the shared Kalshi schema-drift surfacing helper.
"""

from __future__ import annotations

from typing import Any

import structlog

from talos.units import (
    dollars_str_to_bps,
    dollars_str_to_bps_round,
    fp_str_to_fp100,
)

logger = structlog.get_logger()

# Track which unknown fields we've already logged to avoid per-message spam.
_seen_unknown: dict[str, set[str]] = {}  # model_name -> {field_names}


def dollars_to_bps(val: Any) -> int:
    """Convert a Kalshi ``_dollars`` wire payload to internal bps (strict).

    ``'0.0488'`` -> ``488``, ``None`` -> ``0``. Raises ``ValueError`` on
    sub-bps precision — fail-closed at the trust boundary. Use for
    per-contract prices; use :func:`dollars_to_bps_round` for aggregate
    sums that can legitimately carry sub-bps precision.

    Thin alias to :func:`talos.units.dollars_str_to_bps`.
    """
    return dollars_str_to_bps(val)


def dollars_to_bps_round(val: Any) -> int:
    """Aggregate-safe Kalshi ``_dollars`` wire payload -> internal bps.

    Use for AGGREGATE money fields — sums like ``event_exposure``,
    ``realized_pnl``, ``total_cost``, ``fees_paid`` — where Kalshi
    legitimately emits sub-bps precision (6-decimal values) because
    they're summing fractional-fill contributions. Half-even rounds to
    the nearest bps.

    Use :func:`dollars_to_bps` for per-contract prices where strict
    fail-closed precision matters.

    Thin alias to :func:`talos.units.dollars_str_to_bps_round`.
    """
    return dollars_str_to_bps_round(val)


def fp_to_fp100(val: Any) -> int:
    """Convert a Kalshi ``_fp`` wire payload to internal fp100.

    ``'1.89'`` -> ``189``, ``None`` -> ``0``. Raises on sub-fp100
    precision — fail-closed.

    Thin alias to :func:`talos.units.fp_str_to_fp100`.
    """
    return fp_str_to_fp100(val)


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
