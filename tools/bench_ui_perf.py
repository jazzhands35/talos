"""Microbenchmark for UI hot-path performance at scale.

Simulates 500 pairs / 300 events with positions to measure the
core computation time of _recompute_positions() and related loops.

Usage:
    .venv/Scripts/python tools/bench_ui_perf.py

Outputs a single integer: total milliseconds for one full cycle.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import structlog

# Suppress structlog noise during benchmark
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(50))

from talos.cpm import CPMTracker
from talos.models.proposal import Proposal, ProposalKey
from talos.models.strategy import ArbPair, Opportunity
from talos.position_ledger import PositionLedger, Side, compute_display_positions
from talos.proposal_queue import ProposalQueue


def _make_pairs(n: int) -> list[ArbPair]:
    """Create N mock ArbPairs."""
    return [
        ArbPair(
            event_ticker=f"EVT-{i:04d}",
            ticker_a=f"MKT-{i:04d}-A",
            ticker_b=f"MKT-{i:04d}-B",
        )
        for i in range(n)
    ]


def _make_ledgers(
    pairs: list[ArbPair],
    fraction_with_position: float = 0.6,
) -> dict[str, PositionLedger]:
    """Create ledgers with varying states for a fraction of pairs."""
    ledgers: dict[str, PositionLedger] = {}
    for i, pair in enumerate(pairs):
        ledger = PositionLedger(pair.event_ticker)
        if i < int(len(pairs) * fraction_with_position):
            # Simulate various position states
            if i % 4 == 0:
                # Balanced fills, no resting
                ledger.record_fill(Side.A, 20, 25, fees=1)
                ledger.record_fill(Side.B, 20, 30, fees=1)
            elif i % 4 == 1:
                # Imbalanced fills with resting
                ledger.record_fill(Side.A, 20, 25, fees=1)
                ledger.record_placement(Side.B, f"order-{i}-b", 20, 30)
            elif i % 4 == 2:
                # Both sides resting
                ledger.record_placement(Side.A, f"order-{i}-a", 20, 25)
                ledger.record_placement(Side.B, f"order-{i}-b", 20, 30)
            else:
                # Only resting on one side
                ledger.record_placement(Side.A, f"order-{i}-a", 20, 25)
        ledgers[pair.event_ticker] = ledger
    return ledgers


def _make_proposals(pairs: list[ArbPair], count: int) -> ProposalQueue:
    """Create a proposal queue with `count` pending proposals."""
    queue = ProposalQueue()
    for i in range(min(count, len(pairs))):
        kind = "bid" if i % 2 == 0 else "adjustment"
        key = ProposalKey(
            event_ticker=pairs[i].event_ticker,
            side="A" if i % 3 != 0 else "",
            kind=kind,
        )
        queue.add(
            Proposal(
                key=key,
                kind=kind,
                summary=f"Test proposal {i}",
                detail="",
                created_at=datetime.now(UTC),
            )
        )
    return queue


def _make_opportunities(pairs: list[ArbPair]) -> dict[str, Opportunity]:
    """Create opportunity snapshots for all pairs."""
    return {
        pair.event_ticker: Opportunity(
            event_ticker=pair.event_ticker,
            ticker_a=pair.ticker_a,
            ticker_b=pair.ticker_b,
            no_a=25,
            no_b=30,
            raw_edge=45,
            fee_edge=40,
        )
        for pair in pairs
    }


def bench_compute_display_positions(
    ledgers: dict[str, PositionLedger],
    pairs: list[ArbPair],
    cpm: CPMTracker,
) -> float:
    """Benchmark compute_display_positions()."""
    queue_cache: dict[str, int] = {}
    t0 = time.perf_counter()
    compute_display_positions(ledgers, pairs, queue_cache, cpm)
    return (time.perf_counter() - t0) * 1000


def bench_find_pair_linear(pairs: list[ArbPair], event_tickers: list[str]) -> float:
    """Benchmark _find_pair() linear scan pattern."""
    t0 = time.perf_counter()
    for et in event_tickers:
        for pair in pairs:
            if pair.event_ticker == et:
                break
    return (time.perf_counter() - t0) * 1000


def bench_find_pair_dict(
    pair_index: dict[str, ArbPair], event_tickers: list[str]
) -> float:
    """Benchmark dict lookup (optimized replacement)."""
    t0 = time.perf_counter()
    for et in event_tickers:
        pair_index.get(et)
    return (time.perf_counter() - t0) * 1000


def bench_proposal_scan(queue: ProposalQueue, event_tickers: list[str]) -> float:
    """Benchmark proposal queue iteration per event (current pattern)."""
    t0 = time.perf_counter()
    for et in event_tickers:
        pending_kinds = set()
        for p in queue.pending():
            if p.key.event_ticker == et:
                pending_kinds.add(p.kind)
    return (time.perf_counter() - t0) * 1000


def bench_proposal_prebuilt(queue: ProposalQueue, event_tickers: list[str]) -> float:
    """Benchmark pre-built proposal dict (optimized replacement)."""
    t0 = time.perf_counter()
    # Pre-build once
    pending_by_event: dict[str, set[str]] = {}
    for p in queue.pending():
        pending_by_event.setdefault(p.key.event_ticker, set()).add(p.kind)
    # Then O(1) lookups
    for et in event_tickers:
        pending_by_event.get(et, set())
    return (time.perf_counter() - t0) * 1000


def bench_summary_scan(
    summaries: list[object], pairs: list[ArbPair]
) -> float:
    """Benchmark next(s for s in summaries ...) pattern."""
    t0 = time.perf_counter()
    for pair in pairs:
        next(
            (s for s in summaries if getattr(s, "event_ticker", None) == pair.event_ticker),
            None,
        )
    return (time.perf_counter() - t0) * 1000


def bench_summary_dict(
    summary_index: dict[str, object], pairs: list[ArbPair]
) -> float:
    """Benchmark dict lookup for summary (optimized replacement)."""
    t0 = time.perf_counter()
    for pair in pairs:
        summary_index.get(pair.event_ticker)
    return (time.perf_counter() - t0) * 1000


def _one_cycle(
    pairs: list[ArbPair],
    ledgers: dict[str, PositionLedger],
    queue: ProposalQueue,
    cpm: CPMTracker,
    events_with_pos: list[str],
    pair_index: dict[str, ArbPair],
) -> tuple[int, dict[str, float]]:
    """Run one benchmark cycle matching actual engine patterns."""
    t_cdp = bench_compute_display_positions(ledgers, pairs, cpm)

    # _find_pair: now O(1) dict lookup in engine (was linear scan)
    t_find = bench_find_pair_dict(pair_index, events_with_pos * 2)

    # Proposal scan: now pre-built dict in engine (was per-event iteration)
    t_proposals = bench_proposal_prebuilt(queue, events_with_pos)

    # Summary scan: now dict lookup in _recompute_positions (was linear)
    summaries = compute_display_positions(ledgers, pairs, {}, cpm)
    summary_index = {s.event_ticker: s for s in summaries}
    t_summary = bench_summary_dict(summary_index, pairs)

    total_ms = int(t_cdp + t_find + t_proposals + t_summary)
    breakdown = {
        "cdp": t_cdp,
        "find": t_find,
        "proposals": t_proposals,
        "summary": t_summary,
    }
    return total_ms, breakdown


def run_benchmark(n_pairs: int = 500, n_proposals: int = 100) -> int:
    """Run benchmark with warmup + median of 5. Returns median ms."""
    import statistics
    import sys

    pairs = _make_pairs(n_pairs)
    ledgers = _make_ledgers(pairs)
    queue = _make_proposals(pairs, n_proposals)
    cpm = CPMTracker()

    events_with_pos = [
        p.event_ticker for p in pairs
        if p.event_ticker in ledgers and (
            ledgers[p.event_ticker].filled_count(Side.A)
            + ledgers[p.event_ticker].resting_count(Side.A) > 0
            or ledgers[p.event_ticker].filled_count(Side.B)
            + ledgers[p.event_ticker].resting_count(Side.B) > 0
        )
    ]

    # Build pair index (matches engine._pair_index pattern)
    pair_index = {p.event_ticker: p for p in pairs}

    # Warmup
    _one_cycle(pairs, ledgers, queue, cpm, events_with_pos, pair_index)

    # 5 timed runs
    results = []
    for _ in range(5):
        ms, breakdown = _one_cycle(pairs, ledgers, queue, cpm, events_with_pos, pair_index)
        results.append(ms)

    median = int(statistics.median(results))

    # Diagnostics to stderr
    print(f"  pairs={n_pairs} events_with_pos={len(events_with_pos)} proposals={n_proposals}", file=sys.stderr)
    print(f"  last breakdown: cdp={breakdown['cdp']:.1f} find={breakdown['find']:.1f} "
          f"proposals={breakdown['proposals']:.1f} summary={breakdown['summary']:.1f}", file=sys.stderr)
    print(f"  runs: {results}", file=sys.stderr)
    print(f"  median: {median}ms", file=sys.stderr)

    # All patterns now use dict lookups (matching engine optimization)

    # stdout = metric only
    print(median)
    return median


if __name__ == "__main__":
    run_benchmark()
