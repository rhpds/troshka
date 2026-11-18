"""Bounded normal Kubernetes operations for the namespace cleanup adapter."""

import importlib
from typing import TYPE_CHECKING
from urllib.parse import quote

from app.services import native_deployment_origin as origin
from app.services.native_origin_namespace_cleanup import InventoryClient, inventory_rows

ANNOTATION = "troshka.redhat.com/namespace-cleanup"
ACK = "troshka.redhat.com/namespace-cleanup-ack"
OWN_KINDS = ("TroshkaProject", "TroshkaVM", "TroshkaNetwork", "TroshkaCeph")


class NamespaceClient(InventoryClient):
    def new_observation(self):
        """Independent bounded read phase; never renew this client's budget."""
        return NamespaceClient(self.client)

    def resource(self, kind):
        from app.services.native_managed_power import helpers

        version, plural = (helpers().KINDS | helpers().CLEANUP_KINDS)[kind]
        prefix = "/api/" if "/" not in version else "/apis/"
        return dict(path=prefix + version, resource=plural, kind=kind, version=version)

    def rows(self, kind, namespace):
        return inventory_rows(self, namespace, [self.resource(kind)])

    def get(self, kind, namespace, name):
        resource = self.resource(kind)
        scope = "/namespaces/" + quote(namespace, safe="") if namespace else ""
        return self.path(
            resource["path"]
            + scope
            + "/"
            + resource["resource"]
            + "/"
            + quote(name, safe=""),
            absent=True,
        )

    def delete(self, kind, namespace, name, uid, rv, *, orphan=False):
        resource = self.resource(kind)
        path = resource["path"] + "/namespaces/" + quote(namespace, safe="")
        body = dict(
            apiVersion="v1",
            kind="DeleteOptions",
            preconditions=dict(uid=uid, resourceVersion=rv),
        )
        if orphan:
            body["propagationPolicy"] = "Orphan"
        return self.path(
            path + "/" + resource["resource"] + "/" + quote(name, safe=""),
            method="DELETE",
            body=body,
            absent=True,
        )

    def patch_namespace(self, namespace, uid, rv, annotations):
        return self.path(
            "/api/v1/namespaces/" + quote(namespace, safe=""),
            method="PATCH",
            content_type="application/json-patch+json",
            body=[
                dict(op="test", path="/metadata/uid", value=uid),
                dict(op="test", path="/metadata/resourceVersion", value=rv),
                dict(op="add", path="/metadata/annotations", value=annotations),
            ],
        )

    def operator_profile(self, namespace, cluster_uid):
        deployment = self.get("Deployment", namespace, "troshka-operator")
        origin.require(deployment is not None, "Operator deployment unavailable")
        if TYPE_CHECKING:
            assert deployment is not None
        selector = deployment["spec"]["selector"].get("matchLabels", {})
        pods = [
            p
            for p in self.rows("Pod", namespace)
            if all(
                p["metadata"].get("labels", {}).get(k) == v for k, v in selector.items()
            )
            and p.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
        ]
        origin.require(len(pods) == 1, "Operator singleton unavailable")
        pod = pods[0]
        containers = pod.get("status", {}).get("containerStatuses", [])
        origin.require(len(containers) == 1, "Operator container identity unavailable")
        identity = dict(
            namespace=namespace,
            name=pod["metadata"]["name"],
            uid=pod["metadata"]["uid"],
            containerId=containers[0].get("containerID"),
        )
        pins = dict(
            version="single-operator-recreate-v1",
            clusterUid=cluster_uid,
            deploymentName=deployment["metadata"]["name"],
            deploymentUid=deployment["metadata"]["uid"],
            imageId=containers[0].get("imageID"),
        )
        self.qualify_operator(pins, identity)
        return pins, identity

    def qualify_operator(self, pins, identity):
        from app.services.native_managed_power import helpers

        helpers()
        importlib.import_module("helpers.native_origin_operator").qualify(
            self, pins, identity
        )
