"""Maker fee calculations for Kalshi NO+NO arbitrage.

Kalshi uses a **quadratic** fee model on game markets:
    fee_per_contract_dollars = RATE × P × (1 − P)
where P is the price in dollars (bps / 10_000 internally). In bps:
    fee_bps = round(RATE × price_bps × (ONE_DOLLAR_BPS − price_bps) / ONE_DOLLAR_BPS)

The rate is a Kalshi-wide constant (see Kairos's KALSHI_FEES.md):
    0.0175   — full rate (no rebate)
    0.00875  — maker rebate rate (halve when enrolled)

Fees are charged at fill time, not settlement. The ``Series.fee_multiplier``
field from the Kalshi API is NOT a reliable source for the maker rate —
Kalshi has been observed returning sentinel values like 1.0 on both
``quadratic`` and ``quadratic_with_maker_fees`` series. Always use the
constants below, gated by ``fee_type`` only to zero out fee-free markets.

The canonical API is the ``_bps`` variant of each function: inputs and
outputs in internal bps ($1 = 10_000 bps). The legacy cents-scale API
was deleted after the bps/fp100 migration landed (see PR #1 + cleanup
PR on branch ``chore/bps-fp100-cleanup``).
"""

from __future__ import annotations

from talos.units import (
    ONE_CENT_BPS,
    ONE_CONTRACT_FP100,
    ONE_DOLLAR_BPS,
    quadratic_fee_bps,
)

KALSHI_FEE_RATE = 0.0175
KALSHI_MAKER_REBATE_RATE = 0.00875

# Back-compat alias. Callers that expect the historical symbol keep working;
# new code should use ``KALSHI_FEE_RATE`` or ``effective_fee_rate`` directly.
MAKER_FEE_RATE = KALSHI_FEE_RATE


def effective_fee_rate(fee_type: str, *, maker_rebate: bool = False) -> float:
    """Return the effective per-trade fee rate for a given ``fee_type``.

    ``maker_rebate`` halves the rate for accounts enrolled in Kalshi's
    maker rebate program. Defaults to the full rate.
    """
    if fee_type in ("fee_free", "no_fee"):
        return 0.0
    return KALSHI_MAKER_REBATE_RATE if maker_rebate else KALSHI_FEE_RATE


def coerce_persisted_fee_rate(fee_type: str, fee_rate: float) -> float:
    """Heal previously persisted ``fee_rate`` values that predate the
    ``effective_fee_rate`` cleanup.

    Any cached rate that isn't one of the known Kalshi constants
    (0, 0.00875, 0.0175) is treated as corrupt metadata and replaced with
    the default for the given ``fee_type``.
    """
    if fee_rate in (0.0, KALSHI_MAKER_REBATE_RATE, KALSHI_FEE_RATE):
        # Still validate against fee_type: a zero rate on a paying type
        # is suspect and should round-trip to the default.
        if fee_rate == 0.0 and fee_type not in ("fee_free", "no_fee"):
            return effective_fee_rate(fee_type)
        return fee_rate
    return effective_fee_rate(fee_type)


def american_from_win_risk(win: float, risk: float) -> float | None:
    """Convert profit/risk to American odds.

    +200 means $200 profit per $100 risked.
    -200 means risk $200 to profit $100.
    Returns None if either value is non-positive.
    """
    if risk <= 0 or win <= 0:
        return None
    if win >= risk:
        return (win / risk) * 100.0
    return -(risk / win) * 100.0


# ==================================================================
# Bps-aware API (canonical). See
# ``docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md``.
# ==================================================================


def flat_fee_bps(price_bps: int, *, rate: float) -> int:
    """Per-contract fee in bps for the flat fee model.

    ``fee_dollars = price_dollars * rate`` → ``fee_bps = round(price_bps * rate)``.
    """
    from decimal import Decimal

    return int((Decimal(price_bps) * Decimal(str(rate))).to_integral_value())


def compute_fee_bps(
    price_bps: int,
    *,
    fee_type: str = "quadratic_with_maker_fees",
    rate: float = MAKER_FEE_RATE,
) -> int:
    """Dispatch fee calculation by type. Returns per-contract fee in bps."""
    if fee_type in ("quadratic", "quadratic_with_maker_fees"):
        return quadratic_fee_bps(price_bps, rate=rate)
    if fee_type == "flat":
        return flat_fee_bps(price_bps, rate=rate)
    if fee_type in ("fee_free", "no_fee"):
        return 0
    return quadratic_fee_bps(price_bps, rate=rate)


