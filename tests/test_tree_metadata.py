from datetime import UTC, datetime
from pathlib import Path

from talos.tree_metadata import TreeMetadataStore


def test_empty_store(tmp_path: Path):
    store = TreeMetadataStore(path=tmp_path / "tree_metadata.json")
    store.load()
    assert store.manual_event_start("KX-ANYTHING") is None
    assert not store.is_deliberately_unticked("KX-ANYTHING")
    assert not store.is_deliberately_unticked_pending("KX-ANYTHING")


def test_set_and_read_manual_event_start(tmp_path: Path):
    store = TreeMetadataStore(path=tmp_path / "tree_metadata.json")
    store.load()
    dt = datetime(2026, 4, 22, 20, 0, tzinfo=UTC)
    store.set_manual_event_start("KX-FOO", dt.isoformat())
    assert store.manual_event_start("KX-FOO") == dt


def test_manual_event_start_none_value_returns_opt_out(tmp_path: Path):
    store = TreeMetadataStore(path=tmp_path / "tree_metadata.json")
    store.load()
    store.set_manual_event_start("KX-FOO", "none")
    assert store.manual_event_start("KX-FOO") == "none"


def test_first_seen_and_reviewed(tmp_path: Path):
    store = TreeMetadataStore(path=tmp_path / "tree_metadata.json")
    store.load()
    assert not store.is_new("KX-NEW")  # never seen → not new yet
    store.mark_first_seen("KX-NEW")
    assert store.is_new("KX-NEW")  # seen but not reviewed → new
    store.mark_reviewed("KX-NEW")
    assert not store.is_new("KX-NEW")


def test_deliberately_unticked_lifecycle(tmp_path: Path):
    store = TreeMetadataStore(path=tmp_path / "tree_metadata.json")
    store.load()
    store.set_deliberately_unticked("KX-EVT")
    assert store.is_deliberately_unticked("KX-EVT")
    store.clear_deliberately_unticked("KX-EVT")
    assert not store.is_deliberately_unticked("KX-EVT")


def test_deferred_unticked_lifecycle(tmp_path: Path):
    store = TreeMetadataStore(path=tmp_path / "tree_metadata.json")
    store.load()
    store.set_deliberately_unticked_pending("KX-EVT")
    assert store.is_deliberately_unticked_pending("KX-EVT")
    # Promotion: when event fully removed, pending → applied
    store.promote_pending_to_applied("KX-EVT")
    assert not store.is_deliberately_unticked_pending("KX-EVT")
    assert store.is_deliberately_unticked("KX-EVT")


def test_persistence_roundtrip(tmp_path: Path):
    path = tmp_path / "tree_metadata.json"
    s1 = TreeMetadataStore(path=path)
    s1.load()
    s1.set_manual_event_start("KX-A", "2026-04-22T20:00:00-04:00")
    s1.mark_first_seen("KX-A")
    s1.set_deliberately_unticked_pending("KX-A")
    s1.save()

    s2 = TreeMetadataStore(path=path)
    s2.load()
    assert s2.manual_event_start("KX-A") == datetime.fromisoformat("2026-04-22T20:00:00-04:00")
    assert s2.is_deliberately_unticked_pending("KX-A")


def test_save_on_every_mutation_when_autosave(tmp_path: Path):
    path = tmp_path / "tree_metadata.json"
    s1 = TreeMetadataStore(path=path, autosave=True)
    s1.load()
    s1.set_manual_event_start("KX-A", "2026-04-22T20:00:00Z")
    # File should already be written
    s2 = TreeMetadataStore(path=path)
    s2.load()
    assert s2.manual_event_start("KX-A") is not None


def test_save_raises_persistence_error_on_write_failure(tmp_path: Path):
    """Round 6: TreeMetadataStore.save() must NOT swallow write failures.
    The store holds safety-relevant state (manual_event_start overrides
    used by the resolver cascade); a silent failure would let the UI
    proceed as if the override was recorded while a restart loses it."""
    import pytest

    from talos.persistence_errors import PersistenceError

    # Point save at an unwriteable path: parent is a regular file, so
    # mkdir(parents=True, exist_ok=True) inside _atomic_write_text fails.
    blocker = tmp_path / "blocked"
    blocker.write_text("blocking file")
    bad_path = blocker / "child" / "tree_metadata.json"

    store = TreeMetadataStore(path=bad_path, autosave=False)
    store._loaded = True  # bypass load() which would also fail
    store._data = {"manual_event_start": {"KX-A": "2026-04-22T20:00:00Z"}}
    with pytest.raises(PersistenceError):
        store.save()
