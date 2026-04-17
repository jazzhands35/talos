"""Maker fee calculations for Kalshi NO+NO arbitrage.

Kalshi uses a **quadratic** fee model on game markets:
    fee_per_contract_dollars = RATE × P × (1 − P)
where P is the price in dollars (cents / 100). In cents-per-contract:
    fee_cents = RATE × price_cents × (100 − price_cents) / 100

The rate is a Kalshi-wide constant (see Kairos's KALSHI_FEES.md):
    0.0175   — full rate (no rebate)
    0.00875  — maker rebate rate (halve when enrolled)

Fees are charged at fill time, not settlement. The ``Series.fee_multiplier``
field from the Kalshi API is NOT a reliable source for the maker rate —
Kalshi has been observed returning sentinel values like 1.0 on both
``quadratic`` and ``quadratic_with_maker_fees`` series. Always use the
constants below, gated by ``fee_type`` only to zero out fee-free markets.
"""

from __future__ import annotations

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


def quadratic_fee(no_price: int, *, rate: float = MAKER_FEE_RATE) -> float:
    """Per-contract fee in cents using Kalshi's quadratic model."""
    return no_price * (100 - no_price) * rate / 100


def flat_fee(no_price: int, *, rate: float) -> float:
    """Per-contract fee in cents using a flat percentage model."""
    return no_price * rate


def compute_fee(
    no_price: int,
    *,
    fee_type: str = "quadratic_with_maker_fees",
    rate: float = MAKER_FEE_RATE,
) -> float:
    """Dispatch fee calculation by type. Returns per-contract fee in cents."""
    if fee_type in ("quadratic", "quadratic_with_maker_fees"):
        return quadratic_fee(no_price, rate=rate)
    if fee_type == "flat":
        return flat_fee(no_price, rate=rate)
    if fee_type in ("fee_free", "no_fee"):
        return 0.0
    return quadratic_fee(no_price, rate=rate)


def fee_adjusted_cost(no_price: int, *, rate: float = MAKER_FEE_RATE) -> float:
    """Effective cost per contract including quadratic fill fee.

    Fee is ``no_price × (100 - no_price) × rate / 100`` per contract,
    charged at fill time.
    """
    return no_price + quadratic_fee(no_price, rate=rate)


def max_profitable_price(other_avg_price: float, *, rate: float = MAKER_FEE_RATE) -> int:
    """Highest integer price at which a catch-up bid is profitable.

    Given the other side's average fill price, find the max price P where
    fee_adjusted_cost(P) + fee_adjusted_cost(other) < 100.
    Returns 0 if no profitable price exists.
    """
    import math

    other_cost = fee_adjusted_cost(math.ceil(other_avg_price), rate=rate)
    budget = 100 - other_cost
    if budget <= 1:
        return 0
    # Scan downward from 99 — O(99) trivially fast
    for p in range(99, 0, -1):
        if fee_adjusted_cost(p, rate=rate) < budget:
            return p
    return 0


def american_odds(no_price: int, *, rate: float = MAKER_FEE_RATE) -> float | None:
    """Fee-adjusted American odds for a NO contract.

    Uses fee-adjusted effective cost to compute risk/reward odds.
    Returns None for degenerate prices (0 or 100).
    """
    if no_price <= 0 or no_price >= 100:
        return None
    eff = fee_adjusted_cost(no_price, rate=rate)
    win = 100 - eff
    if win <= 0:
        return None
    if eff >= win:  # favorite
        return -(eff / win) * 100
    return (win / eff) * 100  # underdog


def fee_adjusted_edge(no_a: int, no_b: int, *, rate: float = MAKER_FEE_RATE) -> float:
    """Fee-adjusted edge for a NO+NO pair.

    Prices in cents.  Returns edge in cents (can be fractional).
    Fees are quadratic and charged at fill time on both legs,
    so edge = 100 - cost_a - fee_a - cost_b - fee_b.
    """
    return 100 - fee_adjusted_cost(no_a, rate=rate) - fee_adjusted_cost(no_b, rate=rate)


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


def scenario_pnl(
    filled_a: int,
    total_cost_a: int,
    filled_b: int,
    total_cost_b: int,
    fees_a: int = 0,
    fees_b: int = 0,
) -> tuple[float, float]:
    """Net P&L in cents for each outcome of a NO+NO position.

    ``total_cost_a`` / ``total_cost_b`` are the total fill costs in cents
    (sum of price * count across all fills), NOT per-contract averages.
    ``fees_a`` / ``fees_b`` are actual maker fees already paid (from API).

    Returns ``(net_if_a_wins, net_if_b_wins)``:
    - If team A wins: NO-B pays 100¢ each, NO-A worthless.
    - If team B wins: NO-A pays 100¢ each, NO-B worthless.
    Fees are already deducted from balance at fill time.
    """
    total_outlay = total_cost_a + total_cost_b + fees_a + fees_b
    net_a = filled_b * 100 - total_outlay
    net_b = filled_a * 100 - total_outlay
    return (net_a, net_b)


def fee_adjusted_profit_matched(
    matched: int,
    cost_a_total: int,
    cost_b_total: int,
    fees_a: int = 0,
    fees_b: int = 0,
) -> float:
    """Guaranteed profit for matched pairs after fees.

    ``cost_a_total`` / ``cost_b_total`` are the total fill costs allocated
    to the matched contracts (in cents).  ``fees_a`` / ``fees_b`` are actual
    maker fees (from API).  Returns total profit in cents.

    With fees paid at fill time, settlement pays 100¢ per winning contract
    with no additional fee.  For matched pairs both outcomes yield the same net.
    """
    if matched <= 0:
        return 0.0
    return matched * 100 - cost_a_total - cost_b_total - fees_a - fees_b
