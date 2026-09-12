"""Disk-bounded git cache for the workloads subsystem.

- agnosticv: one working copy tracking a single branch (master), refreshed on use.
- agnosticd-v2 / workload repos: one bare treeless mirror per repo plus a git
  worktree per ref (shared object store), with LRU eviction under a disk budget.
"""

import os
import shutil
import subprocess

from app.core.config import config


def _cfg() -> dict:
    return config.get("workloads", {}) or {}


def cache_root() -> str:
    return _cfg().get("cache_root", "/var/lib/troshka/workload-cache")


def default_ref(repo_key: str) -> str:
    return (_cfg().get("default_refs", {}) or {}).get(repo_key, "main")


def _git(*args, cwd=None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _touch(path: str) -> None:
    os.utime(path, None)


def ensure_agnosticv(git_url: str) -> str:
    branch = _cfg().get("agnosticv_branch", "master")
    path = os.path.join(cache_root(), "agnosticv")
    if os.path.isdir(os.path.join(path, ".git")):
        _git("fetch", "-q", "origin", branch, cwd=path)
        _git("reset", "-q", "--hard", f"origin/{branch}", cwd=path)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _git("clone", "-q", "--branch", branch, "--single-branch", git_url, path)
    return path


def _mirror_dir(repo_key: str) -> str:
    return os.path.join(cache_root(), "mirrors", f"{repo_key}.git")


def _worktree_dir(repo_key: str, ref: str) -> str:
    return os.path.join(cache_root(), "worktrees", repo_key, ref)


def _ensure_mirror(repo_key: str, git_url: str) -> str:
    mirror = _mirror_dir(repo_key)
    if os.path.isdir(mirror):
        _git("fetch", "-q", "--filter=blob:none", "origin", cwd=mirror)
    else:
        os.makedirs(os.path.dirname(mirror), exist_ok=True)
        _git("clone", "-q", "--filter=blob:none", "--mirror", git_url, mirror)
    return mirror


def ensure_repo(repo_key: str, git_url: str, ref: str | None) -> str:
    ref = ref or default_ref(repo_key)
    mirror = _ensure_mirror(repo_key, git_url)
    worktree = _worktree_dir(repo_key, ref)
    if os.path.isdir(worktree):
        _git("fetch", "-q", "--filter=blob:none", "origin", cwd=mirror)
        _git("reset", "-q", "--hard", ref, cwd=worktree)
    else:
        os.makedirs(os.path.dirname(worktree), exist_ok=True)
        _git("worktree", "add", "-q", "--force", "--detach", worktree, ref, cwd=mirror)
    _touch(worktree)
    _evict_if_needed()
    return worktree


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = os.path.join(root, name)
            if not os.path.islink(fp):
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    continue
    return total


def _all_worktrees() -> list[str]:
    base = os.path.join(cache_root(), "worktrees")
    result = []
    if not os.path.isdir(base):
        return result
    for repo_key in os.listdir(base):
        repo_dir = os.path.join(base, repo_key)
        if not os.path.isdir(repo_dir):
            continue
        for ref in os.listdir(repo_dir):
            ref_path = os.path.join(repo_dir, ref)
            if os.path.isdir(ref_path):
                result.append(ref_path)
    return result


def _remove_worktree(worktree: str) -> None:
    repo_key = os.path.basename(os.path.dirname(worktree))
    mirror = _mirror_dir(repo_key)
    try:
        _git("worktree", "remove", "-q", "--force", worktree, cwd=mirror)
    except subprocess.CalledProcessError:
        shutil.rmtree(worktree, ignore_errors=True)
        if os.path.isdir(mirror):
            _git("worktree", "prune", cwd=mirror)


def _evict_if_needed() -> None:
    budget = int(_cfg().get("disk_budget_gb", 20)) * (1024**3)
    worktrees = _all_worktrees()
    total = sum(_dir_size(w) for w in worktrees)
    # Oldest first; keep evicting until under budget or only one remains.
    for worktree in sorted(worktrees, key=os.path.getmtime):
        if total <= budget or len(worktrees) <= 1:
            break
        total -= _dir_size(worktree)
        _remove_worktree(worktree)
        worktrees.remove(worktree)
