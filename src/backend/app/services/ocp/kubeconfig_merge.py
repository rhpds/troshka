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

_KUBEADMIN_PW_RE = re.compile(r"^[A-Za-z0-9]+(-[A-Za-z0-9]+){3,}$")


def is_valid_kubeadmin_password(raw: str | None) -> bool:
    """True for an openshift-install kubeadmin password (not shell error noise)."""
    if not raw:
        return False
    text = raw.strip()
    if text.startswith("cat:"):
        return False
    return bool(_KUBEADMIN_PW_RE.match(text))


def is_valid_kubeconfig(raw: str | bytes) -> bool:
    """True when ``raw`` parses as a kubeconfig with at least one context."""
    if not raw:
        return False
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw
    if not text.strip():
        return False
    # troshkad/kubevirt cat failures land in stdout as "cat: /path: No such file..."
    if text.lstrip().startswith("cat:"):
        return False
    try:
        cfg = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    if not isinstance(cfg, dict):
        return False
    if cfg.get("kind") != "Config" and not cfg.get("clusters"):
        return False
    return bool(cfg.get("contexts"))


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


def _console_url(cluster_name: str, base_domain: str) -> str:
    if not cluster_name or not base_domain:
        return ""
    return f"https://console-openshift-console.apps.{cluster_name}.{base_domain}"


def _cluster_meta_by_context(
    clusters: list[dict] | None,
    creds: dict[str, tuple[str, str]] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Map sanitized context name -> kubeadmin password / base domain."""
    kubeadmin: dict[str, str] = {}
    base_domain: dict[str, str] = {}
    if not clusters:
        return kubeadmin, base_domain
    for cluster in clusters:
        display = str(cluster.get("name") or cluster.get("id") or "").strip()
        if not display:
            continue
        ctx = _sanitize_context_name(display)
        key = str(cluster.get("id") or display).strip()
        if creds:
            pw, _kc = creds.get(key, creds.get(display, ("", "")))
            if pw:
                kubeadmin[ctx] = str(pw)
        bd = str(cluster.get("baseDomain") or "").strip()
        if bd:
            base_domain[ctx] = bd
    return kubeadmin, base_domain


def cluster_terminal_motd_text(
    merged_yaml: str,
    *,
    clusters: list[dict] | None = None,
    creds: dict[str, tuple[str, str]] | None = None,
) -> str:
    """Banner for the showroom cluster terminal with per-context access info."""
    if not merged_yaml.strip():
        return ""
    try:
        cfg = yaml.safe_load(merged_yaml)
    except yaml.YAMLError:
        return ""
    if not isinstance(cfg, dict):
        return ""
    contexts = cfg.get("contexts") or []
    if not contexts:
        return ""
    current = str(cfg.get("current-context") or "").strip()
    clusters_by_name = {
        str(c.get("name") or ""): c for c in (clusters or []) if c.get("name")
    }
    kubeadmin_by_ctx, base_domain_by_ctx = _cluster_meta_by_context(clusters, creds)
    if creds is None and clusters is None:
        kubeadmin_by_ctx, base_domain_by_ctx = {}, {}

    lines = ["", "OpenShift cluster terminal", ""]
    if len(contexts) > 1:
        lines.extend(
            [
                "  oc config use-context <name>   # switch cluster",
                "  oc config get-contexts         # list contexts",
                "",
            ]
        )

    cluster_entries = {c.get("name"): c for c in (cfg.get("clusters") or [])}
    for ctx in contexts:
        name = str(ctx.get("name") or "").strip()
        if not name:
            continue
        cluster_body = (cluster_entries.get(name) or {}).get("cluster") or {}
        server = str(cluster_body.get("server") or "").strip()
        sni = str(cluster_body.get("tls-server-name") or "").strip()
        marker = "* " if name == current else "  "
        suffix = "  (current)" if name == current else ""
        lines.append(f"{marker}{name}{suffix}")
        if server:
            api_line = f"    API:       {server}"
            if sni:
                api_line += f"  (SNI: {sni})"
            lines.append(api_line)
        cluster_row = clusters_by_name.get(name) or {}
        display_name = str(cluster_row.get("name") or name)
        bd = base_domain_by_ctx.get(name, "")
        console = _console_url(display_name, bd)
        if console:
            lines.append(f"    Console:   {console}")
        pw = kubeadmin_by_ctx.get(name, "")
        if pw:
            lines.append(f"    Kubeadmin: {pw}")
        lines.append("")

    lines.append("")
    return "\n".join(lines)
