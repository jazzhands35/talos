# Clio Backtester Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a configurable backtester that replays 182M historical Kalshi trades to simulate different order placement strategies, with an interactive dashboard view for exploring results.

**Architecture:** Three Python layers (price paths → strategies → runner) backed by SQLite trade data, served to a React dashboard view via Flask API endpoints. Strategies are composable dataclasses with slider-controllable parameters.

**Tech Stack:** Python 3.12+, pandas, SQLite, Flask (existing API), React/TypeScript/Recharts (existing dashboard)

**Spec:** `docs/superpowers/specs/2026-03-23-clio-backtester-design.md`

**Worktree:** `.worktrees/clio` on branch `feature/clio-ml-optimizer`

**Python venv:** `C:/Users/Sean/Documents/Python/Talos/.venv/Scripts/python`

**Run tests:** `cd .worktrees/clio && PYTHONPATH=. C:/Users/Sean/Documents/Python/Talos/.venv/Scripts/python -m pytest tests/test_clio/ -v`

**Build frontend:** `cd .worktrees/clio/clio/dashboard && npm run build`

---

## Task 1: Strategy Dataclasses

**Files:**
- Create: `clio/strategies.py`
- Create: `tests/test_clio/test_strategies.py`

- [ ] **Step 1: Write tests for strategy dataclasses**

```python
"""Tests for strategy dataclasses and config composition."""

from clio.strategies import (
    SimultaneousStrategy,
    SequentialThinStrategy,
    SequentialSpreadStrategy,
    TimeGatedFilter,
    PriceThresholdFilter,
    DynamicSizing,
    BacktestConfig,
)


def test_simultaneous_defaults() -> None:
    s = SimultaneousStrategy()
    assert s.name == "simultaneous"


def test_sequential_thin_defaults() -> None:
    s = SequentialThinStrategy()
    assert s.wait_timeout_seconds == 300
    assert s.price_tolerance_cents == 2


def test_sequential_spread_defaults() -> None:
    s = SequentialSpreadStrategy()
    assert s.wait_timeout_seconds == 300


def test_time_gated_defaults() -> None:
    f = TimeGatedFilter()
    assert f.max_minutes_before_start == 120
    assert f.min_minutes_before_start == 15


def test_price_threshold_defaults() -> None:
    f = PriceThresholdFilter()
    assert f.min_combined_price == 70
    assert f.max_combined_price == 95


def test_dynamic_sizing_defaults() -> None:
    s = DynamicSizing()
    assert s.base_unit == 20
    assert s.high_risk_unit == 10


def test_backtest_config_from_dict() -> None:
    """Config can be built from API request JSON."""
    raw = {
        "placement": "sequential_thin",
        "placement_params": {"wait_timeout_seconds": 600},
        "entry_filters": {
            "price_threshold": {"min_combined_price": 75},
        },
        "sizing": {"base_unit": 15},
        "filters": {"sport": "Hockey"},
    }
    cfg = BacktestConfig.from_dict(raw)
    assert isinstance(cfg.placement, SequentialThinStrategy)
    assert cfg.placement.wait_timeout_seconds == 600
    assert cfg.entry_filters[0].min_combined_price == 75
    assert cfg.sizing.base_unit == 15
    assert cfg.data_filters["sport"] == "Hockey"


def test_backtest_config_defaults() -> None:
    """Empty dict produces baseline config."""
    cfg = BacktestConfig.from_dict({})
    assert isinstance(cfg.placement, SimultaneousStrategy)
    assert cfg.entry_filters == []
    assert cfg.sizing.base_unit == 20


def test_price_threshold_filter_applies() -> None:
    f = PriceThresholdFilter(min_combined_price=70, max_combined_price=95)
    assert f.should_enter(combined_price=80) is True
    assert f.should_enter(combined_price=60) is False
    assert f.should_enter(combined_price=96) is False


def test_time_gated_filter_applies() -> None:
    f = TimeGatedFilter(max_minutes_before_start=120, min_minutes_before_start=15)
    assert f.should_enter(minutes_to_start=60) is True
    assert f.should_enter(minutes_to_start=5) is False
    assert f.should_enter(minutes_to_start=180) is False


def test_dynamic_sizing_picks_unit() -> None:
    s = DynamicSizing(base_unit=20, high_risk_unit=10,
                      high_risk_vol_ratio=2.0, high_risk_max_combined=75)
    assert s.get_unit(vol_ratio=1.5, combined_price=85) == 20  # low risk
    assert s.get_unit(vol_ratio=3.0, combined_price=85) == 10  # high vol ratio
    assert s.get_unit(vol_ratio=1.2, combined_price=60) == 10  # low combined
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd .worktrees/clio && PYTHONPATH=. .venv/Scripts/python -m pytest tests/test_clio/test_strategies.py -v
```

