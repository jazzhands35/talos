"""Series clustering for the tree UI.

Given a flat list of SeriesNodes in a category, decide whether to group them
by tag, by settlement source, or to leave them flat. Purely functional — no
I/O, no Kalshi calls, no UI. Tree-UI code imports `cluster_series()` and
renders whatever it returns.

Sizing the dials empirically (see tools/analyze_all_categories.py):
- Tag coverage is ≥ 70% for 10/19 categories
- Source coverage (top 5) is ≥ 70% for 5/19 categories
- Both hover around 50% threshold; 50% is where "better than flat" pays off.

Heuristic chosen to match the observed data:
- Tag first: if ≥ 50% of series have ≥ 1 tag, group by first tag.
- Source second: else if the same fraction have a primary settlement source,
  group by primary_source.
- Otherwise: no clustering, series render directly under the category.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from talos.models.tree import SeriesNode

_MIN_COVERAGE_PCT = 50
# Singleton "clusters" are worse than no cluster — they add an extra click
# for a single leaf. Route them into orphans (rendered as top-level series
# nodes alongside the real clusters).
_MIN_CLUSTER_SIZE = 2

ClusterMode = Literal["tag", "source", "none"]


def cluster_series(
    series: list[SeriesNode],
) -> tuple[ClusterMode, list[tuple[str, list[SeriesNode]]], list[SeriesNode]]:
    """Group `series` for tree display.

    Returns:
        (mode, clusters, orphans)

        mode:     "tag" / "source" / "none"
        clusters: ordered [(name, members), ...]  — largest first, alpha tiebreak
        orphans:  series that belong to no cluster ≥ MIN_CLUSTER_SIZE
                  (render as direct children of the category)
    """
    if not series:
        return ("none", [], [])

    # Tag coverage first
    tagged = sum(1 for s in series if s.tags)
    if _pct(tagged, len(series)) >= _MIN_COVERAGE_PCT:
        return _group_tag(series)

    # Settlement-source fallback
    sourced = sum(1 for s in series if s.primary_source)
    if _pct(sourced, len(series)) >= _MIN_COVERAGE_PCT:
        return _group_source(series)

    return ("none", [], list(series))


def _group_tag(
    series: list[SeriesNode],
) -> tuple[ClusterMode, list[tuple[str, list[SeriesNode]]], list[SeriesNode]]:
    """Group by first tag; send untagged to their own 'Untagged' bucket (or
    to orphans if the bucket would be too small). Singleton tag groups go
    to orphans too — they're not worth a cluster row."""
    groups: dict[str, list[SeriesNode]] = defaultdict(list)
    untagged: list[SeriesNode] = []
    for s in series:
        if s.tags:
            groups[s.tags[0]].append(s)
        else:
            untagged.append(s)

    clusters: list[tuple[str, list[SeriesNode]]] = []
    orphans: list[SeriesNode] = []
    for name, members in groups.items():
        if len(members) >= _MIN_CLUSTER_SIZE:
            clusters.append((name, members))
        else:
            orphans.extend(members)

    if len(untagged) >= _MIN_CLUSTER_SIZE:
        clusters.append(("Untagged", untagged))
    else:
        orphans.extend(untagged)

    clusters.sort(key=lambda kv: (-len(kv[1]), kv[0]))
    return ("tag", clusters, orphans)


def _group_source(
    series: list[SeriesNode],
) -> tuple[ClusterMode, list[tuple[str, list[SeriesNode]]], list[SeriesNode]]:
    """Group by primary settlement source. Series without a source (rare —
    0 in practice across categories we surveyed, but handle defensively) go
    to orphans."""
    groups: dict[str, list[SeriesNode]] = defaultdict(list)
    missing: list[SeriesNode] = []
    for s in series:
        if s.primary_source:
            groups[s.primary_source].append(s)
        else:
            missing.append(s)

    clusters: list[tuple[str, list[SeriesNode]]] = []
    orphans: list[SeriesNode] = list(missing)
    for name, members in groups.items():
        if len(members) >= _MIN_CLUSTER_SIZE:
            clusters.append((name, members))
        else:
            orphans.extend(members)

    clusters.sort(key=lambda kv: (-len(kv[1]), kv[0]))
    return ("source", clusters, orphans)


def _pct(numer: int, denom: int) -> int:
    return int(100 * numer / denom) if denom else 0
