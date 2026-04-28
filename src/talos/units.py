"""Unit arithmetic and boundary conversions. Single source of truth.

No other module in src/talos/ is permitted to:
  - use a literal 100 / 10_000 as an arithmetic operand on a price/count
  - use :.2f / :.4f format specs on price/cost/bps variables
  - call float() on a Kalshi _dollars or _fp payload

Violations are caught by tests/test_unit_discipline.py (enabled in Task 12
of the bps/fp100 migration).

Units:
  bps    — integer basis points of a dollar. $1 = 10_000 bps. 1¢ = 100 bps.
  fp100  — integer hundredths of a contract. 1 contract = 100 fp100.

Wire boundary contract (Decimal-based, fail-closed):
  - Parsers raise ValueError on sub-bps / sub-fp100 precision (no silent
    rounding at the trust boundary — that is exactly the failure mode this
    migration exists to eliminate).
  - Serializers emit 2-decimal _dollars for whole-cent values and 4-decimal
    otherwise. This is the settled wire contract: the 2-decimal path is
    proven on every cent-tick market in the portfolio; 4-decimal is
    required for sub-cent markets (DJT-class, fractional-enabled).
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

# ── Constants ─────────────────────────────────────────────────────
ONE_DOLLAR_BPS = 10_000  # $1 = 10_000 bps
ONE_CENT_BPS = 100  # 1¢ = 100 bps
ONE_CONTRACT_FP100 = 100  # 1 contract = 100 fp100

# Kalshi wire precision (decimals)
DOLLARS_WIRE_DECIMALS = 4
FP_WIRE_DECIMALS = 2

# Pagination safety cap for reconcile_from_fills (Task 9 + Task 11).
# 100 pages × 100 limit = 10_000 fills per ticker. Centralized here so
# units.py remains the single source of truth for money-migration scalars.
MAX_FILLS_PAGES = 100

_BPS_SCALE = Decimal(ONE_DOLLAR_BPS)
_FP100_SCALE = Decimal(ONE_CONTRACT_FP100)


# ── Parsing (boundary: wire → internal) ────────────────────────────
def dollars_str_to_bps(val: Any) -> int:
    """'0.0488' → 488 bps. None → 0. Raises on non-integral input.

    Examples:
      '0.53'    → 5300   (whole cents)
      '0.0488'  → 488    (sub-cent, 4 decimals — Kalshi fractional ticker)
      '0.53001' → raises ValueError (extra precision — not a bps value)
    """
    if val is None:
        return 0
    try:
        d = Decimal(str(val))
    except InvalidOperation as exc:
        raise ValueError(f"invalid _dollars payload: {val!r}") from exc
    scaled = d * _BPS_SCALE
    if scaled % 1 != 0:
        raise ValueError(
            f"_dollars payload has sub-bps precision: {val!r} "
            f"(scaled={scaled}) — refusing to round at trust boundary"
        )
    return int(scaled)


def dollars_str_to_bps_round(val: Any) -> int:
    """'20.168040' → 201680 bps (half-even rounded). None → 0.

    Aggregate-safe parser for Kalshi money fields that represent SUMS
    rather than per-contract prices. The Kalshi /portfolio endpoints
    return values like ``event_exposure_dollars='20.168040'`` — six
    decimal digits, i.e. hundredths of a bps — because they're summing
    arbitrary-precision fractional-fill contributions. The strict
    :func:`dollars_str_to_bps` fail-closes on that; for aggregates, a
    half-even round to the nearest bps is safe (loses at most 0.5 bps
    per value, which is below the cents-rounding tolerance the legacy
    integer-cents path already tolerated).

    Use this for: event_exposure, realized_pnl, total_cost, fees_paid,
    market_exposure, total_traded — any value that is a SUM across
    multiple trades/contracts.

    Do NOT use for: per-contract prices (yes_bid/ask, last_price,
    order price fields). Those should stay on the strict parser
    because a 1-bps silent shift there IS a real price drift.
    """
    if val is None:
        return 0
    try:
        d = Decimal(str(val))
    except InvalidOperation as exc:
        raise ValueError(f"invalid _dollars payload: {val!r}") from exc
    scaled = d * _BPS_SCALE
    return int(scaled.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def fp_str_to_fp100(val: Any) -> int:
    """'1.89' → 189 fp100. None → 0. Raises on non-integral input.

    Examples:
      '10.00'   → 1000  (whole contracts)
      '1.89'    → 189   (fractional — partial maker fill)
      '1.891'   → raises ValueError (extra precision — not an fp100 value)
    """
    if val is None:
        return 0
    try:
        d = Decimal(str(val))
    except InvalidOperation as exc:
        raise ValueError(f"invalid _fp payload: {val!r}") from exc
    scaled = d * _FP100_SCALE
    if scaled % 1 != 0:
        raise ValueError(
            f"_fp payload has sub-fp100 precision: {val!r} "
            f"(scaled={scaled}) — refusing to round at trust boundary"
        )
    return int(scaled)


# ── Formatting (boundary: internal → wire) ─────────────────────────
def bps_to_dollars_str(bps: int) -> str:
    """Format an internal bps value as a Kalshi ``_dollars`` wire string.

    Whole-cent values serialize to 2 decimals (``5300 → '0.53'``) matching
    the proven pre-migration wire format that cent-tick markets accept.
    Sub-cent values serialize to 4 decimals (``488 → '0.0488'``), required
    by sub-cent-tick markets (e.g. DJT at 3.8¢/96.1¢).
    """
    if bps % ONE_CENT_BPS == 0:
        return f"{Decimal(bps) / _BPS_SCALE:.2f}"
    return f"{Decimal(bps) / _BPS_SCALE:.{DOLLARS_WIRE_DECIMALS}f}"


def fp100_to_fp_str(fp100: int) -> str:
    """189 fp100 → '1.89' (2-decimal Kalshi wire format)."""
    return f"{Decimal(fp100) / _FP100_SCALE:.{FP_WIRE_DECIMALS}f}"


# ── Helpers ────────────────────────────────────────────────────────
def complement_bps(price_bps: int) -> int:
    """NO = 1 - YES in dollar space. 488 bps → 9_512 bps."""
    return ONE_DOLLAR_BPS - price_bps


def cents_to_bps(cents: int) -> int:
    """Operator-facing cents value → internal bps. Lossless."""
    return cents * ONE_CENT_BPS


def bps_to_cents_round(bps: int) -> int:
    """Internal bps → display cents (half-even round). Lossy.

    488 bps (4.88¢) → 5¢; 450 bps (4.50¢) → 4¢ (banker's round to even).
    """
    quotient = Decimal(bps) / Decimal(ONE_CENT_BPS)
    return int(quotient.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def contracts_to_fp100(contracts: int) -> int:
    """Operator-facing whole-contract quantity → internal fp100."""
    return contracts * ONE_CONTRACT_FP100


def fp100_to_whole_contracts_floor(fp100: int) -> int:
    """Submittable whole-contract quantity from a fractional fp100 count."""
    return fp100 // ONE_CONTRACT_FP100


# ── Display formatters ─────────────────────────────────────────────
def format_bps_as_cents(bps: int) -> str:
    """488 bps → '4.88¢'. Display only."""
    return f"{Decimal(bps) / Decimal(ONE_CENT_BPS):.2f}¢"


def format_bps_as_dollars_display(bps: int) -> str:
    """488 bps → '$0.05'. Display only (2-decimal, rounded)."""
    return f"${Decimal(bps) / _BPS_SCALE:.2f}"


def format_fp100_as_contracts(fp100: int) -> str:
    """189 fp100 → '1.89'. Display only."""
    return f"{Decimal(fp100) / _FP100_SCALE:.2f}"


# ── Fee arithmetic (uses internal bps) ─────────────────────────────
def quadratic_fee_bps(price_bps: int, *, rate: float) -> int:
    """Per-contract maker fee in bps. Rounded half-even.

    Formula in dollar space: fee = rate × price × (1 − price).
    In bps: fee_bps = rate × price_bps × (ONE_DOLLAR_BPS − price_bps) / ONE_DOLLAR_BPS.
    """
    fee_d = (
        Decimal(str(rate)) * Decimal(price_bps) * Decimal(complement_bps(price_bps)) / _BPS_SCALE
    )
    return int(fee_d.to_integral_value())
