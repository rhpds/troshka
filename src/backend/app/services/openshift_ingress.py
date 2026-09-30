"""Resolve OpenShift apps domain from the configured IngressController.

When admin sets IngressController to ``ingress-rhdp-net``, routes must use that
controller's ``status.domain`` (e.g. ``apps.ocpv06.rhdp.net``) as ``spec.host``.
That controller also requires a ``guid`` label on the route's namespace
(``namespaceSelector: guid Exists``).
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from app.services.system_settings import (
    DEFAULT_INGRESS_CONTROLLER,
    get_ingress_controller_name,
)

logger = logging.getLogger(__name__)

_IC_GROUP = "operator.openshift.io"
_IC_VERSION = "v1"
_IC_NAMESPACE = "openshift-ingress-operator"
_IC_PLURAL = "ingresscontrollers"


def route_public_host(route_name: str, namespace: str, apps_domain: str) -> str:
    """OpenShift auto-host shape: ``<route-name>-<namespace>.<apps-domain>``."""
    return f"{route_name}-{namespace}.{apps_domain}"


def uses_non_default_ingress() -> bool:
    return get_ingress_controller_name() != DEFAULT_INGRESS_CONTROLLER


def _domain_from_ingress_controller(custom_api, ic_name: str) -> str:
    try:
        ic = custom_api.get_namespaced_custom_object(
            group=_IC_GROUP,
            version=_IC_VERSION,
            namespace=_IC_NAMESPACE,
            plural=_IC_PLURAL,
            name=ic_name,
        )
    except Exception as e:
        logger.warning(
            "Could not read IngressController %s/%s: %s",
            _IC_NAMESPACE,
            ic_name,
            e,
        )
        return ""
    if not isinstance(ic, dict):
        return ""
    status = ic.get("status")
    if isinstance(status, dict) and status.get("domain"):
        return str(status["domain"])
    return ""


def _domain_from_cluster_ingress(custom_api) -> str:
    try:
        ing = custom_api.get_cluster_custom_object(
            group="config.openshift.io",
            version="v1",
            plural="ingresses",
            name="cluster",
        )
    except Exception:
        return ""
    if not isinstance(ing, dict):
        return ""
    spec = ing.get("spec")
    if isinstance(spec, dict) and spec.get("domain"):
        return str(spec["domain"])
    return ""


def _domain_from_api_url(api_url: str) -> str:
    try:
        host = urlparse(api_url or "").hostname or ""
    except Exception:
        return ""
    if host.startswith("api."):
        return "apps." + host[len("api.") :]
    return ""


def resolve_apps_domain(custom_api, api_url: str = "") -> str:
    """Apps wildcard for the configured IngressController.

    Prefer the named IngressController's ``status.domain``. For ``default``,
    fall back to ``ingresses.config.openshift.io/cluster`` then ``api_url``.
    """
    ic_name = get_ingress_controller_name()
    domain = _domain_from_ingress_controller(custom_api, ic_name)
    if domain:
        return domain
    if ic_name != DEFAULT_INGRESS_CONTROLLER:
        # Non-default IC unreadable — do not silently use the default apps
        # domain (that would pin routes to the wrong router).
        logger.error(
            "IngressController %r domain unavailable; refusing default apps domain",
            ic_name,
        )
        return ""
    return _domain_from_cluster_ingress(custom_api) or _domain_from_api_url(api_url)


def _warn_guid_label_failed(namespace: str, value: str, err) -> None:
    logger.warning(
        "Failed to label namespace %s with guid=%s: %s", namespace, value, err
    )


def _create_namespace_with_guid(core_api, namespace: str, value: str) -> None:
    from kubernetes import client as k8s_client

    try:
        core_api.create_namespace(
            k8s_client.V1Namespace(
                metadata=k8s_client.V1ObjectMeta(
                    name=namespace,
                    labels={"guid": value},
                )
            )
        )
    except Exception as create_err:
        if "AlreadyExists" not in str(create_err):
            _warn_guid_label_failed(namespace, value, create_err)


def ensure_namespace_guid_label(core_api, namespace: str, guid: str) -> None:
    """Ensure ``guid`` exists on the namespace (required by ingress-rhdp-net).

    No-op when the configured IngressController is ``default``.
    """
    if not uses_non_default_ingress():
        return
    value = (guid or "").strip().lower()[:63]
    if not value:
        return

    try:
        ns = core_api.read_namespace(namespace)
        labels = dict(ns.metadata.labels or {})
        if labels.get("guid") == value:
            return
        labels["guid"] = value
        core_api.patch_namespace(
            namespace,
            {"metadata": {"labels": labels}},
        )
    except Exception as e:
        # AlreadyExists / NotFound handled by caller; patch failures log only.
        if "NotFound" in str(e) or getattr(e, "status", None) == 404:
            _create_namespace_with_guid(core_api, namespace, value)
            return
        _warn_guid_label_failed(namespace, value, e)
