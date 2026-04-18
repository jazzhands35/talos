"""Unit tests for tree_clustering.cluster_series."""
from __future__ import annotations

from talos.models.tree import SeriesNode
from talos.tree_clustering import cluster_series


def _mk(ticker: str, tags: list[str] | None = None, source: str = "") -> SeriesNode:
    return SeriesNode(
        ticker=ticker,
        title=ticker,
        category="Test",
        tags=tags or [],
        primary_source=source,
    )


def test_empty_input_returns_none_mode() -> None:
    mode, clusters, orphans = cluster_series([])
    assert mode == "none"
    assert clusters == []
    assert orphans == []


def test_tag_grouping_when_coverage_above_threshold() -> None:
    # 6/8 = 75% tagged -> tag mode
    series = [
        _mk("A", tags=["alpha"]),
        _mk("B", tags=["alpha"]),
        _mk("C", tags=["alpha"]),
        _mk("D", tags=["beta"]),
        _mk("E", tags=["beta"]),
        _mk("F", tags=["gamma"]),  # singleton tag → orphan
        _mk("G"),
        _mk("H"),
    ]
    mode, clusters, orphans = cluster_series(series)
    assert mode == "tag"
    cluster_names = [n for n, _ in clusters]
    # alpha (3) is largest; beta (2) qualifies; untagged (2) qualifies
    assert "alpha" in cluster_names
    assert "beta" in cluster_names
    assert "Untagged" in cluster_names
    assert "gamma" not in cluster_names
    # Singleton tag goes to orphans
    assert [s.ticker for s in orphans] == ["F"]
    # Clusters sorted by size desc
    assert clusters[0][0] == "alpha"
    assert len(clusters[0][1]) == 3


def test_source_fallback_when_tags_sparse() -> None:
    # 2/10 = 20% tagged -> below threshold, fall through to source
    # 8/10 = 80% have a source -> source mode
    series = [
        _mk("A", source="ABC"),
        _mk("B", source="ABC"),
        _mk("C", source="ABC"),
        _mk("D", source="ABC"),
        _mk("E", source="CNN"),
        _mk("F", source="CNN"),
        _mk("G", source="Fox"),  # singleton source → orphan
        _mk("H", source="Reuters"),  # singleton source → orphan
        _mk("I", tags=["x"]),
        _mk("J", tags=["x"]),
    ]
    mode, clusters, orphans = cluster_series(series)
    assert mode == "source"
    assert clusters[0] == ("ABC", [s for s in series if s.primary_source == "ABC"])
    cluster_names = [n for n, _ in clusters]
    assert "CNN" in cluster_names
    assert "Fox" not in cluster_names  # singleton
    # I and J have no source but also no cluster; they go to orphans
    assert {s.ticker for s in orphans} >= {"G", "H", "I", "J"}


def test_no_clustering_when_both_dimensions_sparse() -> None:
    # 1/10 tagged, 1/10 sourced → none mode
    series = [_mk(f"S{i}") for i in range(10)]
    series[0] = _mk("S0", tags=["x"])
    series[1] = _mk("S1", source="ABC")
    mode, clusters, orphans = cluster_series(series)
    assert mode == "none"
    assert clusters == []
    assert len(orphans) == 10


def test_threshold_boundary_exactly_50_percent_tagged_uses_tags() -> None:
    # 5/10 = 50% -> should use tag mode (≥50 threshold)
    series = [_mk(f"A{i}", tags=["x"]) for i in range(5)] + [
        _mk(f"B{i}") for i in range(5)
    ]
    mode, _, _ = cluster_series(series)
    assert mode == "tag"
