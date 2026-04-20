"""Tests for tree/discovery Pydantic models."""

from datetime import UTC, datetime

from talos.models.tree import (
    ArbPairRecord,
    CategoryNode,
    Milestone,
    RemoveOutcome,
    StagedChanges,
)


def test_arbpair_record_minimal_fields() -> None:
    r = ArbPairRecord(
        event_ticker="KXFEDMENTION-26APR-YIEL",
        ticker_a="KXFEDMENTION-26APR-YIEL",
        ticker_b="KXFEDMENTION-26APR-YIEL",
        kalshi_event_ticker="KXFEDMENTION-26APR",
        series_ticker="KXFEDMENTION",
        category="Mentions",
    )
    assert r.side_a == "yes"
    assert r.side_b == "no"
    assert r.source == "tree"
    assert r.markets is None  # null means "all active"
    assert r.volume_24h_a is None


def test_arbpair_record_carries_volume_data() -> None:
    r = ArbPairRecord(
        event_ticker="KXFEDMENTION-26APR-YIEL",
        ticker_a="KXFEDMENTION-26APR-YIEL",
        ticker_b="KXFEDMENTION-26APR-YIEL",
        kalshi_event_ticker="KXFEDMENTION-26APR",
        series_ticker="KXFEDMENTION",
        category="Mentions",
        volume_24h_a=1234,
        volume_24h_b=1234,
    )
    assert r.volume_24h_a == 1234
    assert r.volume_24h_b == 1234


def test_remove_outcome_statuses() -> None:
    o = RemoveOutcome(
        pair_ticker="K-1",
        kalshi_event_ticker="K",
        status="winding_down",
        reason="filled=5,3",
    )
    assert o.status == "winding_down"
    assert o.reason == "filled=5,3"


def test_staged_changes_empty() -> None:
    s = StagedChanges.empty()
    assert s.to_add == []
    assert s.to_remove == []
    assert s.is_empty()


def test_milestone_start_date_parses() -> None:
    m = Milestone(
        id="abc",
        category="mentions",
        type="one_off_milestone",
        start_date=datetime(2026, 4, 22, 20, 0, tzinfo=UTC),
        end_date=datetime(2026, 4, 22, 22, 0, tzinfo=UTC),
        title="Survivor Episode 9",
        related_event_tickers=["KXSURVIVORMENTION-26APR23"],
    )
    assert m.start_date.year == 2026


def test_category_node_series_count() -> None:
    cat = CategoryNode(name="Mentions", series_count=335, series={})
    assert cat.series_count == 335