Expected: FAIL — `clio.strategies` not found.

- [ ] **Step 3: Write `clio/strategies.py`**

```python
"""Strategy dataclasses for backtester configuration.

Each strategy is a composable unit with slider-controllable parameters.
Strategies are composed into a BacktestConfig that the runner executes.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ── Placement Strategies ──

@dataclass
class SimultaneousStrategy:
    """Baseline: place both sides at the same time."""
    name: str = "simultaneous"


@dataclass
class SequentialThinStrategy:
    """Place the side with less trade activity first."""
    name: str = "sequential_thin"
    wait_timeout_seconds: int = 300
    price_tolerance_cents: int = 2


@dataclass
class SequentialSpreadStrategy:
    """Place the side with wider bid/ask spread first."""
    name: str = "sequential_spread"
    wait_timeout_seconds: int = 300
    price_tolerance_cents: int = 2


PlacementStrategy = SimultaneousStrategy | SequentialThinStrategy | SequentialSpreadStrategy


# ── Entry Filters ──

@dataclass
class PriceThresholdFilter:
    """Only enter when combined price is in range."""
    min_combined_price: int = 70
    max_combined_price: int = 95

    def should_enter(self, combined_price: float) -> bool:
        return self.min_combined_price <= combined_price <= self.max_combined_price


@dataclass
class TimeGatedFilter:
    """Only enter within a time window before game start."""
    max_minutes_before_start: int = 120
    min_minutes_before_start: int = 15

    def should_enter(self, minutes_to_start: float) -> bool:
        return self.min_minutes_before_start <= minutes_to_start <= self.max_minutes_before_start


EntryFilter = PriceThresholdFilter | TimeGatedFilter


# ── Sizing ──

@dataclass
class DynamicSizing:
    """Vary unit size based on event risk characteristics."""
    base_unit: int = 20
    high_risk_unit: int = 10
    high_risk_vol_ratio: float = 2.0
    high_risk_max_combined: int = 75

    def get_unit(self, vol_ratio: float, combined_price: float) -> int:
        if vol_ratio > self.high_risk_vol_ratio:
            return self.high_risk_unit
        if combined_price < self.high_risk_max_combined:
            return self.high_risk_unit
        return self.base_unit


# ── Composed Config ──

PLACEMENT_MAP: dict[str, type] = {
    "simultaneous": SimultaneousStrategy,
    "sequential_thin": SequentialThinStrategy,
    "sequential_spread": SequentialSpreadStrategy,
}


@dataclass
class BacktestConfig:
    """Full backtest configuration composed from strategy layers."""
    placement: PlacementStrategy = field(default_factory=SimultaneousStrategy)
    entry_filters: list[EntryFilter] = field(default_factory=list)
    sizing: DynamicSizing = field(default_factory=DynamicSizing)
    data_filters: dict[str, str | None] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict) -> BacktestConfig:
        """Build config from API request JSON."""
        # Placement
        placement_name = raw.get("placement", "simultaneous")
        placement_cls = PLACEMENT_MAP.get(placement_name, SimultaneousStrategy)
        placement_params = raw.get("placement_params", {})
        placement = placement_cls(**{
            k: v for k, v in placement_params.items()
            if k in placement_cls.__dataclass_fields__
        })

        # Entry filters
        filters_raw = raw.get("entry_filters", {})
        entry_filters: list[EntryFilter] = []
        if "price_threshold" in filters_raw:
            entry_filters.append(PriceThresholdFilter(**filters_raw["price_threshold"]))
        if "time_gated" in filters_raw:
            entry_filters.append(TimeGatedFilter(**filters_raw["time_gated"]))

        # Sizing
        sizing_raw = raw.get("sizing", {})
        sizing = DynamicSizing(**{
            k: v for k, v in sizing_raw.items()
            if k in DynamicSizing.__dataclass_fields__
        })

        # Data filters
        data_filters = raw.get("filters", {})

        return cls(
            placement=placement,
            entry_filters=entry_filters,
            sizing=sizing,
            data_filters=data_filters,
        )
```

- [ ] **Step 4: Run tests**

```bash
cd .worktrees/clio && PYTHONPATH=. .venv/Scripts/python -m pytest tests/test_clio/test_strategies.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add clio/strategies.py tests/test_clio/test_strategies.py
git commit -m "feat(clio): strategy dataclasses for backtester configuration"
```

---

## Task 2: Fill Simulator

**Files:**
- Create: `clio/backtest.py`
- Create: `tests/test_clio/test_backtest.py`

The fill simulator is the core engine — given a list of trades and an order (price, unit size, timeout), it determines whether and how much fills.

- [ ] **Step 1: Write tests for fill simulation**

