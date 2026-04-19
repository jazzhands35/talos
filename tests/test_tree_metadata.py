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


def test_set_deliberately_unticked_save_failure_rolls_back_in_memory(
    tmp_path: Path,
):
    """Round-7 plan Fix #4: when _touch() raises PersistenceError,
    in-memory state must be rolled back so memory ↔ disk consistent."""
    import pytest

    from talos.persistence_errors import PersistenceError

    blocker = tmp_path / "blocked"
    blocker.write_text("blocking file")
    bad_path = blocker / "child" / "tree_metadata.json"

    store = TreeMetadataStore(path=bad_path, autosave=True)
    store._loaded = True
    store._data = {"deliberately_unticked": []}

    with pytest.raises(PersistenceError):
        store.set_deliberately_unticked("K")

    # In-memory list must NOT contain "K" (rolled back).
    assert not store.is_deliberately_unticked("K")


def test_clear_deliberately_unticked_save_failure_rolls_back_in_memory(
    tmp_path: Path,
):
    import pytest

    from talos.persistence_errors import PersistenceError

    # Successfully populate first.
    good_path = tmp_path / "tree_metadata.json"
    store = TreeMetadataStore(path=good_path, autosave=True)
    store.load()
    store.set_deliberately_unticked("K")
    assert store.is_deliberately_unticked("K")

    # Now point at an unwriteable path; clear should fail and rollback.
    blocker = tmp_path / "blocked"
    blocker.write_text("blocking")
    store._path = blocker / "child" / "tree_metadata.json"
    with pytest.raises(PersistenceError):
        store.clear_deliberately_unticked("K")
    # In-memory list must STILL contain "K" (rolled back the removal).
    assert store.is_deliberately_unticked("K")


def test_promote_pending_to_applied_save_failure_rolls_back_in_memory(
    tmp_path: Path,
):
    """Round-7 plan Fix #3: atomic promote with rollback. On save
    failure, both pending->no-op and applied->no-op must restore."""
    import pytest

    from talos.persistence_errors import PersistenceError

    good_path = tmp_path / "tree_metadata.json"
    store = TreeMetadataStore(path=good_path, autosave=True)
    store.load()
    store.set_deliberately_unticked_pending("K")
    assert store.is_deliberately_unticked_pending("K")
    assert not store.is_deliberately_unticked("K")

    # Force the next save to fail.
    blocker = tmp_path / "blocked"
    blocker.write_text("blocking")
    store._path = blocker / "child" / "tree_metadata.json"

    with pytest.raises(PersistenceError):
        store.promote_pending_to_applied("K")

    # Both states must reflect the prior snapshot exactly:
    # pending still contains K, applied does not.
    assert store.is_deliberately_unticked_pending("K")
    assert not store.is_deliberately_unticked("K")
