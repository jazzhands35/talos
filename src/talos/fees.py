"""Maker fee calculations for Kalshi NO+NO arbitrage.

Kalshi uses a **quadratic** fee model:
    fee_per_contract = no_price × (100 - no_price) × multiplier / 100
Fees are charged at fill time, not settlement.
"""

from __future__ import annotations

MAKER_FEE_RATE = 0.0175


def quadratic_fee(no_price: int) -> float:
    """Per-contract fee in cents using Kalshi's quadratic model."""
    return no_price * (100 - no_price) * MAKER_FEE_RATE / 100


def fee_adjusted_cost(no_price: int) -> float:
    """Effective cost per contract including quadratic fill fee.

    Fee is ``no_price × (100 - no_price) × 0.0175 / 100`` per contract,
    charged at fill time.
    """
    return no_price + quadratic_fee(no_price)


def american_odds(no_price: int) -> float | None:
    """Fee-adjusted American odds for a NO contract.

    Uses fee-adjusted effective cost to compute risk/reward odds.
    Returns None for degenerate prices (0 or 100).
    """
    if no_price <= 0 or no_price >= 100:
        return None
    eff = fee_adjusted_cost(no_price)
    win = 100 - eff
    if win <= 0:
        return None
    if eff >= win:  # favorite
        return -(eff / win) * 100
    return (win / eff) * 100  # underdog


def fee_adjusted_edge(no_a: int, no_b: int) -> float:
    """Fee-adjusted edge for a NO+NO pair.

    Prices in cents.  Returns edge in cents (can be fractional).
    Fees are quadratic and charged at fill time on both legs,
    so edge = 100 - cost_a - fee_a - cost_b - fee_b.
    """
    return 100 - fee_adjusted_cost(no_a) - fee_adjusted_cost(no_b)


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