```python
"""Tests for fill simulation logic."""

import pandas as pd
from clio.backtest import simulate_fill, FillResult


def test_fill_basic_full_fill() -> None:
    """Enough taker_side=yes trades at our price → full fill."""
    trades = pd.DataFrame({
        "timestamp": pd.to_datetime([
            "2026-03-15T18:00:00Z",
            "2026-03-15T18:00:30Z",
            "2026-03-15T18:01:00Z",
        ]),
        "no_price_cents": [45, 44, 45],
        "count": [10, 8, 5],
        "taker_side": ["yes", "yes", "yes"],
    })
    result = simulate_fill(
        trades=trades,
        bid_price=45,
        unit_size=20,
        placement_time=pd.Timestamp("2026-03-15T18:00:00Z"),
        timeout_seconds=300,
    )
    assert result.filled is True
    assert result.fill_qty == 20
    assert result.time_to_fill > 0


def test_fill_partial_fill() -> None:
    """Not enough volume → partial fill."""
    trades = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-03-15T18:00:10Z"]),
        "no_price_cents": [45],
        "count": [8],
        "taker_side": ["yes"],
    })
    result = simulate_fill(
        trades=trades,
        bid_price=45,
        unit_size=20,
        placement_time=pd.Timestamp("2026-03-15T18:00:00Z"),
        timeout_seconds=300,
    )
    assert result.filled is False
    assert result.fill_qty == 8


def test_fill_ignores_wrong_taker_side() -> None:
    """Trades where taker_side=no are ignored (not a NO fill)."""
    trades = pd.DataFrame({
        "timestamp": pd.to_datetime([
            "2026-03-15T18:00:10Z",
            "2026-03-15T18:00:20Z",
        ]),
        "no_price_cents": [45, 45],
        "count": [20, 20],
        "taker_side": ["no", "yes"],  # first trade is wrong side
    })
    result = simulate_fill(
        trades=trades,
        bid_price=45,
        unit_size=20,
        placement_time=pd.Timestamp("2026-03-15T18:00:00Z"),
        timeout_seconds=300,
    )
    assert result.filled is True
    assert result.fill_qty == 20


def test_fill_ignores_trades_above_bid() -> None:
    """Trades at higher NO price than our bid are ignored."""
    trades = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-03-15T18:00:10Z"]),
        "no_price_cents": [50],  # above our bid of 45
        "count": [100],
        "taker_side": ["yes"],
    })
    result = simulate_fill(
        trades=trades,
        bid_price=45,
        unit_size=20,
        placement_time=pd.Timestamp("2026-03-15T18:00:00Z"),
        timeout_seconds=300,
    )
    assert result.filled is False
    assert result.fill_qty == 0


def test_fill_timeout() -> None:
    """Trades after timeout are ignored."""
    trades = pd.DataFrame({
        "timestamp": pd.to_datetime([
            "2026-03-15T18:10:00Z",  # 10 min after placement, beyond 5 min timeout
        ]),
        "no_price_cents": [45],
        "count": [20],
        "taker_side": ["yes"],
    })
    result = simulate_fill(
        trades=trades,
        bid_price=45,
        unit_size=20,
        placement_time=pd.Timestamp("2026-03-15T18:00:00Z"),
        timeout_seconds=300,  # 5 min
    )
    assert result.filled is False
    assert result.fill_qty == 0


def test_fill_empty_trades() -> None:
    """No trades at all → no fill."""
    trades = pd.DataFrame(columns=["timestamp", "no_price_cents", "count", "taker_side"])
    result = simulate_fill(
        trades=trades,
        bid_price=45,
        unit_size=20,
        placement_time=pd.Timestamp("2026-03-15T18:00:00Z"),
        timeout_seconds=300,
    )
    assert result.filled is False
    assert result.fill_qty == 0
    assert result.time_to_fill == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd .worktrees/clio && PYTHONPATH=. .venv/Scripts/python -m pytest tests/test_clio/test_backtest.py -v
```

- [ ] **Step 3: Write fill simulator in `clio/backtest.py`**

