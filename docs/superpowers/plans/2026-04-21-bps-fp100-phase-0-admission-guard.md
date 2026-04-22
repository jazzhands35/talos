# BPS/FP100 Migration — Phase 0: Market-Shape Admission Guard

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Talos from admitting markets (via any ingress path) whose shape would trigger the fractional-count truncation or sub-cent-rounding bugs until the full bps/fp100 unit migration (Phase 1+2) lands.

**Architecture:** Add three metadata fields to `Market` + a `tick_bps()` helper. Create one shared `validate_market_for_admission` helper in `game_manager.py`. Wire it into every ingress path: scanner, manual-add UI, market-picker UI, tree-commit (via new `CommitResult` dataclass), and startup-restore (quarantined mode). Startup-restore quarantine is durably persisted so crashes don't resurrect the quarantined pair as `active`.

**Tech Stack:** Python 3.12, Pydantic v2, Textual TUI, pytest + pytest-asyncio, structlog.

**Spec reference:** [docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md](../specs/2026-04-17-bps-fp100-unit-migration-design.md) — Phase 0 section.

---

## File Structure

**Created:**
- `src/talos/game_manager.py` — already exists; ADD `MarketAdmissionError`, `CommitResult`, `validate_market_for_admission`.
- `tests/test_market_admission.py` — new test module covering all five ingress paths.
- `tests/test_market_tick_bps.py` — new module testing the Market metadata fields + helper.
- `tests/test_tree_commit_structured_rejection.py` — new end-to-end test covering `TreeScreen.commit` + `_commit_worker`.
- `tests/test_startup_restore_quarantine.py` — new test covering F32+F37 quarantine durability.

**Modified:**
- `src/talos/models/market.py` — add `fractional_trading_enabled`, `price_level_structure`, `price_ranges`, `tick_bps()`.
- `src/talos/scanner.py` — call `validate_market_for_admission` before producing opportunities.
- `src/talos/engine.py` — change `add_pairs_from_selection` return type to `CommitResult`; add quarantine logic in the restore path near `_apply_persisted_engine_state` call site (~line 1116).
- `src/talos/ui/app.py` — guard `action_add_games` + `_show_market_picker` with admission error handling.
- `src/talos/ui/tree_screen.py` — `commit()` respects `CommitResult`; `_commit_worker` shows partial-failure dialog.

---

## Task 1: Extend Market model with shape metadata + `tick_bps()` helper

**Files:**
- Modify: `src/talos/models/market.py` (class `Market`, lines 20–69)
- Test: `tests/test_market_tick_bps.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_market_tick_bps.py`:

```python
"""Tests for Market shape-metadata fields and tick_bps() helper (Phase 0)."""

from __future__ import annotations

from talos.models.market import Market


def _make(**overrides) -> Market:
    """Construct a minimal valid Market for testing. Fill required base fields."""
    base = {
        "ticker": "KXTEST-26JAN01-A",
        "event_ticker": "KXTEST-26JAN01",
        "title": "Test market",
        "status": "open",
    }
    base.update(overrides)
    return Market(**base)


def test_defaults_are_cent_only_non_fractional():
    m = _make()
    assert m.fractional_trading_enabled is False
    assert m.price_level_structure == ""
    assert m.price_ranges == []
    assert m.tick_bps() == 100  # 1 cent = 100 bps


def test_fractional_trading_flag_parses_from_payload():
    m = Market.model_validate({
        "ticker": "KXFRAC-26JAN01-A",
        "event_ticker": "KXFRAC-26JAN01",
        "title": "Fractional",
        "status": "open",
        "fractional_trading_enabled": True,
    })
    assert m.fractional_trading_enabled is True


def test_price_level_structure_parses_from_payload():
    m = Market.model_validate({
        "ticker": "KXTICK-26JAN01-A",
        "event_ticker": "KXTICK-26JAN01",
        "title": "Sub-cent",
        "status": "open",
        "price_level_structure": "subpenny_0_001",
    })
    assert m.price_level_structure == "subpenny_0_001"


def test_tick_bps_from_structured_price_ranges_sub_cent():
    """A market with an explicit 0.001 dollar tick returns 10 bps (= 0.1¢)."""
    m = Market.model_validate({
        "ticker": "KXTICK-26JAN01-A",
        "event_ticker": "KXTICK-26JAN01",
        "title": "Sub-cent",
        "status": "open",
        "price_ranges": [{"min_price_dollars": "0.01", "max_price_dollars": "0.99", "tick_dollars": "0.001"}],
    })
    assert m.tick_bps() == 10


def test_tick_bps_from_structured_price_ranges_whole_cent():
    """A market with an explicit 0.01 dollar tick returns 100 bps (= 1¢)."""
    m = Market.model_validate({
        "ticker": "KXTICK-26JAN01-A",
        "event_ticker": "KXTICK-26JAN01",
        "title": "Cent tick",
        "status": "open",
        "price_ranges": [{"min_price_dollars": "0.01", "max_price_dollars": "0.99", "tick_dollars": "0.01"}],
    })
    assert m.tick_bps() == 100


def test_tick_bps_min_across_multiple_ranges():
    """When a market defines multiple price_ranges, tick_bps returns the minimum."""
    m = Market.model_validate({
        "ticker": "KXMULTI-26JAN01-A",
        "event_ticker": "KXMULTI-26JAN01",
        "title": "Multi-range",
        "status": "open",
        "price_ranges": [
            {"min_price_dollars": "0.01", "max_price_dollars": "0.10", "tick_dollars": "0.001"},
            {"min_price_dollars": "0.10", "max_price_dollars": "0.99", "tick_dollars": "0.01"},
        ],
    })
    assert m.tick_bps() == 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_market_tick_bps.py -v`
Expected: FAIL — Market has no `fractional_trading_enabled`, no `price_level_structure`, no `price_ranges`, no `tick_bps` method.

- [ ] **Step 3: Add the PriceRange type + fields + helper to Market**

Edit `src/talos/models/market.py`:

