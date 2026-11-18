"""Bounded provider transport; all mutations use retained exact evidence."""

from urllib.parse import quote

from app.services import native_deployment_origin as origin
from app.services.native_origin_namespace_cleanup import InventoryClient


class OpsClient(InventoryClient):
    def resource(self, kind, namespace, name=None):
        routes = {
            "Pod": "pods",
            "Secret": "secrets",  # pragma: allowlist secret -- Kubernetes route
        }
        origin.require(kind in routes, "Native support resource unsupported")
        path = "/api/v1/namespaces/" + quote(namespace, safe="") + "/" + routes[kind]
        return path + "/" + quote(name, safe="") if name else path

    def get(self, kind, namespace, name):
        if kind == "Namespace":
            return super().get(kind, namespace, name)
        return self.path(self.resource(kind, namespace, name), absent=True)

    def post(self, kind, body):
        return self.path(
            self.resource(kind, body["metadata"]["namespace"]), method="POST", body=body
        )

    def delete(self, kind, namespace, name, uid, rv):
        return self.path(
            self.resource(kind, namespace, name),
            method="DELETE",
            absent=True,
            body={
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": uid, "resourceVersion": rv},
            },
        )

    def root(self, namespace, name):
        return self.path(self.root_path(namespace, name), absent=True)

    def root_path(self, namespace, name):
        return (
            "/apis/troshka.redhat.com/v1alpha1/namespaces/"
            + quote(namespace, safe="")
            + "/troshkaprojects/"
            + quote(name, safe="")
        )

    def request_file(self, namespace, name, uid, rv, annotations):
        patch = [
            dict(op="test", path="/metadata/uid", value=uid),
            dict(op="test", path="/metadata/resourceVersion", value=rv),
            dict(op="add", path="/metadata/annotations", value=annotations),
        ]
        return self.path(
            self.root_path(namespace, name),
            method="PATCH",
            body=patch,
            content_type="application/json-patch+json",
        )