```python
"""Backtester — replays historical trades to simulate placement strategies.

Three layers:
1. Price path builder — loads trades per event from kalshi_history.db
2. Fill simulator — models whether a resting NO bid would fill
3. Runner — iterates events, applies strategy, collects results
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from clio.fees import fee_adjusted_cost


@dataclass
class FillResult:
    """Result of simulating a single order fill."""
    filled: bool
    fill_qty: int
    avg_fill_price: float
    time_to_fill: float  # seconds from placement to last fill trade


def simulate_fill(
    trades: pd.DataFrame,
    bid_price: int | float,
    unit_size: int,
    placement_time: pd.Timestamp,
    timeout_seconds: int,
) -> FillResult:
    """Simulate whether a resting NO bid would fill from trade history.

    Uses taker-side filtering: only trades where taker_side="yes"
    (someone sold into the bid) at no_price_cents <= our bid.
    """
    if trades.empty:
        return FillResult(filled=False, fill_qty=0, avg_fill_price=0.0, time_to_fill=0.0)

    deadline = placement_time + pd.Timedelta(seconds=timeout_seconds)

    # Filter: after placement, before timeout, correct side, at or below bid
    mask = (
        (trades["timestamp"] >= placement_time)
        & (trades["timestamp"] <= deadline)
        & (trades["taker_side"] == "yes")
        & (trades["no_price_cents"] <= bid_price)
    )
    eligible = trades.loc[mask].sort_values("timestamp")

    if eligible.empty:
        return FillResult(filled=False, fill_qty=0, avg_fill_price=0.0, time_to_fill=0.0)

    # Accumulate fills up to unit_size
    qty_remaining = unit_size
    total_cost = 0.0
    last_time = placement_time

    for _, trade in eligible.iterrows():
        take = min(int(trade["count"]), qty_remaining)
        total_cost += take * float(trade["no_price_cents"])
        qty_remaining -= take
        last_time = trade["timestamp"]
        if qty_remaining <= 0:
            break

    fill_qty = unit_size - qty_remaining
    avg_price = total_cost / fill_qty if fill_qty > 0 else 0.0
    ttf = (last_time - placement_time).total_seconds() if fill_qty > 0 else 0.0

    return FillResult(
        filled=(qty_remaining <= 0),
        fill_qty=fill_qty,
        avg_fill_price=round(avg_price, 2),
        time_to_fill=round(ttf, 1),
    )
```

- [ ] **Step 4: Run tests**

```bash
cd .worktrees/clio && PYTHONPATH=. .venv/Scripts/python -m pytest tests/test_clio/test_backtest.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add clio/backtest.py tests/test_clio/test_backtest.py
git commit -m "feat(clio): fill simulator with taker-side filtering"
```

---

## Task 3: PnL Calculator and Event Simulator

**Files:**
- Modify: `clio/backtest.py`
- Modify: `tests/test_clio/test_backtest.py`

- [ ] **Step 1: Add PnL and event simulation tests**

```python
"""Additional tests for PnL calculation and event-level simulation."""

from clio.backtest import calculate_pnl, simulate_event, EventResult
from clio.strategies import SimultaneousStrategy, SequentialThinStrategy, BacktestConfig
import pandas as pd


def test_pnl_both_filled() -> None:
    """Both sides fill → profit from edge minus fees."""
    pnl = calculate_pnl(
        filled_a=True, filled_b=True,
        avg_price_a=45, avg_price_b=44,
        qty_a=20, qty_b=20,
    )
    # 100 - 45 - 44 = 11 cents edge per contract
    # minus fees on both sides
    # net should be positive
    assert pnl > 0


def test_pnl_one_side_trapped() -> None:
    """Only side A fills → naked loss."""
    pnl = calculate_pnl(
        filled_a=True, filled_b=False,
        avg_price_a=45, avg_price_b=0,
        qty_a=20, qty_b=0,
    )
    # Trapped: lose the cost of side A
    assert pnl < 0


def test_pnl_neither_fills() -> None:
    """Neither side fills → $0."""
    pnl = calculate_pnl(
        filled_a=False, filled_b=False,
        avg_price_a=0, avg_price_b=0,
        qty_a=0, qty_b=0,
    )
    assert pnl == 0
```

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Implement `calculate_pnl` and `simulate_event`**

Add to `clio/backtest.py`:

```python
@dataclass
class EventResult:
    """Result of simulating one event."""
    event_ticker: str
    sport: str
    league: str
    entered: bool
    filled_a: bool
    filled_b: bool
    price_a: float
    price_b: float
    combined: float
    fill_time_a: float
    fill_time_b: float
    unit_size: int
    pnl: float  # dollars, not cents
    result: str  # "clean", "trapped", "no_entry", "no_fill"


def calculate_pnl(
    filled_a: bool,
    filled_b: bool,
    avg_price_a: float,
    avg_price_b: float,
    qty_a: int,
    qty_b: int,
) -> float:
    """Calculate net PnL in dollars for a simulated trade.

    Both fill: profit = (100 - price_a - fee_a - price_b - fee_b) * min(qty) / 100
    One fills: loss = -(qty * price / 100)  (naked position settles to 0 on average)
    Neither: $0
    """
    if not filled_a and not filled_b:
        return 0.0

    if filled_a and filled_b:
        qty = min(qty_a, qty_b)
        cost_a = fee_adjusted_cost(avg_price_a)
        cost_b = fee_adjusted_cost(avg_price_b)
        edge_cents = 100 - cost_a - cost_b
        return round(edge_cents * qty / 100, 2)

    # One side trapped — lose the cost of the filled side
    # Average case: the filled NO position has ~50% chance of winning
    # But in practice, traps happen when the game result goes against us
    # Conservative: assume full loss of cost basis
    if filled_a:
        return round(-(qty_a * avg_price_a / 100), 2)
    else:
        return round(-(qty_b * avg_price_b / 100), 2)
```