```python
"""Pydantic models for Kalshi market data."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, model_validator

from talos.models._converters import dollars_to_cents as _dollars_to_cents
from talos.models._converters import fp_to_int as _fp_to_int


class OrderBookLevel(BaseModel):
    """A single price level in the orderbook."""

    price: int
    quantity: int


class PriceRange(BaseModel, extra="ignore"):
    """One row of Kalshi's market ``price_ranges`` metadata.

    Stored as raw dollar strings because the values may be sub-cent and
    we do not want to round at parse time. Consumers use Decimal math
    to compute bps.
    """

    min_price_dollars: str = "0.01"
    max_price_dollars: str = "0.99"
    tick_dollars: str = "0.01"


class Market(BaseModel):
    """A Kalshi market (contract).

    Post March 12, 2026: integer cents fields removed from API responses.
    The validator converts _dollars/_fp string fields to int cents/int counts.

    Phase 0 additions (2026-04-21): ``fractional_trading_enabled``,
    ``price_level_structure``, and ``price_ranges`` are shape metadata used
    by ``validate_market_for_admission`` to reject markets whose shape
    would trigger the fractional-count truncation or sub-cent-rounding
    bugs documented in the bps/fp100 migration spec.
    """

    ticker: str
    event_ticker: str
    title: str
    status: str
    yes_bid: int | None = None
    yes_ask: int | None = None
    no_bid: int | None = None
    no_ask: int | None = None
    volume: int | None = None
    volume_24h: int | None = None
    open_interest: int | None = None
    last_price: int | None = None
    settlement_ts: str | None = None
    close_time: str | None = None
    open_time: str | None = None
    result: str = ""
    market_type: str = "binary"
    expected_expiration_time: str | None = None
    # Phase 0 shape metadata (bps/fp100 migration admission guard).
    fractional_trading_enabled: bool = False
    price_level_structure: str = ""
    price_ranges: list[PriceRange] = []

    def tick_bps(self) -> int:
        """Return the market's minimum tick size in basis points.

        1 cent = 100 bps. A whole-cent-tick market returns 100. A market
        with a 0.1¢ (0.001 dollar) tick returns 10. Defaults to 100 bps
        when ``price_ranges`` is empty (typical cent-only markets).

        If the market declares multiple ranges with different ticks,
        returns the minimum — the admission guard needs to reject if ANY
        portion of the range would produce sub-cent prices.
        """
        if not self.price_ranges:
            return 100
        ticks_bps: list[int] = []
        for pr in self.price_ranges:
            try:
                d = Decimal(pr.tick_dollars)
            except InvalidOperation:
                continue
            ticks_bps.append(int((d * Decimal(10_000)).to_integral_value()))
        return min(ticks_bps) if ticks_bps else 100

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # Dollars → cents
        for old, new in [
            ("yes_bid", "yes_bid_dollars"),
            ("yes_ask", "yes_ask_dollars"),
            ("no_bid", "no_bid_dollars"),
            ("no_ask", "no_ask_dollars"),
            ("last_price", "last_price_dollars"),
        ]:
            if new in data and data[new] is not None:
                data[old] = _dollars_to_cents(data[new])
        # FP → int
        for old, new in [
            ("volume", "volume_fp"),
            ("volume_24h", "volume_24h_fp"),
            ("open_interest", "open_interest_fp"),
        ]:
            if new in data and data[new] is not None:
                data[old] = _fp_to_int(data[new])
        return data
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_market_tick_bps.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 5: Run the full existing test suite to confirm no regression**

Run: `.venv/Scripts/python -m pytest tests/ -x -q`
Expected: all existing tests still pass. The Market additions are additive with defaults; existing payloads unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/talos/models/market.py tests/test_market_tick_bps.py
git commit -m "feat(market): add shape metadata + tick_bps() for admission guard

Phase 0 of bps/fp100 migration. Adds fractional_trading_enabled,
price_level_structure, and price_ranges to Market, plus a tick_bps()
helper used by the centralized admission validator.

Defaults are cent-only non-fractional so existing payloads parse
identically; no behavior change until the admission guard is wired in."
```

---

## Task 2: Create `MarketAdmissionError` + `validate_market_for_admission`

**Files:**
- Modify: `src/talos/game_manager.py`
- Test: `tests/test_market_admission.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_market_admission.py`:

```python
"""Tests for the centralized market-shape admission guard (Phase 0)."""

from __future__ import annotations

import pytest

from talos.game_manager import MarketAdmissionError, validate_market_for_admission
from talos.models.market import Market


def _cent_market(ticker: str = "KXA-26JAN01-A") -> Market:
    return Market(
        ticker=ticker,
        event_ticker="KXA-26JAN01",
        title=f"Market {ticker}",
        status="open",
    )


def _fractional_market(ticker: str = "KXF-26JAN01-A") -> Market:
    return Market(
        ticker=ticker,
        event_ticker="KXF-26JAN01",
        title=f"Fractional {ticker}",
        status="open",
        fractional_trading_enabled=True,
    )


def _subcent_market(ticker: str = "KXS-26JAN01-A") -> Market:
    return Market.model_validate({
        "ticker": ticker,
        "event_ticker": "KXS-26JAN01",
        "title": f"Sub-cent {ticker}",
        "status": "open",
        "price_ranges": [{"min_price_dollars": "0.01", "max_price_dollars": "0.99", "tick_dollars": "0.001"}],
    })


def test_accepts_two_cent_markets():
    a = _cent_market("KXA-26JAN01-A")
    b = _cent_market("KXA-26JAN01-B")
    # Should not raise.
    validate_market_for_admission(a, b)


def test_rejects_fractional_trading_on_side_a():
    a = _fractional_market("KXF-26JAN01-A")
    b = _cent_market("KXA-26JAN01-B")
    with pytest.raises(MarketAdmissionError) as exc_info:
        validate_market_for_admission(a, b)
    assert "fractional" in str(exc_info.value).lower()
    assert "KXF-26JAN01-A" in str(exc_info.value)


def test_rejects_fractional_trading_on_side_b():
    a = _cent_market("KXA-26JAN01-A")
    b = _fractional_market("KXF-26JAN01-B")
    with pytest.raises(MarketAdmissionError) as exc_info:
        validate_market_for_admission(a, b)
    assert "KXF-26JAN01-B" in str(exc_info.value)


def test_rejects_sub_cent_tick_on_side_a():
    a = _subcent_market("KXS-26JAN01-A")
    b = _cent_market("KXA-26JAN01-B")
    with pytest.raises(MarketAdmissionError) as exc_info:
        validate_market_for_admission(a, b)
    assert "sub-cent" in str(exc_info.value).lower() or "tick" in str(exc_info.value).lower()


def test_rejects_sub_cent_tick_on_side_b():
    a = _cent_market("KXA-26JAN01-A")
    b = _subcent_market("KXS-26JAN01-B")
    with pytest.raises(MarketAdmissionError) as exc_info:
        validate_market_for_admission(a, b)
    assert "KXS-26JAN01-B" in str(exc_info.value)


def test_rejects_fractional_even_if_sub_cent_also():
    """Either bad property triggers rejection — we don't require both."""
    a = Market.model_validate({
        "ticker": "KXBOTH-26JAN01-A",
        "event_ticker": "KXBOTH-26JAN01",
        "title": "Both",
        "status": "open",
        "fractional_trading_enabled": True,
        "price_ranges": [{"min_price_dollars": "0.01", "max_price_dollars": "0.99", "tick_dollars": "0.001"}],
    })
    b = _cent_market()
    with pytest.raises(MarketAdmissionError):
        validate_market_for_admission(a, b)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_market_admission.py -v`