def fee_adjusted_cost_bps(price_bps: int, *, rate: float = MAKER_FEE_RATE) -> int:
    """Effective cost per contract in bps including quadratic fill fee."""
    return price_bps + quadratic_fee_bps(price_bps, rate=rate)


def max_profitable_price_bps(other_avg_price_bps: int, *, rate: float = MAKER_FEE_RATE) -> int:
    """Highest integer-cent-aligned bps price at which a catch-up bid is profitable.

    Scans whole-cent prices (100 bps increments) from 99¢ down — Talos
    places whole-cent orders only, so the candidate space is the 99
    integer cents. Returns 0 if no profitable price exists.
    """
    import math

    other_bps_rounded = math.ceil(other_avg_price_bps / ONE_CENT_BPS) * ONE_CENT_BPS
    other_cost_bps = fee_adjusted_cost_bps(other_bps_rounded, rate=rate)
    budget_bps = ONE_DOLLAR_BPS - other_cost_bps
    if budget_bps <= ONE_CENT_BPS:
        return 0
    for cents in range(99, 0, -1):
        candidate_bps = cents * ONE_CENT_BPS
        if fee_adjusted_cost_bps(candidate_bps, rate=rate) < budget_bps:
            return candidate_bps
    return 0


def american_odds_bps(price_bps: int, *, rate: float = MAKER_FEE_RATE) -> float | None:
    """Fee-adjusted American odds for a NO contract, given price in bps.

    Returns ``None`` for degenerate prices (0 or ``ONE_DOLLAR_BPS``).
    """
    if price_bps <= 0 or price_bps >= ONE_DOLLAR_BPS:
        return None
    eff_bps = fee_adjusted_cost_bps(price_bps, rate=rate)
    win_bps = ONE_DOLLAR_BPS - eff_bps
    if win_bps <= 0:
        return None
    if eff_bps >= win_bps:
        return -(eff_bps / win_bps) * 100
    return (win_bps / eff_bps) * 100


def fee_adjusted_edge_bps(no_a_bps: int, no_b_bps: int, *, rate: float = MAKER_FEE_RATE) -> int:
    """Fee-adjusted edge in bps for a NO+NO pair.

    ``edge_bps = ONE_DOLLAR_BPS - fee_adjusted_cost_bps(a) - fee_adjusted_cost_bps(b)``.
    """
    return (
        ONE_DOLLAR_BPS
        - fee_adjusted_cost_bps(no_a_bps, rate=rate)
        - fee_adjusted_cost_bps(no_b_bps, rate=rate)
    )


def scenario_pnl_bps(
    filled_a_fp100: int,
    total_cost_bps_a: int,
    filled_b_fp100: int,
    total_cost_bps_b: int,
    fees_bps_a: int = 0,
    fees_bps_b: int = 0,
) -> tuple[int, int]:
    """Net P&L in bps for each outcome of a NO+NO position.

    Counts are fp100 (1 contract = 100 fp100); costs and fees are bps
    ($1 = 10_000 bps). Winner-side payout is
    ``count_fp100 * ONE_DOLLAR_BPS / ONE_CONTRACT_FP100`` — i.e. each
    contract (100 fp100) pays ``ONE_DOLLAR_BPS``.

    Returns ``(net_if_a_wins_bps, net_if_b_wins_bps)``.
    """
    total_outlay_bps = total_cost_bps_a + total_cost_bps_b + fees_bps_a + fees_bps_b
    net_a_bps = (filled_b_fp100 * ONE_DOLLAR_BPS) // ONE_CONTRACT_FP100 - total_outlay_bps
    net_b_bps = (filled_a_fp100 * ONE_DOLLAR_BPS) // ONE_CONTRACT_FP100 - total_outlay_bps
    return (net_a_bps, net_b_bps)


def fee_adjusted_profit_matched_bps(
    matched_fp100: int,
    cost_a_total_bps: int,
    cost_b_total_bps: int,
    fees_bps_a: int = 0,
    fees_bps_b: int = 0,
) -> int:
    """Guaranteed bps profit for matched pairs after fees.

    ``matched_fp100`` contracts (in fp100) pay out
    ``(matched_fp100 * ONE_DOLLAR_BPS / ONE_CONTRACT_FP100)`` at settlement,
    regardless of which side wins.
    """
    if matched_fp100 <= 0:
        return 0
    return (
        (matched_fp100 * ONE_DOLLAR_BPS) // ONE_CONTRACT_FP100
        - cost_a_total_bps
        - cost_b_total_bps
        - fees_bps_a
        - fees_bps_b
    )
