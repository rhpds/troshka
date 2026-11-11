"""Real filesystem allocation: sparse length never masquerades as used blocks."""
import os
from uuid import uuid4

import pytest

import troshkad


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "troshka"
    root.mkdir()
    (root / "vms").mkdir()
    project = str(uuid4())
    directory = root / "vms" / project
    directory.mkdir()
    monkeypatch.setattr(troshkad, "_TROSHKA_DIR", str(root))
    monkeypatch.setattr(troshkad, "_SHARED_DIR", str(root / "shared"))
    monkeypatch.setattr(troshkad, "_config", {"storage_mode": "local"})
    return root, project, directory


def test_sparse_and_retained_files_count_allocated_blocks_and_base_once(store):
    root, project, directory = store
    sparse = directory / "disk.qcow2"
    with sparse.open("wb") as file:
        file.write(b"x" * 4096)
        file.truncate(100 * 2**30)
    retained = directory / "retained-overlay"
    retained.write_bytes(b"y" * 8192)
    images = root / "images"
    images.mkdir()
    base = images / "shared.qcow2"
    base.write_bytes(b"z" * 16384)
    observation = troshkad._quota_storage_inventory(project, "generation-1")
    actual = sum(path.stat().st_blocks * 512 for path in (sparse, retained))
    assert sum(item["allocated_bytes"] for item in observation["files"]) == actual
    assert actual < sparse.stat().st_size
    assert observation["pools"][0]["platform_allocated_bytes"] == base.stat().st_blocks * 512
    assert observation["complete"] is True
    # A second project observes the same pool identity, never a copied base allocation.
    other = troshkad._quota_storage_inventory(str(uuid4()), "generation-2")
    assert other["files"] == []
    assert other["pools"][0]["id"] == observation["pools"][0]["id"]


@pytest.mark.parametrize("alias", ["symlink", "hardlink", "directory", "fifo"])
def test_ambiguous_paths_are_not_zero_usage(store, alias):
    root, project, directory = store
    target = root / "outside"
    target.write_bytes(b"x")
    path = directory / "hidden"
    if alias == "symlink":
        path.symlink_to(target)
    elif alias == "hardlink":
        os.link(target, path)
    elif alias == "directory":
        path.symlink_to(root, target_is_directory=True)
    else:
        os.mkfifo(path)
    with pytest.raises((ValueError, OSError)):
        troshkad._quota_storage_inventory(project, "generation")


def test_missing_root_and_shared_mount_fail_closed(store):
    root, project, directory = store
    directory.rmdir()
    (root / "vms").rmdir()
    with pytest.raises(FileNotFoundError):
        troshkad._quota_storage_inventory(project, "generation")
    (root / "vms").mkdir()
    (root / "shared" / "vms").mkdir(parents=True)
    with pytest.raises(ValueError, match="cluster-wide"):
        troshkad._quota_storage_inventory(project, "generation")


def test_invalid_project_and_expired_walk_budget(store, monkeypatch):
    _, project, directory = store
    with pytest.raises(ValueError):
        troshkad._quota_storage_inventory("../escape", "generation")
    (directory / "file").write_bytes(b"x")
    times = iter([0, 5, 5, 5])
    monkeypatch.setattr(troshkad.time, "monotonic", lambda: next(times))
    with pytest.raises(ValueError, match="bound"):
        troshkad._quota_storage_inventory(project, "generation")
