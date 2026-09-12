# src/backend/tests/test_workload_repo_cache.py
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _run(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _make_remote(tmp_path, name, branches):
    """Create a bare 'remote' repo with the given branches, each holding a
    marker file whose content is the branch name."""
    work = tmp_path / f"{name}-work"
    bare = tmp_path / f"{name}.git"
    _run("git", "init", "-q", str(work))
    _run("git", "-C", str(work), "config", "user.email", "t@t")
    _run("git", "-C", str(work), "config", "user.name", "t")
    _run("git", "-C", str(work), "checkout", "-q", "-b", branches[0])
    for i, br in enumerate(branches):
        if i > 0:
            _run("git", "-C", str(work), "checkout", "-q", "-b", br)
        (work / "marker.txt").write_text(br)
        _run("git", "-C", str(work), "add", "marker.txt")
        _run("git", "-C", str(work), "commit", "-q", "-m", f"on {br}")
    _run("git", "clone", "-q", "--bare", str(work), str(bare))
    return f"file://{bare}"


def _configure_cache(monkeypatch, tmp_path):
    from app.services.workloads import repo_cache

    root = str(tmp_path / "cache")
    monkeypatch.setattr(repo_cache, "cache_root", lambda: root)
    monkeypatch.setattr(
        repo_cache,
        "_cfg",
        lambda: {
            "disk_budget_gb": 100,
            "agnosticv_branch": "master",
            "default_refs": {"demo_workloads": "main"},
        },
    )
    return repo_cache, root


def test_default_ref_from_config(monkeypatch, tmp_path):
    repo_cache, _ = _configure_cache(monkeypatch, tmp_path)
    assert repo_cache.default_ref("demo_workloads") == "main"


def test_ensure_repo_checks_out_requested_ref(monkeypatch, tmp_path):
    repo_cache, _ = _configure_cache(monkeypatch, tmp_path)
    url = _make_remote(tmp_path, "wl", ["main", "development"])
    wt = repo_cache.ensure_repo("wl", url, "development")
    assert (open(os.path.join(wt, "marker.txt")).read()) == "development"


def test_ensure_repo_shares_object_store_across_refs(monkeypatch, tmp_path):
    repo_cache, root = _configure_cache(monkeypatch, tmp_path)
    url = _make_remote(tmp_path, "wl", ["main", "development"])
    repo_cache.ensure_repo("wl", url, "main")
    repo_cache.ensure_repo("wl", url, "development")
    mirrors = os.listdir(os.path.join(root, "mirrors"))
    assert mirrors == ["wl.git"]  # one shared mirror, two worktrees


def test_ensure_repo_none_uses_default_ref(monkeypatch, tmp_path):
    repo_cache, _ = _configure_cache(monkeypatch, tmp_path)
    url = _make_remote(tmp_path, "demo_workloads", ["main"])
    wt = repo_cache.ensure_repo("demo_workloads", url, None)
    assert (open(os.path.join(wt, "marker.txt")).read()) == "main"


def test_ensure_agnosticv_tracks_master(monkeypatch, tmp_path):
    repo_cache, _ = _configure_cache(monkeypatch, tmp_path)
    url = _make_remote(tmp_path, "agnosticv", ["master"])
    path = repo_cache.ensure_agnosticv(url)
    assert (open(os.path.join(path, "marker.txt")).read()) == "master"


def test_lru_eviction_under_budget(monkeypatch, tmp_path):
    from app.services.workloads import repo_cache

    root = str(tmp_path / "cache")
    monkeypatch.setattr(repo_cache, "cache_root", lambda: root)
    # Tiny budget forces eviction after the first worktree.
    monkeypatch.setattr(
        repo_cache,
        "_cfg",
        lambda: {"disk_budget_gb": 0, "agnosticv_branch": "master", "default_refs": {}},
    )
    url = _make_remote(tmp_path, "wl", ["main", "development"])
    repo_cache.ensure_repo("wl", url, "main")
    repo_cache.ensure_repo("wl", url, "development")
    worktrees = os.path.join(root, "worktrees", "wl")
    # With a zero budget, only the most-recent worktree survives eviction.
    assert os.listdir(worktrees) == ["development"]


def test_eviction_ignores_stray_files(monkeypatch, tmp_path):
    """Eviction should skip non-directory entries (e.g. .DS_Store on macOS)."""
    repo_cache, root = _configure_cache(monkeypatch, tmp_path)
    url = _make_remote(tmp_path, "wl", ["main"])
    # Create a worktree, which creates the worktrees/ and worktrees/wl/ dirs.
    repo_cache.ensure_repo("wl", url, "main")
    # Plant stray files: one in worktrees/ base, one in worktrees/wl/.
    (tmp_path / "cache" / "worktrees" / ".DS_Store").write_text("stray base")
    (tmp_path / "cache" / "worktrees" / "wl" / ".DS_Store").write_text("stray repo")
    # Eviction runs on every ensure_repo() call; should not crash.
    wt = repo_cache.ensure_repo("wl", url, "main")
    assert (open(os.path.join(wt, "marker.txt")).read()) == "main"
