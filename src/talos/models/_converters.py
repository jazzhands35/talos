"""Shared FP migration helpers for Pydantic model validators.

Kalshi's API migrated from integer fields to fixed-point string fields
(e.g., ``yes_price`` int → ``yes_price_dollars`` str). These helpers
convert the new format back to internal integer representation.

Single source of truth — all model validators import from here.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

# Track which unknown fields we've already logged to avoid per-message spam.
_seen_unknown: set[tuple[str, str]] = set()  # (model_name, field_name)


def dollars_to_cents(val: Any) -> int:
    """Convert a ``_dollars`` string/float to integer cents.

    ``'0.52'`` → ``52``, ``0.52`` → ``52``, ``None`` → ``0``.
    """
    if val is None:
        return 0
    return round(float(val) * 100)


def fp_to_int(val: Any) -> int:
    """Convert an ``_fp`` string to integer.

    ``'10.00'`` → ``10``, ``10.0`` → ``10``, ``None`` → ``0``.
    """
    if val is None:
        return 0
    return int(float(val))


def log_unknown_fields(model_name: str, data: dict[str, Any], known: set[str]) -> None:
    """Log fields in *data* not in *known*, once per field per session.

    Surfaces Kalshi API schema drift at DEBUG level without per-message spam.
    """
    unknown = set(data.keys()) - known
    if not unknown:
        return
    new = unknown - {f for m, f in _seen_unknown if m == model_name}
    if not new:
        return
    for field in new:
        _seen_unknown.add((model_name, field))
    logger.debug("unknown_api_fields", model=model_name, fields=sorted(new))
