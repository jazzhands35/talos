"""Maker fee calculations for Kalshi NO+NO arbitrage."""

from __future__ import annotations

MAKER_FEE_RATE = 0.0175
_KEEP = 1 - MAKER_FEE_RATE  # 0.9825


def fee_adjusted_cost(no_price: int) -> float:
    """Effective cost per contract including the fee paid on payout.

    When a NO contract at price ``p`` wins, the fee is ``(100 - p) * 0.0175``.
    So the effective cost is ``p + (100 - p) * fee_rate``.
    """
    return no_price + (100 - no_price) * MAKER_FEE_RATE


def american_odds(no_price: int) -> float | None:
    """Fee-adjusted American odds for a NO contract.

    Uses the same formula as the user's Google Sheets:
    - Favorite (p >= 50%): -(p/(1-p))*100 / (1 - fee_rate)
    - Underdog (p < 50%):  ((1-p)/p)*100 * (1 - fee_rate)
    Returns None for degenerate prices (0 or 100).
    """
    if no_price <= 0 or no_price >= 100:
        return None
    p = no_price / 100
    if p >= 0.5:
        return -(p / (1 - p)) * 100 / _KEEP
    return ((1 - p) / p) * 100 * _KEEP


def fee_adjusted_edge(no_a: int, no_b: int) -> float:
    """Worst-case fee-adjusted edge for a NO+NO pair.

    Prices in cents.  Returns edge in cents (can be fractional).
    The fee is 1.75% of the winning side's profit (100 - cost).
    Since we don't know which team wins, we take the min of both scenarios.
    """
    # If team A wins → NO-B pays out, fee on B's profit
    scenario_a = (100 - no_b) * _KEEP - no_a
    # If team B wins → NO-A pays out, fee on A's profit
    scenario_b = (100 - no_a) * _KEEP - no_b
    return min(scenario_a, scenario_b)


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
) -> tuple[float, float]:
    """Net P&L in cents for each outcome of a NO+NO position.

    ``total_cost_a`` / ``total_cost_b`` are the total fill costs in cents
    (sum of price * count across all fills), NOT per-contract averages.

    Returns ``(net_if_a_wins, net_if_b_wins)``:
    - If team A wins: NO-B pays out (profit after fees) minus NO-A cost (lost).
    - If team B wins: NO-A pays out (profit after fees) minus NO-B cost (lost).
    """
    net_a = (filled_b * 100 - total_cost_b) * _KEEP - total_cost_a
    net_b = (filled_a * 100 - total_cost_a) * _KEEP - total_cost_b
    return (net_a, net_b)


def fee_adjusted_profit_matched(
    matched: int, cost_a_total: int, cost_b_total: int
) -> float:
    """Fee-adjusted guaranteed profit for matched pairs.

    ``cost_a_total`` / ``cost_b_total`` are the total fill costs allocated
    to the matched contracts (in cents).  Returns total profit in cents.
    """
    if matched <= 0:
        return 0.0
    revenue = matched * 100
    raw = revenue - cost_a_total - cost_b_total
    # Fee on winning side's profit — worst case is the side with more profit
    fee_if_a_wins = (revenue - cost_b_total) * MAKER_FEE_RATE
    fee_if_b_wins = (revenue - cost_a_total) * MAKER_FEE_RATE
    return raw - max(fee_if_a_wins, fee_if_b_wins)
