"""Regression: _persist_games must preserve source + engine_state round-trip
so flag-off sessions don't strip durability fields."""

from pathlib import Path

from talos.models.strategy import ArbPair
from talos.persistence import load_saved_games_full, save_games_full


def test_games_full_preserves_source_field(tmp_path: Path):
    record: dict[str, str | float | None] = {
        "event_ticker": "KXFEDMENTION-26APR-YIEL",
        "ticker_a": "KXFEDMENTION-26APR-YIEL",
        "ticker_b": "KXFEDMENTION-26APR-YIEL",
        "side_a": "yes",
        "side_b": "no",
        "fee_type": "quadratic_with_maker_fees",
        "fee_rate": 0.0175,
        "source": "tree",
        "engine_state": "winding_down",
    }
    save_games_full([record], path=tmp_path / "games_full.json")
    loaded = load_saved_games_full(path=tmp_path / "games_full.json")
    assert loaded is not None
    assert loaded[0]["source"] == "tree"
    assert loaded[0]["engine_state"] == "winding_down"


def test_persist_games_roundtrips_source_and_engine_state(tmp_path: Path, monkeypatch):
    """Simulate what happens when __main__._persist_games writes a pair with
    source + engine_state and we re-read it — the fields must survive."""
    from talos import persistence

    monkeypatch.setattr(persistence, "_data_dir", tmp_path)

    pair = ArbPair(
        event_ticker="KXSURVIVORMENTION-26APR23",
        ticker_a="KXSURVIVORMENTION-26APR23-MRBE",
        ticker_b="KXSURVIVORMENTION-26APR23-MRBE",
        side_a="yes",
        side_b="no",
        source="tree",
        engine_state="winding_down",
    )
    # Simulate _persist_games' inner loop: build entry dict
    entry: dict[str, str | float | None] = {
        "event_ticker": pair.event_ticker,
        "ticker_a": pair.ticker_a,
        "ticker_b": pair.ticker_b,
        "side_a": pair.side_a,
        "side_b": pair.side_b,
        "fee_type": pair.fee_type,
        "fee_rate": pair.fee_rate,
    }
    # Phase 1 requirement: writer adds these
    if pair.source is not None:
        entry["source"] = pair.source
    entry["engine_state"] = pair.engine_state
    save_games_full([entry])

    reloaded = load_saved_games_full()
    assert reloaded is not None
    assert reloaded[0]["source"] == "tree"
    assert reloaded[0]["engine_state"] == "winding_down"


def test_persist_games_on_change_preserves_fields_flag_off(tmp_path: Path, monkeypatch):
    """Phase 3 dual-run proof: if a tree-mode session wrote a pair with
    source='tree' and engine_state='winding_down', a later legacy session
    that triggers _persist_games (via on_change) must preserve them.

    This test simulates the full legacy flow: load from games_full.json,
    reconstitute into ArbPair(s), re-persist using the updated _persist_games
    pattern. Result must be byte-identical on the new fields.
    """
    from talos import persistence

    monkeypatch.setattr(persistence, "_data_dir", tmp_path)

    # Seed the file as if a tree-mode session had just written it
    original: list[dict[str, str | float | None]] = [
        {
            "event_ticker": "KXFEDMENTION-26APR-YIEL",
            "ticker_a": "KXFEDMENTION-26APR-YIEL",
            "ticker_b": "KXFEDMENTION-26APR-YIEL",
            "side_a": "yes",
            "side_b": "no",
            "fee_type": "quadratic_with_maker_fees",
            "fee_rate": 0.0175,
            "close_time": "2026-04-30T14:00:00Z",
            "expected_expiration_time": "2026-04-29T14:00:00Z",
            "source": "tree",
            "engine_state": "winding_down",
        }
    ]
    save_games_full(original)

    # Simulate legacy session: load via load_saved_games_full, restore into
    # ArbPair(s) (which carry the new fields thanks to Task 1), and
    # re-persist via the updated _persist_games pattern.
    loaded = load_saved_games_full()
    assert loaded is not None
    # Reconstruct ArbPair explicitly instead of **-splatting a loosely typed
    # dict — pyright cannot narrow dict[str, str | float | None] into the
    # mix of str / float / str|None fields ArbPair requires.
    pairs: list[ArbPair] = []
    for r in loaded:
        close_time_val = r.get("close_time")
        exp_time_val = r.get("expected_expiration_time")
        source_val = r.get("source")
        pairs.append(
            ArbPair(
                event_ticker=str(r["event_ticker"]),
                ticker_a=str(r["ticker_a"]),
                ticker_b=str(r["ticker_b"]),
                side_a=str(r.get("side_a", "no")),
                side_b=str(r.get("side_b", "no")),
                fee_type=str(r.get("fee_type", "quadratic_with_maker_fees")),
                fee_rate=float(r.get("fee_rate", 0.0175) or 0.0175),
                close_time=str(close_time_val) if close_time_val is not None else None,
                expected_expiration_time=(str(exp_time_val) if exp_time_val is not None else None),
                source=str(source_val) if source_val is not None else None,
                engine_state=str(r.get("engine_state", "active")),
            )
        )

    entries: list[dict[str, str | float | None]] = []
    for p in pairs:
        entry: dict[str, str | float | None] = {
            "event_ticker": p.event_ticker,
            "ticker_a": p.ticker_a,
            "ticker_b": p.ticker_b,
            "side_a": p.side_a,
            "side_b": p.side_b,
            "fee_type": p.fee_type,
            "fee_rate": p.fee_rate,
        }
        if p.source is not None:
            entry["source"] = p.source
        entry["engine_state"] = p.engine_state
        entries.append(entry)
    save_games_full(entries)

    # Round-trip must preserve both fields
    reloaded = load_saved_games_full()
    assert reloaded is not None
    assert reloaded[0]["source"] == "tree"
    assert reloaded[0]["engine_state"] == "winding_down"