`simulate_event` takes a `BacktestConfig`, trade DataFrames for both markets, event metadata (est_start, volume_a/b), and runs the full simulation:

```python
def simulate_event(
    config: BacktestConfig,
    trades_a: pd.DataFrame,
    trades_b: pd.DataFrame,
    event_ticker: str,
    sport: str,
    league: str,
    est_start: pd.Timestamp | None,
    no_price_a: int,
    no_price_b: int,
    volume_a: int,
    volume_b: int,
) -> EventResult:
    """Simulate a single event under the given strategy config."""
    combined = no_price_a + no_price_b

    # Check entry filters
    for f in config.entry_filters:
        if hasattr(f, "should_enter"):
            if hasattr(f, "min_combined_price"):
                if not f.should_enter(combined_price=combined):
                    return EventResult(
                        event_ticker=event_ticker, sport=sport, league=league,
                        entered=False, filled_a=False, filled_b=False,
                        price_a=no_price_a, price_b=no_price_b, combined=combined,
                        fill_time_a=0, fill_time_b=0, unit_size=0, pnl=0.0,
                        result="no_entry",
                    )
            if hasattr(f, "min_minutes_before_start") and est_start is not None:
                # Find the entry time from trades
                all_trades = pd.concat([trades_a, trades_b])
                if all_trades.empty:
                    return EventResult(
                        event_ticker=event_ticker, sport=sport, league=league,
                        entered=False, filled_a=False, filled_b=False,
                        price_a=no_price_a, price_b=no_price_b, combined=combined,
                        fill_time_a=0, fill_time_b=0, unit_size=0, pnl=0.0,
                        result="no_entry",
                    )
                earliest = all_trades["timestamp"].min()
                mins_to_start = (est_start - earliest).total_seconds() / 60
                if not f.should_enter(minutes_to_start=mins_to_start):
                    return EventResult(
                        event_ticker=event_ticker, sport=sport, league=league,
                        entered=False, filled_a=False, filled_b=False,
                        price_a=no_price_a, price_b=no_price_b, combined=combined,
                        fill_time_a=0, fill_time_b=0, unit_size=0, pnl=0.0,
                        result="no_entry",
                    )

    # Determine unit size
    vol_ratio = max(volume_a, volume_b) / max(min(volume_a, volume_b), 1)
    unit_size = config.sizing.get_unit(vol_ratio=vol_ratio, combined_price=combined)

    # Determine entry time — use median trade time in pre-game window as proxy
    all_ts = pd.concat([trades_a["timestamp"], trades_b["timestamp"]])
    if all_ts.empty:
        return EventResult(
            event_ticker=event_ticker, sport=sport, league=league,
            entered=True, filled_a=False, filled_b=False,
            price_a=no_price_a, price_b=no_price_b, combined=combined,
            fill_time_a=0, fill_time_b=0, unit_size=unit_size, pnl=0.0,
            result="no_fill",
        )
    entry_time = all_ts.sort_values().iloc[len(all_ts) // 4]  # 25th percentile — early entry

    placement = config.placement

    if isinstance(placement, SimultaneousStrategy):
        # Place both at same time
        fill_a = simulate_fill(trades_a, no_price_a, unit_size, entry_time, timeout_seconds=86400)
        fill_b = simulate_fill(trades_b, no_price_b, unit_size, entry_time, timeout_seconds=86400)
    elif isinstance(placement, (SequentialThinStrategy, SequentialSpreadStrategy)):
        # Determine which side goes first
        if isinstance(placement, SequentialThinStrategy):
            a_trades_count = len(trades_a)
            b_trades_count = len(trades_b)
            first_is_a = a_trades_count <= b_trades_count  # thin side first
        else:
            # Spread-informed: wider spread goes first (use price std as proxy)
            a_std = trades_a["no_price_cents"].std() if len(trades_a) > 1 else 999
            b_std = trades_b["no_price_cents"].std() if len(trades_b) > 1 else 999
            first_is_a = a_std >= b_std

        if first_is_a:
            first_trades, first_price = trades_a, no_price_a
            second_trades, second_price = trades_b, no_price_b
        else:
            first_trades, first_price = trades_b, no_price_b
            second_trades, second_price = trades_a, no_price_a

        # Place first leg
        first_fill = simulate_fill(
            first_trades, first_price, unit_size, entry_time,
            placement.wait_timeout_seconds,
        )

        if not first_fill.filled:
            # First leg didn't fill → abort, no trap
            fill_a = first_fill if first_is_a else FillResult(False, 0, 0.0, 0.0)
            fill_b = FillResult(False, 0, 0.0, 0.0) if first_is_a else first_fill
        else:
            # First leg filled → check price tolerance on second leg
            second_entry_time = entry_time + pd.Timedelta(seconds=first_fill.time_to_fill)

            # Check if second leg price has moved too far
            second_trades_at_entry = second_trades[
                second_trades["timestamp"] >= second_entry_time
            ]
            if not second_trades_at_entry.empty:
                current_price = second_trades_at_entry.iloc[0]["no_price_cents"]
                if abs(current_price - second_price) > placement.price_tolerance_cents:
                    # Price moved too far — abort second leg
                    fill_a = first_fill if first_is_a else FillResult(False, 0, 0.0, 0.0)
                    fill_b = FillResult(False, 0, 0.0, 0.0) if first_is_a else first_fill
                else:
                    second_fill = simulate_fill(
                        second_trades, second_price, unit_size, second_entry_time,
                        86400,  # no timeout on second leg
                    )
                    fill_a = first_fill if first_is_a else second_fill
                    fill_b = second_fill if first_is_a else first_fill
            else:
                fill_a = first_fill if first_is_a else FillResult(False, 0, 0.0, 0.0)
                fill_b = FillResult(False, 0, 0.0, 0.0) if first_is_a else first_fill
    else:
        # Unknown strategy — treat as simultaneous
        fill_a = simulate_fill(trades_a, no_price_a, unit_size, entry_time, timeout_seconds=86400)
        fill_b = simulate_fill(trades_b, no_price_b, unit_size, entry_time, timeout_seconds=86400)

    pnl = calculate_pnl(
        fill_a.filled, fill_b.filled,
        fill_a.avg_fill_price, fill_b.avg_fill_price,
        fill_a.fill_qty, fill_b.fill_qty,
    )

    if fill_a.filled and fill_b.filled:
        result_str = "clean"
    elif fill_a.filled or fill_b.filled:
        result_str = "trapped"
    else:
        result_str = "no_fill"

    return EventResult(
        event_ticker=event_ticker, sport=sport, league=league,
        entered=True,
        filled_a=fill_a.filled, filled_b=fill_b.filled,
        price_a=fill_a.avg_fill_price, price_b=fill_b.avg_fill_price,
        combined=fill_a.avg_fill_price + fill_b.avg_fill_price,
        fill_time_a=fill_a.time_to_fill, fill_time_b=fill_b.time_to_fill,
        unit_size=unit_size, pnl=pnl, result=result_str,
    )
```

