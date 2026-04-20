"""Empirical analysis: prefix clustering vs tag-based grouping for Politics."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.request import urlopen

_KALSHI = "https://api.elections.kalshi.com/trade-api/v2/series"


def fetch_series() -> list[dict]:
    cache = Path(__file__).parent / "_series_cache.json"
    if cache.exists():
        data = json.loads(cache.read_text())
        # Legacy cache stored the bare list; new cache stores the raw dict.
        if isinstance(data, list):
            return data
        return data["series"]
    print(f"Fetching {_KALSHI} ...")
    with urlopen(_KALSHI, timeout=60) as resp:
        data = json.loads(resp.read())
    cache.write_text(json.dumps(data))
    return data["series"]


def longest_common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def prefix_cluster(
    tickers: list[str], *, min_prefix: int, min_group: int
) -> tuple[dict[str, list[str]], list[str]]:
    """Group tickers by longest-common-prefix runs in sorted order.

    Returns (cluster_prefix -> members, ungrouped singletons).
    """
    tickers = sorted(tickers)
    clusters: dict[str, list[str]] = {}
    ungrouped: list[str] = []

    i = 0
    while i < len(tickers):
        best_prefix = tickers[i]
        members = [tickers[i]]
        j = i + 1
        while j < len(tickers):
            lcp = longest_common_prefix(best_prefix, tickers[j])
            if lcp < min_prefix:
                break
            best_prefix = best_prefix[:lcp]
            members.append(tickers[j])
            j += 1

        if len(members) >= min_group:
            clusters[best_prefix] = members
        else:
            ungrouped.extend(members)
        i = j

    return clusters, ungrouped


def explore_fields(politics: list[dict]) -> None:
    """Dump the schema of a typical Politics series + field fill-rates."""
    print("\n--- AVAILABLE FIELDS ON A POLITICS SERIES ---")
    sample = politics[0]
    for k in sorted(sample.keys()):
        val = sample[k]
        preview = json.dumps(val)[:120] if val is not None else "null"
        print(f"  {k:30s} = {preview}")

    print("\n--- FIELD FILL-RATES (non-null, non-empty) ---")
    from collections import Counter as _C

    fills: _C[str] = _C()
    n = len(politics)
    for s in politics:
        for k, v in s.items():
            if v not in (None, "", [], {}):
                fills[k] += 1
    for k, c in fills.most_common():
        pct = int(100 * c / n)
        print(f"  {k:30s}  {c:4d} / {n}  ({pct}%)")

    print("\n--- DISTINCT VALUES FOR LOW-CARDINALITY FIELDS ---")
    candidates = [
        "frequency",
        "fee_type",
        "fee_multiplier",
        "mutually_exclusive",
        "product_metadata.market_type",
    ]
    from collections import Counter as _C2

    for field in candidates:
        values: _C2[Any] = _C2()  # type: ignore[name-defined]
        for s in politics:
            cur: Any = s  # type: ignore[name-defined]
            for part in field.split("."):
                if isinstance(cur, dict):
                    cur = cur.get(part)
                else:
                    cur = None
                    break
            if cur is not None:
                values[json.dumps(cur) if isinstance(cur, list | dict) else cur] += 1
        if values:
            print(f"  {field}: {dict(values.most_common(10))}")


def main() -> None:
    all_series = fetch_series()
    politics = [s for s in all_series if s.get("category") == "Politics"]
    print(f"\n=== Politics category: {len(politics)} series ===\n")
    explore_fields(politics)

    # Strip KX prefix so clustering works on the meaningful part
    def _normalize(t: str) -> str:
        return t[2:] if t.startswith("KX") else t

    tickers = [_normalize(s["ticker"]) for s in politics]

    # --- Prefix clustering ---
    print("--- PREFIX CLUSTERING ---")
    for min_prefix, min_group in [(4, 2), (5, 2), (6, 2), (6, 3), (7, 3), (8, 3)]:
        clusters, ungrouped = prefix_cluster(
            tickers, min_prefix=min_prefix, min_group=min_group
        )
        group_sizes = Counter(len(m) for m in clusters.values())
        print(
            f"min_prefix={min_prefix}, min_group={min_group}: "
            f"{len(clusters)} clusters, "
            f"{len(ungrouped)} ungrouped singletons, "
            f"top cluster sizes: {sorted(group_sizes.items(), reverse=True)[:5]}"
        )

    # Pick one as headline example
    print("\n--- HEADLINE EXAMPLE: min_prefix=6, min_group=3 ---")
    clusters, ungrouped = prefix_cluster(tickers, min_prefix=6, min_group=3)
    print(f"Total clusters: {len(clusters)}")
    print(f"Singletons / ungrouped: {len(ungrouped)}")
    print(f"Series covered by clusters: {sum(len(m) for m in clusters.values())}")
    print()
    print("Top 15 clusters by size:")
    for prefix, members in sorted(
        clusters.items(), key=lambda kv: -len(kv[1])
    )[:15]:
        print(f"  KX{prefix}*  ({len(members)} series)")
    print()
    singleton_clusters = [p for p, m in clusters.items() if len(m) == 1]
    two_member_clusters = [p for p, m in clusters.items() if len(m) == 2]
    print(f"1-member clusters (shouldn't happen with min_group=3): {len(singleton_clusters)}")
    print(f"2-member clusters: {len(two_member_clusters)}")

    # --- Tag analysis ---
    print("\n--- TAG ANALYSIS ---")
    series_tags: list[tuple[str, list[str]]] = [
        (s["ticker"], s.get("tags") or []) for s in politics
    ]
    no_tags = [t for t, tags in series_tags if not tags]
    print(f"Series with NO tags: {len(no_tags)} / {len(politics)}")
    if no_tags[:5]:
        print(f"  examples: {no_tags[:5]}")

    tag_counter: Counter[str] = Counter()
    for _, tags in series_tags:
        for t in tags:
            tag_counter[t] += 1
    print(f"Unique tags: {len(tag_counter)}")
    print(f"Top 15 tags by series count:")
    for tag, n in tag_counter.most_common(15):
        print(f"  {tag}: {n} series")

    multi_tag = [(t, tags) for t, tags in series_tags if len(tags) > 1]
    print(f"\nSeries with MULTIPLE tags: {len(multi_tag)} / {len(politics)}")
    tag_count_dist = Counter(len(tags) for _, tags in series_tags)
    print(f"Distribution of tag counts per series: {sorted(tag_count_dist.items())}")

    # Singleton-tag problem: if we grouped by first tag, how many tags would have only 1 series?
    first_tag_group: dict[str, list[str]] = defaultdict(list)
    untagged: list[str] = []
    for ticker, tags in series_tags:
        if tags:
            first_tag_group[tags[0]].append(ticker)
        else:
            untagged.append(ticker)
    singleton_tag_groups = [t for t, m in first_tag_group.items() if len(m) == 1]
    print(
        f"\nGroup by FIRST tag: {len(first_tag_group)} groups, "
        f"{len(singleton_tag_groups)} of them have only 1 series, "
        f"{len(untagged)} untagged series left over"
    )

    # --- Settlement sources ---
    print("\n--- SETTLEMENT-SOURCE ANALYSIS ---")
    primary_source: Counter[str] = Counter()
    no_source = 0
    for s in politics:
        srcs = s.get("settlement_sources") or []
        if srcs:
            primary_source[srcs[0].get("name", "?")] += 1
        else:
            no_source += 1
    print(f"Distinct primary sources: {len(primary_source)}")
    print(f"Series with no source: {no_source}")
    print("Top 15 sources by series count:")
    for name, n in primary_source.most_common(15):
        print(f"  {name}: {n}")

    # --- Frequency partition ---
    print("\n--- FREQUENCY PARTITION ---")
    freq_counts = Counter(s.get("frequency", "?") for s in politics)
    for f, n in freq_counts.most_common():
        print(f"  {f}: {n}")

    # --- Title keyword clustering (first word, normalized) ---
    print("\n--- TITLE FIRST-WORD CLUSTERS ---")
    STOPWORDS = {"the", "a", "an", "will"}
    first_word_groups: dict[str, list[str]] = defaultdict(list)
    for s in politics:
        words = s.get("title", "").strip().split()
        w = next(
            (x.lower().strip(".,:?") for x in words if x.lower() not in STOPWORDS),
            "",
        )
        first_word_groups[w or "?"].append(s["ticker"])
    print(f"Distinct first-words: {len(first_word_groups)}")
    sizes = Counter(len(m) for m in first_word_groups.values())
    singletons_fw = sum(c for size, c in sizes.items() if size == 1)
    print(f"Singletons: {singletons_fw}")
    print("Top 15 first-word groups:")
    for w, members in sorted(
        first_word_groups.items(), key=lambda kv: -len(kv[1])
    )[:15]:
        print(f"  '{w}': {len(members)}")


if __name__ == "__main__":
    main()