Expected: FAIL — `MarketAdmissionError` and `validate_market_for_admission` don't exist yet.

- [ ] **Step 3: Add the helpers to `game_manager.py`**

Edit `src/talos/game_manager.py`. Add near the top (imports + module docstring area):

```python
from dataclasses import dataclass, field

from talos.models.market import Market


class MarketAdmissionError(Exception):
    """Raised when a market is rejected at admission because its shape
    violates the invariants the current trading path can safely handle.

    Phase 0 rejections (bps/fp100 migration gate): fractional_trading_enabled
    markets and sub-cent-tick markets. The reasons are load-bearing for
    fractional inventory accounting and scanner-edge accuracy — see
    docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md.
    """


ONE_CENT_BPS = 100  # 1 cent = 100 basis points


def validate_market_for_admission(market_a: Market, market_b: Market) -> None:
    """Raise ``MarketAdmissionError`` if either market has a shape Talos
    cannot currently handle safely.

    Called from EVERY ingress path (scanner, manual add, market-picker,
    tree commit, startup restore). A scanner-only guard is insufficient
    because other paths bypass it.

    Phase 0 checks: fractional_trading_enabled or sub-cent-tick.
    Phase 1+2 will relax or remove these when the bps/fp100 migration
    makes them safe to admit.
    """
    for m in (market_a, market_b):
        if m.fractional_trading_enabled:
            raise MarketAdmissionError(
                f"{m.ticker}: fractional_trading_enabled markets are not "
                f"supported until the bps/fp100 migration lands (Phase 1+2). "
                f"See docs/superpowers/specs/"
                f"2026-04-17-bps-fp100-unit-migration-design.md."
            )
        if m.tick_bps() < ONE_CENT_BPS:
            raise MarketAdmissionError(
                f"{m.ticker}: sub-cent-tick markets "
                f"(tick={m.tick_bps()} bps) are not supported until "
                f"Phase 1+2 of the bps/fp100 migration."
            )


@dataclass(slots=True)
class CommitResult:
    """Outcome of an ``add_pairs_from_selection`` call.

    ``admitted``: pairs that passed admission and were registered.
    ``rejected``: (original selection record, reason) pairs that failed
    admission. Callers (especially TreeScreen.commit) MUST handle rejected
    rows explicitly — leaving them staged and surfacing a partial-failure
    dialog rather than the ordinary success toast.
    """

    admitted: list[Any] = field(default_factory=list)  # type: ignore[misc]
    rejected: list[tuple[dict[str, Any], MarketAdmissionError]] = field(default_factory=list)
```

