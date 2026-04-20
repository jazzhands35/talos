"""Survey each category's groupability via settlement_sources, tags, and prefix clustering."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path


def load_series() -> list[dict]:
    cache = Path(__file__).parent / "_series_cache.json"
    data = json.loads(cache.read_text())
    if isinstance(data, list):
        return data
    return data["series"]


def longest_common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def prefix_cluster_count(
    tickers: list[str], *, min_prefix: int, min_group: int
) -> tuple[int, int]:
    """Returns (cluster_count, ungrouped_count) for the given thresholds."""
    tickers = sorted(t[2:] if t.startswith("KX") else t for t in tickers)
    clusters = 0
    ungrouped = 0
    i = 0
    while i < len(tickers):
        best = tickers[i]
        members = 1
        j = i + 1
        while j < len(tickers):
            lcp = longest_common_prefix(best, tickers[j])
            if lcp < min_prefix:
                break
            best = best[:lcp]
            members += 1
            j += 1
        if members >= min_group:
            clusters += 1
        else:
            ungrouped += members
        i = j
    return clusters, ungrouped


def analyze_category(cat_name: str, series: list[dict]) -> dict:
    n = len(series)
    primary_sources: Counter[str] = Counter()
    for s in series:
        srcs = s.get("settlement_sources") or []
        if srcs:
            primary_sources[srcs[0].get("name", "?")] += 1

    distinct_sources = len(primary_sources)
    top5_coverage = sum(c for _, c in primary_sources.most_common(5))
    top10_coverage = sum(c for _, c in primary_sources.most_common(10))

    tagged = sum(1 for s in series if s.get("tags"))
    distinct_tags = len(
        {t for s in series for t in (s.get("tags") or [])}
    )

    clusters, ungrouped = prefix_cluster_count(
        [s["ticker"] for s in series], min_prefix=6, min_group=3
    )

    return {
        "n": n,
        "distinct_sources": distinct_sources,
        "top5_cov": top5_coverage,
        "top5_pct": int(100 * top5_coverage / n) if n else 0,
        "top10_cov": top10_coverage,
        "top10_pct": int(100 * top10_coverage / n) if n else 0,
        "top3_source_names": [name for name, _ in primary_sources.most_common(3)],
        "tag_coverage": tagged,
        "tag_pct": int(100 * tagged / n) if n else 0,
        "distinct_tags": distinct_tags,
        "prefix_clusters": clusters,
        "prefix_ungrouped": ungrouped,
        "prefix_covered_pct": int(100 * (n - ungrouped) / n) if n else 0,
    }


def main() -> None:
    all_series = load_series()
    cats: dict[str, list[dict]] = defaultdict(list)
    for s in all_series:
        c = s.get("category", "?") or "Uncategorized"
        cats[c].append(s)

    results = {c: analyze_category(c, sl) for c, sl in cats.items()}

    # Order by size descending
    ordered = sorted(results.items(), key=lambda kv: -kv[1]["n"])

    print(f"{'Category':<22}{'N':>6}{'Src':>5}{'Top5%':>7}{'Top10%':>8}"
          f"{'Tag%':>6}{'Tags':>6}{'Pre#':>6}{'PreCov%':>9}   Top-3 sources")
    print("-" * 110)
    for cat, r in ordered:
        top = " / ".join(r["top3_source_names"][:3])[:40]
        print(
            f"{cat:<22}{r['n']:>6}{r['distinct_sources']:>5}"
            f"{r['top5_pct']:>6}%{r['top10_pct']:>7}%"
            f"{r['tag_pct']:>5}%{r['distinct_tags']:>6}"
            f"{r['prefix_clusters']:>6}{r['prefix_covered_pct']:>8}%"
            f"   {top}"
        )

    print()
    print("Legend:")
    print("  N         = series count")
    print("  Src       = distinct primary settlement sources")
    print("  Top5%     = % of series covered by top 5 sources")
    print("  Top10%    = % of series covered by top 10 sources")
    print("  Tag%      = % of series with at least one tag")
    print("  Tags      = distinct tags in this category")
    print("  Pre#      = number of prefix clusters @ min_prefix=6, min_group=3")
    print("  PreCov%   = % of series covered by a prefix cluster")


if __name__ == "__main__":
    main()
