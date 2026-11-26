"""Read-only resource inspection with exact project identity verification."""

from contextlib import contextmanager
from typing import Any, cast

from fastapi import HTTPException
from kubernetes.client import V1Namespace

from app.models.native_deployment_origin import (
    NativeDeploymentNamespaceReceipt,
    NativeDeploymentOriginActive,
)
from app.models.provider import Provider
from app.services.assistant.projection import failure_categories, identifier


@contextmanager
def project_clients(db, project, host):
    from app.services.providers.kubevirt import (
        CRD_GROUP,
        CRD_VERSION,
        _get_k8s_clients,
        _project_ns,
    )

    provider = db.get(Provider, host.provider_id)
    if not provider:
        raise HTTPException(503, "Provider unavailable")
    active = db.get(NativeDeploymentOriginActive, project.id)
    receipt = None
    if active:
        receipt = (
            db.query(NativeDeploymentNamespaceReceipt)
            .filter_by(intent_id=active.intent_id)
            .first()
        )
    expected_uid = (receipt.payload or {}).get("namespace_uid") if receipt else None
    ns = _project_ns(provider, project.id)
    db.commit()  # Never retain a database transaction over remote I/O.
    custom, core, extra_client = _get_k8s_clients(provider)
    try:
        namespace = cast(V1Namespace, core.read_namespace(ns, _request_timeout=5))
        if expected_uid and (
            not namespace.metadata or namespace.metadata.uid != expected_uid
        ):
            raise HTTPException(403, "Namespace identity changed")
        cr = cast(
            dict[str, Any],
            custom.get_namespaced_custom_object(
                CRD_GROUP,
                CRD_VERSION,
                ns,
                "troshkaprojects",
                f"project-{project.id[:8]}",
                _request_timeout=5,
            ),
        )
        if cr.get("spec", {}).get("projectId") != project.id:
            raise HTTPException(403, "Project resource identity mismatch")
        yield custom, core, ns, provider, cr
    finally:
        custom.api_client.close()
        extra_client.close()


def vm_state(db, project, host, vm_id):
    with project_clients(db, project, host) as (custom, _, ns, _, cr):
        obj = cast(
            dict[str, Any],
            custom.get_namespaced_custom_object(
                "kubevirt.io",
                "v1",
                ns,
                "virtualmachines",
                f"troshka-vm-{vm_id[:8]}",
                _request_timeout=5,
            ),
        )
        # Verify association through the TroshkaVM controller, not just name prefix.
        refs = obj.get("metadata", {}).get("ownerReferences", [])
        owner = next((r for r in refs if r.get("kind") == "TroshkaVM"), None)
        if not owner:
            raise HTTPException(403, "VM ownership unavailable")
        from app.services.providers.kubevirt import CRD_GROUP, CRD_VERSION

        parent = cast(
            dict[str, Any],
            custom.get_namespaced_custom_object(
                CRD_GROUP,
                CRD_VERSION,
                ns,
                "troshkavms",
                owner["name"],
                _request_timeout=5,
            ),
        )
        parents = parent.get("metadata", {}).get("ownerReferences", [])
        if parent.get("metadata", {}).get("uid") != owner.get("uid") or not any(
            r.get("uid") == cr["metadata"]["uid"] for r in parents
        ):
            raise HTTPException(403, "VM ownership mismatch")
        status = obj.get("status", {})
        return {
            "ready": status.get("ready") is True,
            "printable_status": identifier(status.get("printableStatus", "unknown")),
            "conditions": [
                {
                    "type": identifier(c.get("type")),
                    "status": identifier(c.get("status")),
                    "failures": failure_categories(c.get("message")),
                }
                for c in status.get("conditions", [])[:10]
            ],
        }