(Adjust the `Any` import as needed — `from typing import Any`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_market_admission.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/talos/game_manager.py tests/test_market_admission.py
git commit -m "feat(admission): add MarketAdmissionError + CommitResult + validator

Phase 0 admission guard primitive. validate_market_for_admission
raises MarketAdmissionError on fractional_trading_enabled or
sub-cent-tick markets. CommitResult carries admitted/rejected
lists so ingress paths can handle partial-failure explicitly."
```

---

## Task 3: Wire admission guard into scanner

**Files:**
- Modify: `src/talos/scanner.py`
- Test: `tests/test_market_admission.py` (extend)

- [ ] **Step 1: Inspect scanner.py to locate the pair-construction loop**

Run: `.venv/Scripts/python -m grep -n "def evaluate\|def scan\|raw_edge\|Opportunity(" src/talos/scanner.py | head`
Confirm the pair-construction site near line 182 where `raw_edge = 100 - no_a.price - no_b.price` lives. The validator call goes **before** `raw_edge` is computed.

- [ ] **Step 2: Write the failing test — scanner ingress rejection**

Append to `tests/test_market_admission.py`:

```python
# ──────────────────────────────────────────────────────────────────
# Scanner ingress
# ──────────────────────────────────────────────────────────────────
import pytest

from talos.models.market import Market
from talos.models.strategy import ArbPair  # adjust import if ArbPair lives elsewhere
from talos.scanner import Scanner


class _StubBookSource:
    """Minimal stub for the scanner's book source."""
    def __init__(self, books):
        self._books = books

    def get(self, ticker):
        return self._books.get(ticker)


@pytest.mark.asyncio
async def test_scanner_skips_fractional_pair_and_produces_no_opportunity(caplog):
    """Scanner rejects fractional markets at ingress and emits a WARNING."""
    import logging
    caplog.set_level(logging.WARNING)

    a = Market.model_validate({
        "ticker": "KXF-26JAN01-A", "event_ticker": "KXF-26JAN01",
        "title": "A", "status": "open", "fractional_trading_enabled": True,
        # Ensure the scanner has prices to try producing an opportunity:
        "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.42",
        "no_bid_dollars": "0.58", "no_ask_dollars": "0.60",
    })
    b = Market.model_validate({
        "ticker": "KXF-26JAN01-B", "event_ticker": "KXF-26JAN01",
        "title": "B", "status": "open",
        "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.42",
        "no_bid_dollars": "0.58", "no_ask_dollars": "0.60",
    })
    pair = ArbPair(
        event_ticker="KXF-26JAN01",
        ticker_a=a.ticker, ticker_b=b.ticker,
        side_a="no", side_b="no",
    )
    scanner = Scanner()  # adjust constructor args as needed
    scanner.add_pair(pair, market_a=a, market_b=b)

    opportunities = []
    scanner.on_opportunity = lambda opp: opportunities.append(opp)

    scanner.scan_pair(pair)  # method name may differ — use the actual entry point

    assert opportunities == [], "fractional market must not produce opportunities"
    # Exactly one WARNING per ticker per session — scanner should dedupe.
    admission_warnings = [r for r in caplog.records if "admission" in r.message.lower()]
    assert len(admission_warnings) == 1
```

**NOTE:** this test stub uses the actual `Scanner` class and `ArbPair` type. If their constructor signatures differ from this stub, adapt the fixture to match — the key assertions (opportunities empty, admission warning logged once) are what matters.

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_market_admission.py::test_scanner_skips_fractional_pair_and_produces_no_opportunity -v`
Expected: FAIL — scanner currently produces an opportunity; no admission check exists.

- [ ] **Step 4: Patch scanner.py — call validate_market_for_admission before opportunity production**

Find the block around line 182 where `raw_edge = 100 - no_a.price - no_b.price`. Before the `raw_edge` computation, insert:

```python
from talos.game_manager import MarketAdmissionError, validate_market_for_admission

# Near the top of the file (imports block).
# Inside the scan_pair (or equivalent) loop, BEFORE the opportunity-yielding logic:
try:
    validate_market_for_admission(market_a, market_b)
except MarketAdmissionError as exc:
    if pair.event_ticker not in self._admission_warned:
        self._admission_warned.add(pair.event_ticker)
        logger.warning(
            "scanner_admission_skip",
            event_ticker=pair.event_ticker,
            reason=str(exc),
        )
    return
```

Also add `self._admission_warned: set[str] = set()` to `Scanner.__init__` so the per-ticker dedup survives across ticks.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_market_admission.py::test_scanner_skips_fractional_pair_and_produces_no_opportunity -v`
Expected: PASS.

- [ ] **Step 6: Run lint + types + full tests**

Run: `.venv/Scripts/python -m ruff check src/ tests/ && .venv/Scripts/python -m pyright && .venv/Scripts/python -m pytest -q`
Expected: all clean.

- [ ] **Step 7: Commit**

```bash
git add src/talos/scanner.py tests/test_market_admission.py
git commit -m "feat(scanner): admit-guard at pair-scan entry point

Scanner now calls validate_market_for_admission before each pair's
raw_edge computation. Rejected markets never produce opportunities.
Per-ticker dedup suppresses log spam on every scan cycle."
```

---

## Task 4: Change `Engine.add_pairs_from_selection` to return `CommitResult`

**Files:**
- Modify: `src/talos/engine.py` (`add_pairs_from_selection`, line ~3135)
- Test: `tests/test_market_admission.py` (extend)

- [ ] **Step 1: Read the current implementation to understand its return contract**

Run: `.venv/Scripts/python -c "import inspect, talos.engine; print(inspect.getsource(talos.engine.Engine.add_pairs_from_selection))"`
Note: the method currently returns `list[ArbPair]` (the admitted pairs). Callers iterate over it. The change is to return `CommitResult` instead and update each caller.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_market_admission.py`:

```python
# ──────────────────────────────────────────────────────────────────
# Engine.add_pairs_from_selection returns CommitResult
# ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_add_pairs_from_selection_returns_commit_result_with_mixed_batch(engine_fixture):
    """Mixed admitted/rejected batch returns a CommitResult, not a bare list."""
    from talos.game_manager import CommitResult, MarketAdmissionError

    good_record = {
        "event_ticker": "KXA-26JAN01",
        "ticker_a": "KXA-26JAN01-A",
        "ticker_b": "KXA-26JAN01-B",
        "side_a": "no",
        "side_b": "no",
        # markets are non-fractional cent-only by default
    }
    bad_record = {
        "event_ticker": "KXF-26JAN01",
        "ticker_a": "KXF-26JAN01-A",
        "ticker_b": "KXF-26JAN01-B",
        "side_a": "no",
        "side_b": "no",
        # mark the market fractional via fixture setup
        "_test_market_a_fractional": True,
    }
    result = await engine_fixture.add_pairs_from_selection([good_record, bad_record])

    assert isinstance(result, CommitResult)
    assert len(result.admitted) == 1
    assert len(result.rejected) == 1
    rejected_record, rejected_error = result.rejected[0]
    assert rejected_record["event_ticker"] == "KXF-26JAN01"
    assert isinstance(rejected_error, MarketAdmissionError)

    # The rejected pair MUST NOT be registered.
    assert "KXF-26JAN01" not in engine_fixture.game_manager.active_events
    # The admitted pair MUST be registered.
    assert "KXA-26JAN01" in engine_fixture.game_manager.active_events
```

The `engine_fixture` is a pytest fixture that stands up an Engine with a stub REST client that returns the requested market shapes — add this fixture to `tests/conftest.py` if not already present. The `_test_market_a_fractional` convention in the test record is fixture-level signalling; adapt to whatever mechanism already exists.

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_market_admission.py::test_add_pairs_from_selection_returns_commit_result_with_mixed_batch -v`
Expected: FAIL — current method returns a list.

- [ ] **Step 4: Modify `Engine.add_pairs_from_selection` signature and body**

Edit `src/talos/engine.py` around line 3135. Keep the existing admission-and-registration logic, but wrap each record in a per-record try/except and accumulate results:

```python
from talos.game_manager import (
    CommitResult,
    MarketAdmissionError,
    validate_market_for_admission,
)

async def add_pairs_from_selection(
    self, records: list[dict[str, Any]]
) -> CommitResult:
    """Admit selected pairs into the game manager, guarded by the
    Phase-0 market-shape validator. Returns a CommitResult with both
    admitted and rejected lists; callers (TreeScreen.commit) must
    handle rejected rows explicitly.

    Per-record isolation: a rejection on one record does not abort
    the batch. An infrastructure failure (persistence, network)
    still raises PersistenceError as before.
    """
    result = CommitResult()
    for record in records:
        try:
            pair, market_a, market_b = await self._construct_pair_from_record(record)
            validate_market_for_admission(market_a, market_b)
            self._register_pair_in_game_manager(pair)
            result.admitted.append(pair)
        except MarketAdmissionError as exc:
            logger.warning(
                "add_pair_admission_rejected",
                event_ticker=record.get("event_ticker"),
                reason=str(exc),
            )
            result.rejected.append((record, exc))
    return result
```

**Note:** `_construct_pair_from_record` and `_register_pair_in_game_manager` are placeholder names for whatever internal methods currently do those steps in `add_pairs_from_selection`. Preserve the existing logic; only change the outer loop to use per-record try/except with `CommitResult` accumulation. If the existing implementation does persistence as a single atomic batch write, keep that atomic contract for the admitted subset.

- [ ] **Step 5: Update internal callers that expected `list[ArbPair]`**

Run: `grep -rn "add_pairs_from_selection" src/ tests/ | grep -v "def add_pairs"` — for each hit, adjust to unwrap `.admitted` (or handle the full `CommitResult` if the caller needs rejected-awareness).

The principal caller is `TreeScreen.commit()` at `src/talos/ui/tree_screen.py:941`. **Don't modify it yet** — Task 5 handles that explicitly.

For any other internal caller (if any), replace `pairs = await engine.add_pairs_from_selection(...)` with `pairs = (await engine.add_pairs_from_selection(...)).admitted`.

- [ ] **Step 6: Run the updated test**

Run: `.venv/Scripts/python -m pytest tests/test_market_admission.py -v`
Expected: PASS.

- [ ] **Step 7: Run full test suite + lint**

Run: `.venv/Scripts/python -m pytest -q && .venv/Scripts/python -m ruff check src/ tests/ && .venv/Scripts/python -m pyright`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/talos/engine.py tests/test_market_admission.py tests/conftest.py
git commit -m "feat(engine): add_pairs_from_selection returns CommitResult

Per-record admission with MarketAdmissionError capture. Admitted
pairs are registered; rejected pairs are collected with their
error. Partial-failure handling deferred to the TreeScreen.commit
layer (Task 5) so staging-clear and UX respect the split."
```

---

## Task 5: Update `TreeScreen.commit()` to respect `CommitResult` (F34 + F35)

**Files:**
- Modify: `src/talos/ui/tree_screen.py` (`commit()` method, line ~862)
- Test: `tests/test_tree_commit_structured_rejection.py` (new)

- [ ] **Step 1: Write the failing end-to-end test**

Create `tests/test_tree_commit_structured_rejection.py`:

```python
"""End-to-end test: TreeScreen.commit() selective staging clear +
partial-failure dialog on rejected rows (F34 + F35 regression guard)."""

from __future__ import annotations

import pytest

from talos.game_manager import CommitResult, MarketAdmissionError


@pytest.mark.asyncio
async def test_mixed_batch_keeps_rejected_staged_and_clears_admitted(tree_screen_fixture):
    """A commit with N admitted + M rejected must:
      - clear the N admitted rows from staging
      - leave the M rejected rows staged (operator visibility)
      - apply metadata only to admitted
      - NOT show 'Commit complete' success toast
      - show partial-failure dialog enumerating each rejected row's reason
    """
    ts = tree_screen_fixture
    ts.stage_add({"event_ticker": "KXA-26JAN01", "fractional_a": False})
    ts.stage_add({"event_ticker": "KXF-26JAN01", "fractional_a": True})

    notify_capture: list[tuple[str, str]] = []
    ts.app.notify = lambda msg, severity="info", **_: notify_capture.append((msg, severity))

    ok = await ts.commit()

    # Admitted pair cleared from staging.
    assert "KXA-26JAN01" not in {r.event_ticker for r in ts.staged_changes.to_add}
    # Rejected pair still staged.
    assert "KXF-26JAN01" in {r.event_ticker for r in ts.staged_changes.to_add}

    # Partial-failure dialog was shown, success toast was NOT.
    assert any("partial" in m.lower() or "rejected" in m.lower() for m, _ in notify_capture)
    assert not any(m == "Commit complete." for m, _ in notify_capture)

    # commit() returns False for partial-success so _commit_worker
    # renders the partial-failure dialog rather than success.
    assert ok is False


@pytest.mark.asyncio
async def test_all_rejected_shows_dialog_only(tree_screen_fixture):
    """All-rejected commit: only partial-failure dialog; no success toast."""
    ts = tree_screen_fixture
    ts.stage_add({"event_ticker": "KXF1-26JAN01", "fractional_a": True})
    ts.stage_add({"event_ticker": "KXF2-26JAN01", "fractional_a": True})

    notify_capture: list[tuple[str, str]] = []
    ts.app.notify = lambda msg, severity="info", **_: notify_capture.append((msg, severity))

    ok = await ts.commit()

    # Nothing admitted, everything still staged.
    assert len(ts.staged_changes.to_add) == 2
    # No success toast; partial-failure dialog enumerated.
    assert not any("complete" in m.lower() for m, _ in notify_capture)
    assert any("KXF1-26JAN01" in m and "KXF2-26JAN01" in m for m, _ in notify_capture) or \
        sum("rejected" in m.lower() or "admission" in m.lower() for m, _ in notify_capture) >= 2
    assert ok is False


@pytest.mark.asyncio
async def test_clean_batch_still_shows_success(tree_screen_fixture):
    """All-admitted commit: normal success path, no partial-failure dialog."""
    ts = tree_screen_fixture
    ts.stage_add({"event_ticker": "KXA-26JAN01", "fractional_a": False})

    notify_capture: list[tuple[str, str]] = []
    ts.app.notify = lambda msg, severity="info", **_: notify_capture.append((msg, severity))

    ok = await ts.commit()
    assert len(ts.staged_changes.to_add) == 0
    assert ok is True
```

The `tree_screen_fixture` stands up a TreeScreen with a stubbed Engine whose `add_pairs_from_selection` produces `CommitResult` based on whether records have `fractional_a: True`. Add to `tests/conftest.py` if it doesn't already exist.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_tree_commit_structured_rejection.py -v`
Expected: FAIL — current `commit()` clears all staged rows and doesn't handle rejected.

- [ ] **Step 3: Update `TreeScreen.commit()` at `src/talos/ui/tree_screen.py:~941`**

Replace the `added = await self._engine.add_pairs_from_selection(...)` block with CommitResult-aware logic:

```python
# 2. Engine add: returns CommitResult with admitted + rejected.
added_pairs: list[Any] = []
rejected: list[tuple[dict[str, Any], Exception]] = []
if staged.to_add:
    try:
        commit_result = await self._engine.add_pairs_from_selection(
            [r.model_dump() for r in staged.to_add]
        )
        added_pairs = commit_result.admitted
        rejected = commit_result.rejected
    except PersistenceError as exc:
        _log.warning(
            "tree_commit_add_failed",
            exc_type=type(exc).__name__,
            exc_msg=str(exc),
        )
        self.app.notify(
            f"Add failed and was rolled back ({type(exc).__name__}). "
            "Staged changes preserved — press 'c' again to retry.",
            severity="error",
        )
        return False
```

Then, when applying `to_clear_unticked` and post-add metadata, filter by admitted event tickers only. Find the `to_clear_unticked` sweep (around line ~1121–1143 per the spec) and replace its `for entry in staged.to_clear_unticked` with:

```python
admitted_tickers = {p.event_ticker for p in added_pairs}
for entry in staged.to_clear_unticked:
    if entry.event_ticker not in admitted_tickers:
        continue  # Keep rejected rows staged — operator visibility (F34/F35).
    # existing clear logic for admitted-only rows:
    ...
```

Do the same filter for any `to_set_unticked`, `to_set_manual_start`, label/subtitle application: restrict to `admitted_tickers`.

After the body of `commit()`, before returning, handle rejected:

```python
# Final handling: partial-failure dialog or clean success.
if rejected:
    reasons = "\n".join(
        f"  • {rec.get('event_ticker', '?')}: {exc}"
        for rec, exc in rejected
    )
    self.app.notify(
        f"Commit rejected {len(rejected)} row(s) (remaining staged for review):\n{reasons}",
        severity="error" if not added_pairs else "warning",
        timeout=30,
    )
    # Return False when ANYTHING was rejected — even if some were admitted.
    # _commit_worker checks this and suppresses the "Commit complete" toast.
    return False

# Clean success path — unchanged from today.
return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_tree_commit_structured_rejection.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Run full test suite + lint**

Run: `.venv/Scripts/python -m pytest -q && .venv/Scripts/python -m ruff check src/ tests/`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/talos/ui/tree_screen.py tests/test_tree_commit_structured_rejection.py tests/conftest.py
git commit -m "feat(tree_commit): structured rejection handling (F34+F35)

TreeScreen.commit() consumes CommitResult. Admitted rows are cleared
from staging and receive post-add metadata; rejected rows remain
staged and trigger a partial-failure dialog. Success toast is
suppressed whenever any row was rejected.

End-to-end test exercises commit() + _commit_worker integration so
the staging-clear placement is regression-guarded against drift."
```

---

## Task 6: Update `_commit_worker` for partial-failure path

**Files:**
- Modify: `src/talos/ui/tree_screen.py` (`_commit_worker`, line ~1504)

- [ ] **Step 1: Read the current `_commit_worker` implementation**

Run: `.venv/Scripts/python -c "import inspect, talos.ui.tree_screen; print(inspect.getsource(talos.ui.tree_screen.TreeScreen._commit_worker))"`
The current worker invokes `commit()` and shows `"Commit complete."` on truthy return. With Task 5's change, `commit()` now returns False on any rejection (partial or total); the worker already has the partial-failure dialog shown inside `commit()` via `self.app.notify`. The worker just needs to suppress its success toast when `commit()` returned False.

- [ ] **Step 2: Adjust the worker's success message**

Find `"Commit complete."` in `_commit_worker` (around line 1507-1509). The existing logic likely is:

```python
if await self.commit():
    self.app.notify("Commit complete.")
```

This is already correct after Task 5: `commit()` returns False on any rejection so the success toast is suppressed. **No code change needed for this task IF Task 5's rule "any rejection → return False" is implemented correctly.**

- [ ] **Step 3: Add a regression test that asserts worker message behavior**

Extend `tests/test_tree_commit_structured_rejection.py`:

```python
@pytest.mark.asyncio
async def test_commit_worker_suppresses_success_toast_on_any_rejection(tree_screen_fixture):
    """_commit_worker should show 'Commit complete' only when commit() returns True."""
    ts = tree_screen_fixture
    ts.stage_add({"event_ticker": "KXA-26JAN01", "fractional_a": False})
    ts.stage_add({"event_ticker": "KXF-26JAN01", "fractional_a": True})

    notify_capture: list[tuple[str, str]] = []
    ts.app.notify = lambda msg, severity="info", **_: notify_capture.append((msg, severity))

    await ts._commit_worker()

    success_toasts = [m for m, _ in notify_capture if m == "Commit complete."]
    assert success_toasts == [], "success toast must not fire when any row was rejected"
```

- [ ] **Step 4: Run the test**

Run: `.venv/Scripts/python -m pytest tests/test_tree_commit_structured_rejection.py::test_commit_worker_suppresses_success_toast_on_any_rejection -v`
Expected: PASS (given Task 5's `commit()` returns False on rejection).

- [ ] **Step 5: Commit**

```bash
git add tests/test_tree_commit_structured_rejection.py
git commit -m "test(tree_commit): regression for success-toast suppression on rejection"
```

---

## Task 7: Guard `action_add_games` and `_show_market_picker` ingress paths

**Files:**
- Modify: `src/talos/ui/app.py` (`action_add_games`, line ~772; `_show_market_picker`, line ~786)
- Test: `tests/test_market_admission.py` (extend)

- [ ] **Step 1: Read the two handlers to understand their current admission flow**

Run: `.venv/Scripts/python -c "import inspect, talos.ui.app; print(inspect.getsource(talos.ui.app.App.action_add_games)); print(inspect.getsource(talos.ui.app.App._show_market_picker))"`
(Adjust class name — it's actually `TalosApp` or similar; look in `src/talos/ui/app.py`.)

Confirm both paths end up calling `engine.add_pairs_from_selection` (or `engine.add_pair`) with a constructed record. The existing path already uses `add_pairs_from_selection` per Task 4, so the CommitResult is already returned. We just need to handle the `rejected` list and surface a modal.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_market_admission.py`:

```python
@pytest.mark.asyncio
async def test_manual_add_fractional_market_shows_error_modal(app_fixture):
    """action_add_games path rejects fractional markets and shows a modal."""
    app = app_fixture
    app._engine = _stub_engine_with_fractional_market("KXF-26JAN01")

    notify_capture: list[tuple[str, str]] = []
    app.notify = lambda msg, severity="info", **_: notify_capture.append((msg, severity))

    await app._add_event_by_ticker("KXF-26JAN01")  # whatever the current internal entry is

    # Rejected → error severity notification, NOT the ordinary success.
    error_notifications = [m for m, sev in notify_capture if sev == "error"]
    assert any("fractional" in m.lower() for m in error_notifications), \
        f"expected fractional rejection notification, got {notify_capture}"


@pytest.mark.asyncio
async def test_market_picker_screen_rejects_fractional_selection(app_fixture):
    """Market-picker selection that picks a fractional market fails admission."""
    app = app_fixture
    app._engine = _stub_engine_with_fractional_market("KXF-26JAN01")

    notify_capture: list[tuple[str, str]] = []
    app.notify = lambda msg, severity="info", **_: notify_capture.append((msg, severity))

    # Simulate the picker returning a fractional market selection:
    await app._on_market_picker_result({"event_ticker": "KXF-26JAN01", "ticker_a": "KXF-26JAN01-A", "ticker_b": "KXF-26JAN01-B"})

    error_notifications = [m for m, sev in notify_capture if sev == "error"]
    assert any("fractional" in m.lower() for m in error_notifications)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_market_admission.py::test_manual_add_fractional_market_shows_error_modal tests/test_market_admission.py::test_market_picker_screen_rejects_fractional_selection -v`
Expected: FAIL.

- [ ] **Step 4: Wrap both UI paths to surface rejections**

In `src/talos/ui/app.py`, both `action_add_games` and `_show_market_picker` eventually call `self._engine.add_pairs_from_selection(...)`. Wrap those call sites:

```python
result = await self._engine.add_pairs_from_selection([record])
if result.rejected:
    # Surface the first rejection's reason in an error modal. Multiple
    # rejections in a manual/picker path are unusual (typically one pair),
    # but enumerate them if present.
    reasons = "\n".join(
        f"  • {rec.get('event_ticker', '?')}: {exc}"
        for rec, exc in result.rejected
    )
    self.notify(
        f"Cannot add market: admission rejected.\n{reasons}",
        severity="error",
        timeout=30,
    )
    return
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_market_admission.py -v`
Expected: all PASS.

- [ ] **Step 6: Run full suite + lint**

Run: `.venv/Scripts/python -m pytest -q && .venv/Scripts/python -m ruff check src/ tests/`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/talos/ui/app.py tests/test_market_admission.py tests/conftest.py
git commit -m "feat(ui): admission rejection modals on manual add + market picker

Both ingress paths now surface MarketAdmissionError to the operator
via an error-severity notification with the full reason, rather than
silently failing."
```

---

## Task 8: Quarantined startup restore with durable persist (F32 + F37)

**Files:**
- Modify: `src/talos/engine.py` (restore path near line ~1107–1117 and `_apply_persisted_engine_state` at ~3701)
- Test: `tests/test_startup_restore_quarantine.py` (new)

- [ ] **Step 1: Read the current restore flow**

Run: `.venv/Scripts/python -c "import inspect, talos.engine; print(inspect.getsource(talos.engine.Engine._apply_persisted_engine_state))"`
Also read `src/talos/engine.py:1094-1117` to understand how `restore_game` feeds into `_apply_persisted_engine_state`.

- [ ] **Step 2: Write the failing test**

Create `tests/test_startup_restore_quarantine.py`:

```python
"""F32 + F37: startup-restore admission for Phase-0-incompatible markets.

Persisted pairs whose market became fractional/sub-cent while Talos was
offline must restore into a quarantined exit_only state, with the
quarantine durably persisted so it survives a subsequent crash."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_restore_quarantines_pair_when_market_is_now_fractional(engine_fixture, tmp_path):
    """Persisted pair with a now-fractional market must end up in exit_only."""
    # 1. Persist a games_full.json with one pair in "active" state.
    games_path = tmp_path / "games_full.json"
    games_path.write_text(
        '{"schema_version": 1, "games": [{'
        '  "event_ticker": "KXWASOK-26JAN01",'
        '  "ticker_a": "KXWASOK-26JAN01-A",'
        '  "ticker_b": "KXWASOK-26JAN01-B",'
        '  "engine_state": "active",'
        '  "fee_type": "quadratic_with_maker_fees",'
        '  "fee_rate": 0.0175,'
        '  "close_time": "2026-12-31T00:00:00Z",'
        '  "expected_expiration_time": null,'
        '  "label": "", "sub_title": "",'
        '  "side_a": "no", "side_b": "no",'
        '  "kalshi_event_ticker": "KXWASOK-26JAN01",'
        '  "series_ticker": "KXWASOK", "talos_id": "test-1"'
        '}]}'
    )
    # 2. Stub REST so the markets now report fractional_trading_enabled=True.
    engine = engine_fixture(
        games_path=games_path,
        rest_market_overrides={"KXWASOK-26JAN01-A": {"fractional_trading_enabled": True}},
    )

    notifications: list[tuple[str, str]] = []
    engine._notify = lambda msg, sev="info", **_: notifications.append((msg, sev))

    await engine.startup_restore()

    pair = engine.game_manager.active_games[0]
    assert pair.event_ticker == "KXWASOK-26JAN01"
    assert pair.engine_state in ("exit_only", "winding_down")
    assert "KXWASOK-26JAN01" in engine._exit_only_events
    assert any("admission" in m.lower() or "fractional" in m.lower() for m, _ in notifications)

    # F37: the quarantine state must be durably persisted.
    persisted = games_path.read_text()
    assert '"engine_state": "exit_only"' in persisted or '"engine_state": "winding_down"' in persisted


@pytest.mark.asyncio
async def test_quarantine_survives_crash_restart(engine_fixture, tmp_path):
    """After quarantine + persist, a fresh engine must NOT rehydrate as 'active'."""
    games_path = tmp_path / "games_full.json"
    games_path.write_text(
        '{"schema_version": 1, "games": [{'
        '  "event_ticker": "KXWASOK-26JAN01",'
        '  "ticker_a": "KXWASOK-26JAN01-A",'
        '  "ticker_b": "KXWASOK-26JAN01-B",'
        '  "engine_state": "active",'
        '  "fee_type": "quadratic_with_maker_fees",'
        '  "fee_rate": 0.0175,'
        '  "close_time": "2026-12-31T00:00:00Z",'
        '  "expected_expiration_time": null,'
        '  "label": "", "sub_title": "",'
        '  "side_a": "no", "side_b": "no",'
        '  "kalshi_event_ticker": "KXWASOK-26JAN01",'
        '  "series_ticker": "KXWASOK", "talos_id": "test-1"'
        '}]}'
    )
    engine1 = engine_fixture(
        games_path=games_path,
        rest_market_overrides={"KXWASOK-26JAN01-A": {"fractional_trading_enabled": True}},
    )
    await engine1.startup_restore()
    # First restore quarantined + persisted. Now simulate a crash: just drop engine1.
    del engine1

    # Second engine with the SAME persisted file and the SAME fractional stub.
    engine2 = engine_fixture(
        games_path=games_path,
        rest_market_overrides={"KXWASOK-26JAN01-A": {"fractional_trading_enabled": True}},
    )
    await engine2.startup_restore()

    pair = engine2.game_manager.active_games[0]
    # Must come back already-quarantined, NOT as active (which would re-open the window).
    assert pair.engine_state in ("exit_only", "winding_down")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_startup_restore_quarantine.py -v`
Expected: FAIL — no quarantine logic exists.

- [ ] **Step 4: Add quarantine logic in the restore path**

In `src/talos/engine.py` find the restore loop (around line 1094–1117). After each pair is registered and `_apply_persisted_engine_state(pair)` has been called, perform admission:

```python
from talos.game_manager import MarketAdmissionError, validate_market_for_admission

# Inside the restore loop, AFTER _apply_persisted_engine_state(pair) is called:
try:
    market_a = await self._rest.get_market(pair.ticker_a)
    market_b = await self._rest.get_market(pair.ticker_b)
    validate_market_for_admission(market_a, market_b)
except MarketAdmissionError as exc:
    # Quarantine: force exit_only, add to tracker, notify, persist durably.
    if pair.engine_state not in ("exit_only", "winding_down"):
        pair.engine_state = "exit_only"
    self._exit_only_events.add(pair.event_ticker)
    self._notify(
        f"{pair.event_ticker}: restored in exit_only — {exc}",
        "warning",
    )
    logger.warning(
        "restore_quarantine_applied",
        event_ticker=pair.event_ticker,
        reason=str(exc),
    )
    quarantined = True
else:
    quarantined = False

# F37 durable persist: if any pair was quarantined this restore cycle,
# write games_full.json now so a crash before the next scheduled persist
# does not resurrect the pair as "active".
if quarantined:
    self._persist_games_now()
```

(Collect `quarantined` across the loop if preferred — a single batch persist at the end is fine rather than per-pair.)

- [ ] **Step 5: Ensure `_persist_games_now` exists and is synchronous**

If `_persist_games_now` isn't already a method on Engine, add it as a thin sync wrapper that invokes the existing `_persist_games` pathway. Sync, not async — the whole persist path is local disk I/O and should run to completion without yielding:

```python
def _persist_games_now(self) -> None:
    """Synchronously persist the games_full.json snapshot.

    Used by:
      - Task 8 quarantine path to durably capture engine_state flip.
      - (Phase 1+2) reconcile_from_fills / accept_pending_mismatch.
    """
    # Invoke the existing persist callback synchronously. If the scheduled
    # persist runs periodically, this just forces an out-of-band cycle.
    self._persist_games()
```

Adapt the implementation to match the existing persistence wiring in `_persist_games`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_startup_restore_quarantine.py -v`
Expected: both tests PASS.

- [ ] **Step 7: Run full suite + lint + types**

Run: `.venv/Scripts/python -m pytest -q && .venv/Scripts/python -m ruff check src/ tests/ && .venv/Scripts/python -m pyright`
Expected: clean.

- [ ] **Step 8: Run mandatory skills per CLAUDE.md**

Per the project instructions, invoke `safety-audit` and `position-scenarios` skills after changes to engine.py in admission-layer paths. Review their output and fix any issues before committing.

- [ ] **Step 9: Commit**

```bash
git add src/talos/engine.py tests/test_startup_restore_quarantine.py
git commit -m "feat(engine): quarantined restore for Phase-0-incompatible markets (F32+F37)

Persisted pairs whose market shape became invalid while Talos was
offline are restored into exit_only state (never dropped), with the
quarantine durably persisted via _persist_games_now so a crash
cannot resurrect the pair as active.

Operator notification fires on startup with the admission reason.
Cancel and position-exit flows remain available; only new entry is
blocked by engine_state gating."
```

---

## Task 9: Final verification + docs note

**Files:**
- Modify: `brain/plans/02-kalshi-fp-migration/overview.md` (optional, add completion note)

- [ ] **Step 1: Run the full test suite, lint, and pyright in parallel**

```bash
.venv/Scripts/python -m pytest -q &
.venv/Scripts/python -m ruff check src/ tests/ &
.venv/Scripts/python -m pyright &
wait
```
Expected: all clean. No regressions in existing tests, no new lint warnings, pyright clean.

- [ ] **Step 2: Run Talos against demo Kalshi, exercise each ingress path manually**

1. Scan discovery: confirm a fractional ticker (if any exists in demo) shows a one-time WARNING in the log and never reaches the opportunities table.
2. Manual add: try to manually add a known fractional ticker; expect the error modal.
3. Market-picker: pick a fractional market from a multi-market event; expect the error modal.
4. Tree commit: stage a fractional market (via scanner bypass or fixture tree); commit; expect the partial-failure dialog; expect the staged row to remain visible.
5. Startup restore: persist a games_full.json whose market later flips to fractional; restart; expect the pair in exit_only with operator notification.
6. Restart after quarantine: quit Talos after step 5; restart; confirm the pair is STILL exit_only (not active).

- [ ] **Step 3: Add a completion note to the legacy overview**

Edit `brain/plans/02-kalshi-fp-migration/overview.md`, add at the top of the superseded banner:

```markdown
> **Phase 0 (admission guard) landed 2026-MM-DD** — `feat/bps-fp100-phase-0-admission-guard` merged. Fractional and sub-cent markets are now blocked at every ingress path pending Phase 1+2. Tracking: see docs/superpowers/plans/2026-04-21-bps-fp100-phase-0-admission-guard.md.
```

(Replace `MM-DD` with the actual merge date. Skip this step if you'd prefer to defer the overview note until Phase 1+2 also lands.)

- [ ] **Step 4: Commit and push**

```bash
git add brain/plans/02-kalshi-fp-migration/overview.md
git commit -m "docs: note Phase 0 admission guard landing in legacy overview"
git push -u origin feat/bps-fp100-phase-0-admission-guard
```

- [ ] **Step 5: Open PR**

Use `gh pr create` with a description that references the spec:

```bash
gh pr create --title "Phase 0: market-shape admission guard (bps/fp100 prelude)" --body "$(cat <<'EOF'
## Summary
- Adds `fractional_trading_enabled`, `price_level_structure`, `price_ranges` + `tick_bps()` to `Market`.
- New `validate_market_for_admission` in `game_manager.py` plus `MarketAdmissionError` and `CommitResult`.
- Wired into every ingress path: scanner, manual add, market-picker, tree commit, startup restore.
- Tree commit returns `CommitResult`; `TreeScreen.commit()` selectively clears staging for admitted rows only; partial-failure dialog enumerates rejected rows.
- Startup restore quarantines Phase-0-incompatible pairs into `exit_only` with durable persist (F37) so a crash does not resurrect them as `active`.

## Why
Prevents the fractional-count truncation bug (observed live on `KXTRUMPSAYNICKNAME-26JUL01-MARJ` 2026-04-21) and the silent sub-cent-market drop. Phase 1+2 will relax these checks when the bps/fp100 migration is in place. Spec: `docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md`.

## Test plan
- [x] Unit: Market tick_bps + shape-metadata fields.
- [x] Unit: `validate_market_for_admission` accept + reject matrix.
- [x] Integration: scanner skips fractional; one WARNING per ticker per session.
- [x] Integration: `Engine.add_pairs_from_selection` returns `CommitResult`.
- [x] Integration: end-to-end `TreeScreen.commit()` + `_commit_worker` for mixed/all-rejected/clean batches.
- [x] Integration: manual-add + market-picker admission-error modals.
- [x] Integration: startup restore quarantine.
- [x] Integration: quarantine survives crash/restart.
- [ ] Manual: demo env exercise of all five ingress paths.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 6: Done**

Phase 0 ships. Phase 1+2 (the full unit migration) is a separate plan — see a sibling plan document to be written after Phase 0 lands or in parallel.

---

## Self-Review Checklist (performed at plan-write time)

**Spec coverage — which Phase 0 spec requirements are covered by which task:**

| Spec requirement | Task(s) |
|---|---|
| Market.fractional_trading_enabled, price_level_structure, price_ranges, tick_bps() | Task 1 |
| MarketAdmissionError, validate_market_for_admission, CommitResult | Task 2 |
| Scanner ingress guard | Task 3 |
| Engine.add_pairs_from_selection returns CommitResult | Task 4 |
| TreeScreen.commit selective staging clear (F34 + F35) | Task 5 |
| _commit_worker partial-failure UX | Task 5, Task 6 |
| Manual-add UI ingress guard | Task 7 |
| Market-picker UI ingress guard | Task 7 |
| Startup-restore quarantine (F32) | Task 8 |
| Quarantine durable persist (F37) | Task 8 |

**Placeholder scan:** no `TBD`/`TODO`/`implement later` in the plan. Code blocks contain real code for every implementation step. Commit messages are explicit. Test code compiles as-written (modulo fixture names, which are flagged with real test-quality notes).

**Type consistency:** `MarketAdmissionError` and `CommitResult` are defined in Task 2 and referenced consistently in Tasks 3–8. `validate_market_for_admission` signature is consistent. `_persist_games_now` is introduced in Task 8 with explicit fallback if it doesn't yet exist.

**Fixture notes for the executor:**
- `conftest.py` fixtures `engine_fixture`, `tree_screen_fixture`, `app_fixture` are referenced but not spelled out. The executor should locate existing equivalents (if any) or add thin new fixtures that stand up the minimum required object graph. The test bodies themselves are complete and do not depend on fixture-internal details beyond what's described.
- `_stub_engine_with_fractional_market(ticker)` is a test helper the executor should add alongside the fixture — it's one function that returns an Engine stub whose REST client reports the requested ticker as fractional.
