"""Bounded provider Kubernetes reader, shared by worker and collector."""

import time
from datetime import UTC, datetime
from typing import Any, cast

from app.schemas.kubevirt_storage import ReadResult


class ProviderReader:
    """Only bounded GET/list methods, using the configured provider API identity."""

    def __init__(self, provider, limits, deadline):
        from kubernetes import client

        credentials = provider.get_credentials()
        config = client.Configuration()
        config.host = credentials["api_url"]
        config.api_key = {"authorization": "Bearer " + credentials["token"]}
        config.verify_ssl = credentials.get("verify_ssl", False)
        self._configure(provider.id, config, limits, deadline)

    @classmethod
    def from_kubeconfig(cls, provider_id, kubeconfig, limits, deadline):
        """Use the protected collector transport for CSI identity reads."""
        from kubernetes import client, config

        configuration = client.Configuration()
        config.load_kube_config_from_dict(
            kubeconfig, client_configuration=configuration
        )
        if not configuration.verify_ssl or not configuration.host.startswith(
            "https://"
        ):
            raise ValueError("Collector transport requires verified TLS")
        reader = cls.__new__(cls)
        reader._configure(provider_id, configuration, limits, deadline)
        return reader

    def _configure(self, provider_id, config, limits, deadline):
        from kubernetes import client

        self.api = client.ApiClient(config)
        self.provider_id = provider_id
        try:
            self.api.rest_client.pool_manager.connection_pool_kw["retries"] = 0
            self.core = client.CoreV1Api(self.api)
            self.custom = client.CustomObjectsApi(self.api)
            self.limits, self.deadline = limits, deadline
        except Exception:
            self.api.close()
            raise

    def _timeout(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("verification limit")
        return min(15, self.limits.command_timeout_seconds, remaining)

    def namespace(self, name):
        result = self.core.read_namespace(name, _request_timeout=self._timeout())
        return cast(dict[str, Any], self.api.sanitize_for_serialization(result))

    def cluster_identity(self):
        return self.namespace("kube-system")["metadata"]["uid"]

    def projects(self, namespace):
        result = cast(
            dict[str, Any],
            self.custom.list_namespaced_custom_object(
                "troshka.redhat.com",
                "v1alpha1",
                namespace,
                "troshkaprojects",
                limit=2,
                _request_timeout=self._timeout(),
            ),
        )
        if result.get("metadata", {}).get("continue"):
            raise ValueError("ambiguous project namespace")
        return result["items"]

    def close(self):
        self.api.close()

    def pending_root(self, target, operation_id, profile):
        from app.services.native_managed_deploy import bind
        from app.services.native_managed_power import helpers

        return bind(helpers().PowerClient(self.api), target, operation_id, profile)

    def storage_class(self, name) -> dict[str, Any]:
        from kubernetes import client

        result = client.StorageV1Api(self.api).read_storage_class(
            name, _request_timeout=self._timeout()
        )
        return cast(dict[str, Any], self.api.sanitize_for_serialization(result))

    def _identity_list(self, method, namespace, selector):
        if selector.get("matchExpressions") or not selector.get("matchLabels"):
            raise ValueError("unsupported CSI selector")
        result = cast(
            dict[str, Any],
            self.api.sanitize_for_serialization(
                method(
                    namespace,
                    label_selector=",".join(
                        f"{k}={v}" for k, v in sorted(selector["matchLabels"].items())
                    ),
                    limit=min(self.limits.max_objects, 1000),
                    _request_timeout=self._timeout(),
                )
            ),
        )
        if result.get("metadata", {}).get("continue"):
            raise ValueError("CSI inventory exceeds bound")
        return result["items"]

    def _identity_dependencies(self, read, namespace, workloads, pods):
        from kubernetes import client

        from app.services.native_initialization_observer import (
            priority_class_identity,
            service_account_identity,
        )

        dependencies = {}
        for role, workload in workloads.items():
            spec = workload["spec"]["template"]["spec"]
            selected = {}
            if "imagePullSecrets" not in spec and any(
                p["spec"].get("imagePullSecrets") for p in pods[role]
            ):
                name = spec.get("serviceAccountName", "default")
                selected["service_account"] = service_account_identity(
                    read(self.core.read_namespaced_service_account, name, namespace),
                    namespace,
                    name,
                )
            name = spec.get("priorityClassName")
            if name:
                selected["priority_class"] = priority_class_identity(
                    read(client.SchedulingV1Api(self.api).read_priority_class, name),
                    name,
                )
            if selected:
                dependencies[role] = selected
        return dependencies

    def initialization_identity(self, storage_class_name):
        from kubernetes import client

        started = datetime.now(UTC)
        try:
            sc = self.storage_class(storage_class_name)
            driver = sc["provisioner"]
            namespace = sc["parameters"]["clusterID"]
            if (
                namespace != "openshift-storage"
                or driver != namespace + ".rbd.csi.ceph.com"
            ):
                return ReadResult(status="unsupported", collected_at=started)
            apps, storage = client.AppsV1Api(self.api), client.StorageV1Api(self.api)

            def read(method, *args) -> dict[str, Any]:
                return cast(
                    dict[str, Any],
                    self.api.sanitize_for_serialization(
                        method(*args, _request_timeout=self._timeout())
                    ),
                )

            controller = read(
                apps.read_namespaced_deployment, driver + "-ctrlplugin", namespace
            )
            node = read(
                apps.read_namespaced_daemon_set, driver + "-nodeplugin", namespace
            )
            value = {
                "provider_id": self.provider_id,
                "cluster_key": self.cluster_identity(),
                "storage_class": sc,
                "namespace": self.namespace(namespace),
                "driver": read(storage.read_csi_driver, driver),
                "controller": controller,
                "node": node,
                "replica_sets": self._identity_list(
                    apps.list_namespaced_replica_set,
                    namespace,
                    controller["spec"]["selector"],
                ),
                "controller_pods": self._identity_list(
                    self.core.list_namespaced_pod,
                    namespace,
                    controller["spec"]["selector"],
                ),
                "node_pods": self._identity_list(
                    self.core.list_namespaced_pod, namespace, node["spec"]["selector"]
                ),
            }
            value["admission_dependencies"] = self._identity_dependencies(
                read,
                namespace,
                {"controller": controller, "node": node},
                {"controller": value["controller_pods"], "node": value["node_pods"]},
            )
            return ReadResult(status="complete", collected_at=started, value=value)
        except Exception:  # noqa: BLE001 -- sanitize transport errors
            return ReadResult(status="unavailable", collected_at=started)