- [ ] **Step 4: Run tests**

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add clio/backtest.py tests/test_clio/test_backtest.py
git commit -m "feat(clio): PnL calculator and event-level simulation"
```

---

## Task 4: Price Path Builder and Runner

**Files:**
- Modify: `clio/backtest.py`

Adds `build_price_paths()` to load trades from SQLite and `run_backtest()` to iterate events.

- [ ] **Step 1: Implement price path builder**

Add to `clio/backtest.py`:

```python
import sqlite3
from pathlib import Path
from clio.config import ClioConfig, SERIES_SPORT_MAP
from clio.prices import get_estimated_start, pair_markets_for_event


def build_price_paths(
    conn: sqlite3.Connection,
    event_ticker: str,
    ticker_a: str,
    ticker_b: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load trade history for both markets of an event.

    Returns two DataFrames with columns: timestamp, no_price_cents, count, taker_side
    """
    dfs = []
    for ticker in [ticker_a, ticker_b]:
        query = """
            SELECT created_time as timestamp, no_price_cents, count, taker_side
            FROM trades WHERE ticker = ?
            ORDER BY created_time
        """
        df = pd.read_sql_query(query, conn, params=[ticker])
        if not df.empty:
            df["timestamp"] = pd.to_datetime(df["timestamp"])
        dfs.append(df)
    return dfs[0], dfs[1]


def run_backtest(config: BacktestConfig, cfg: ClioConfig, progress_cb=None) -> dict:
    """Run a full backtest across all matching events.

    Returns dict with 'summary' and 'events' keys matching the API response format.
    """
    conn = sqlite3.connect(str(cfg.history_db))
    conn.row_factory = sqlite3.Row

    # Find all 2-market settled events
    rows = conn.execute("""
        SELECT e.event_ticker, e.series_ticker,
               m.ticker, m.volume, m.open_interest, m.result, m.raw_json
        FROM events e
        JOIN markets m ON m.event_ticker = e.event_ticker
        WHERE e.market_count = 2
        AND m.result IS NOT NULL AND m.result != ''
        ORDER BY e.event_ticker
    """).fetchall()

    # Group by event
    events: dict[str, list[dict]] = {}
    for row in rows:
        et = row["event_ticker"]
        if et not in events:
            events[et] = []
        events[et].append(dict(row))

    valid = {k: v for k, v in events.items() if len(v) == 2}

    # Apply data filters
    df_filters = config.data_filters
    if df_filters.get("sport"):
        valid = {
            k: v for k, v in valid.items()
            if SERIES_SPORT_MAP.get(v[0]["series_ticker"], ("", ""))[0] == df_filters["sport"]
        }
    if df_filters.get("league"):
        valid = {
            k: v for k, v in valid.items()
            if SERIES_SPORT_MAP.get(v[0]["series_ticker"], ("", ""))[1] == df_filters["league"]
        }

    results: list[EventResult] = []
    total = len(valid)

    for i, (event_ticker, markets) in enumerate(valid.items()):
        if progress_cb:
            progress_cb(i, total)

        m0, m1 = markets[0], markets[1]
        tickers = [m0["ticker"], m1["ticker"]]
        ticker_a, ticker_b = pair_markets_for_event(tickers)
        ma = m0 if m0["ticker"] == ticker_a else m1
        mb = m1 if m0["ticker"] == ticker_a else m0

        series = ma["series_ticker"]
        sport, league = SERIES_SPORT_MAP.get(series, ("Unknown", "Unknown"))
        est_start_dt = get_estimated_start(ma["raw_json"], series)
        est_start = pd.Timestamp(est_start_dt) if est_start_dt else None

        # Get representative NO prices from market data
        no_a = ma.get("no_bid_cents") or 50
        no_b = mb.get("no_bid_cents") or 50

        trades_a, trades_b = build_price_paths(conn, event_ticker, ticker_a, ticker_b)

        # Use median trade price as better price estimate
        if not trades_a.empty:
            no_a = int(trades_a["no_price_cents"].median())
        if not trades_b.empty:
            no_b = int(trades_b["no_price_cents"].median())

        result = simulate_event(
            config=config,
            trades_a=trades_a,
            trades_b=trades_b,
            event_ticker=event_ticker,
            sport=sport,
            league=league,
            est_start=est_start,
            no_price_a=no_a,
            no_price_b=no_b,
            volume_a=ma["volume"] or 0,
            volume_b=mb["volume"] or 0,
        )
        results.append(result)

    conn.close()

    # Build summary
    entered = [r for r in results if r.entered]
    both = [r for r in entered if r.filled_a and r.filled_b]
    one = [r for r in entered if (r.filled_a) != (r.filled_b)]
    neither = [r for r in entered if not r.filled_a and not r.filled_b]
    net_pnl = sum(r.pnl for r in results)

    # Run baseline for comparison
    baseline_config = BacktestConfig()  # simultaneous, no filters, unit=20
    # (baseline computed separately or cached)

    return {
        "summary": {
            "events_tested": total,
            "events_entered": len(entered),
            "events_both_filled": len(both),
            "events_one_filled": len(one),
            "events_no_fill": len(neither),
            "net_pnl": round(net_pnl, 2),
            "fill_rate": round(len(both) / max(len(entered), 1), 3),
            "trap_rate": round(len(one) / max(len(entered), 1), 3),
        },
        "events": [
            {
                "event_ticker": r.event_ticker,
                "sport": r.sport,
                "league": r.league,
                "entered": r.entered,
                "filled_a": r.filled_a,
                "filled_b": r.filled_b,
                "price_a": r.price_a,
                "price_b": r.price_b,
                "combined": r.combined,
                "fill_time_a": r.fill_time_a,
                "fill_time_b": r.fill_time_b,
                "unit_size": r.unit_size,
                "pnl": r.pnl,
                "result": r.result,
            }
            for r in results
        ],
    }
```

- [ ] **Step 2: Verify it imports**

```bash
cd .worktrees/clio && PYTHONPATH=. .venv/Scripts/python -c "from clio.backtest import run_backtest; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add clio/backtest.py
git commit -m "feat(clio): price path builder and backtest runner"
```

---

## Task 5: API Endpoints

**Files:**
- Modify: `clio/dashboard/api.py`

- [ ] **Step 1: Add backtest API endpoints**

Add to `create_app()` in `api.py`, before the config endpoint:

```python
import threading

_backtest_state = {"running": False, "progress": 0.0, "events_processed": 0, "events_total": 0, "result": None}

@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    if _backtest_state["running"]:
        return jsonify({"error": "Backtest already running"}), 409

    from clio.strategies import BacktestConfig
    from clio.backtest import run_backtest
    from clio.config import ClioConfig

    raw = request.get_json() or {}
    config = BacktestConfig.from_dict(raw)
    cfg = ClioConfig()

    def progress_cb(current, total):
        _backtest_state["events_processed"] = current
        _backtest_state["events_total"] = total
        _backtest_state["progress"] = current / max(total, 1)

    def run():
        _backtest_state["running"] = True
        _backtest_state["progress"] = 0.0
        try:
            result = run_backtest(config, cfg, progress_cb=progress_cb)
            _backtest_state["result"] = result
        except Exception as e:
            _backtest_state["result"] = {"error": str(e)}
        finally:
            _backtest_state["running"] = False

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return jsonify({"status": "started"})


@app.route("/api/backtest/status")
def api_backtest_status():
    return jsonify({
        "running": _backtest_state["running"],
        "progress": _backtest_state["progress"],
        "events_processed": _backtest_state["events_processed"],
        "events_total": _backtest_state["events_total"],
    })


@app.route("/api/backtest/results")
def api_backtest_results():
    result = _backtest_state.get("result")
    if result is None:
        return jsonify({"error": "No backtest results. Run a backtest first."}), 404
    return jsonify(result)
```

- [ ] **Step 2: Verify API imports**

```bash
cd .worktrees/clio && PYTHONPATH=. .venv/Scripts/python -c "from clio.dashboard.api import create_app; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add clio/dashboard/api.py
git commit -m "feat(clio): backtest API endpoints with background execution"
```

---

## Task 6: Dashboard View

**Files:**
- Create: `clio/dashboard/src/views/Backtest.tsx`
- Modify: `clio/dashboard/src/api.ts`
- Modify: `clio/dashboard/src/App.tsx`
- Modify: `clio/dashboard/src/components/Sidebar.tsx`

- [ ] **Step 1: Add TypeScript types and fetch functions to `api.ts`**

Add backtest types and fetch functions matching the API response format. Include `BacktestConfig` request type, `BacktestSummary`, `BacktestEventResult`, and `BacktestResponse`.

- [ ] **Step 2: Write `Backtest.tsx`**

Two-panel layout:
- Left: Strategy builder with radio buttons (placement), checkboxes (filters, sizing), sliders/inputs for parameters, sport/league dropdowns, Run button with progress bar
- Right: KPI row (Net PnL, vs Baseline, Events, Fill Rate, Trap Rate), comparison table for multiple runs, per-event results table (sortable, color-coded)

Style consistent with existing views — dark theme, same card/table patterns.

- [ ] **Step 3: Add route to `App.tsx` and nav item to `Sidebar.tsx`**

Route: `/backtest` → lazy-loaded `Backtest` component.
Sidebar: Add "Backtest" nav item between "Trap Analysis" and "Configuration".

- [ ] **Step 4: Build and verify**

```bash
cd .worktrees/clio/clio/dashboard && npm run build
```

Expected: clean build.

- [ ] **Step 5: Commit**

```bash
git add clio/dashboard/src/
git commit -m "feat(clio): interactive backtest dashboard view"
```

---

## Task 7: Integration Test

**Files:**
- Modify: `tests/test_clio/test_backtest.py`

- [ ] **Step 1: Add integration test against real data**

```python
import pytest
from pathlib import Path

HISTORY_DB = Path(__file__).resolve().parent.parent.parent / "kalshi_history.db"

@pytest.mark.skipif(not HISTORY_DB.exists(), reason="Requires kalshi_history.db")
def test_backtest_small_subset():
    """Run backtest on a small sport subset to verify end-to-end."""
    from clio.strategies import BacktestConfig
    from clio.backtest import run_backtest
    from clio.config import ClioConfig

    config = BacktestConfig.from_dict({
        "placement": "sequential_thin",
        "placement_params": {"wait_timeout_seconds": 300},
        "entry_filters": {"price_threshold": {"min_combined_price": 80, "max_combined_price": 95}},
        "filters": {"sport": "Hockey"},
    })
    cfg = ClioConfig(history_db=HISTORY_DB)
    result = run_backtest(config, cfg)

    assert "summary" in result
    assert "events" in result
    assert result["summary"]["events_tested"] > 0
    assert isinstance(result["summary"]["net_pnl"], float)
```

- [ ] **Step 2: Run integration test**

```bash
cd .worktrees/clio && PYTHONPATH=. .venv/Scripts/python -m pytest tests/test_clio/test_backtest.py::test_backtest_small_subset -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_clio/test_backtest.py
git commit -m "test(clio): integration test for backtest runner"
```

---

## Summary

| Task | What | Key Files |
|------|------|-----------|
| 1 | Strategy dataclasses | `clio/strategies.py` |
| 2 | Fill simulator | `clio/backtest.py` |
| 3 | PnL calculator + event sim | `clio/backtest.py` |
| 4 | Price path builder + runner | `clio/backtest.py` |
| 5 | API endpoints | `clio/dashboard/api.py` |
| 6 | Dashboard view | `Backtest.tsx`, `api.ts`, `App.tsx`, `Sidebar.tsx` |
| 7 | Integration test | `test_backtest.py` |
