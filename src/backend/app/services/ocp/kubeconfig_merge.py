"""Merge per-cluster kubeconfigs into a single kubeconfig for the showroom oc
terminal.

Each deployed OCP cluster harvests its own kubeconfig (see
``deploy_service._store_ops_pod_creds``). The bastionless "cluster terminal"
container serves one merged kubeconfig where each cluster is a context named
after the cluster's display name, so ``oc config use-context <name>`` switches
between them. With a single cluster its context is the current-context, so the
terminal works with no setup.
"""

from __future__ import annotations

import re

import yaml


def _sanitize_context_name(name: str) -> str:
    """A shell/oc-friendly context name: lowercase, non-alnum runs -> single '-'."""
    slug = re.sub(r"[^a-z0-9._-]+", "-", (name or "").strip().lower())
    return slug.strip("-") or "cluster"


def _pick_context(cfg: dict) -> dict | None:
    """The kubeconfig's current-context entry, else the first context."""
    contexts = cfg.get("contexts") or []
    if not contexts:
        return None
    cur = cfg.get("current-context")
    return next((c for c in contexts if c.get("name") == cur), None) or contexts[0]


def merge_kubeconfigs(named_configs: list[tuple[str, str]]) -> str:
    """Merge ``[(display_name, kubeconfig_yaml), ...]`` into one kubeconfig.

    Each source's active context is re-emitted as a context/cluster/user trio all
    named after ``display_name`` (sanitized). current-context is the first
    successfully merged cluster. Empty/malformed inputs are skipped. Always
    returns a valid (possibly empty) kubeconfig YAML document.
    """
    merged: dict = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [],
        "contexts": [],
        "users": [],
        "current-context": "",
    }
    seen: set[str] = set()
    for display, raw in named_configs:
        if not raw:
            continue
        try:
            cfg = yaml.safe_load(raw)
        except yaml.YAMLError:
            continue
        if not isinstance(cfg, dict):
            continue
        ctx = _pick_context(cfg)
        if not ctx:
            continue
        body = ctx.get("context", {}) or {}
        cluster_entry = next(
            (
                c
                for c in (cfg.get("clusters") or [])
                if c.get("name") == body.get("cluster")
            ),
            None,
        )
        user_entry = next(
            (u for u in (cfg.get("users") or []) if u.get("name") == body.get("user")),
            None,
        )
        if not cluster_entry or not user_entry:
            continue
        name = _sanitize_context_name(display)
        if name in seen:  # keep names unique if two clusters slug to the same
            i = 2
            while f"{name}-{i}" in seen:
                i += 1
            name = f"{name}-{i}"
        seen.add(name)
        merged["clusters"].append(
            {"name": name, "cluster": cluster_entry.get("cluster", {})}
        )
        merged["users"].append({"name": name, "user": user_entry.get("user", {})})
        new_ctx: dict = {"name": name, "context": {"cluster": name, "user": name}}
        if body.get("namespace"):
            new_ctx["context"]["namespace"] = body["namespace"]
        merged["contexts"].append(new_ctx)
        if not merged["current-context"]:
            merged["current-context"] = name
    return yaml.safe_dump(merged, default_flow_style=False, sort_keys=False)
