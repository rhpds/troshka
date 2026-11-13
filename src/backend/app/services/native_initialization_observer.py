"""Exact ODF RBD installation observation, never physical-bound acceptance.

This is deliberately one Kubernetes installation shape and fixed public reads.
No CSI vendor-version mapping, pool-free estimate or runtime authority lives here.
"""

import re
from copy import deepcopy
from datetime import UTC, datetime

from app.schemas.kubevirt_storage import (
    CsiInstallationIdentity,
    CsiWorkloadIdentity,
    InitializationObservation,
    InitializationTarget,
    StorageClassOptions,
)
from app.schemas.quota import AccountingUnavailable
from app.services.kubevirt_storage_enrollment import digest

OBSERVER_VERSION = "odf-rbd-installation-v1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def owner(value, kind, uid):
    refs = [
        r for r in value["metadata"].get("ownerReferences", []) if r.get("controller")
    ]
    return len(refs) == 1 and refs[0]["kind"] == kind and refs[0]["uid"] == uid


def image_ids(pod):
    require(not pod["metadata"].get("deletionTimestamp"), "terminating CSI pod")
    status = pod["status"]
    require(status.get("phase") == "Running", "CSI pod not running")
    require(
        any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in status.get("conditions", [])
        ),
        "CSI pod not ready",
    )
    specs = pod["spec"]["containers"]
    rows = status.get("containerStatuses", [])
    require(
        len(specs) == len(rows) and len({r["name"] for r in rows}) == len(rows),
        "incomplete CSI containers",
    )
    require(
        {c["name"] for c in specs} == {c["name"] for c in rows},
        "CSI containers changed",
    )
    result = {}
    for row in rows:
        match = re.fullmatch(
            r"(?:[^\s]+@)?(sha256:[a-f0-9]{64})", row.get("imageID", "")
        )
        require(
            row.get("ready") is True
            and bool(row.get("state", {}).get("running"))
            and match is not None,
            "CSI immutable running image unavailable",
        )
        if match is None:
            raise ValueError("CSI image digest missing")
        result[row["name"]] = match[1]
    require("csi-rbdplugin" in result, "unsupported CSI container layout")
    return result


def ready_count(workload, kind):
    require(
        not workload["metadata"].get("deletionTimestamp"), "terminating CSI workload"
    )
    status, spec = workload["status"], workload["spec"]
    require(
        status.get("observedGeneration") == workload["metadata"]["generation"],
        "CSI generation not observed",
    )
    if kind == "Deployment":
        count = spec.get("replicas", 1)
        fields = ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")
        require(not spec.get("paused"), "paused CSI deployment")
    else:
        count = status["desiredNumberScheduled"]
        fields = (
            "currentNumberScheduled",
            "updatedNumberScheduled",
            "numberReady",
            "numberAvailable",
        )
        require(status.get("numberMisscheduled", 0) == 0, "CSI nodes misscheduled")
    require(
        type(count) is int
        and count > 0
        and all(status.get(k) == count for k in fields),
        "CSI rollout incomplete",
    )
    return count


def pod_spec(value):
    """Only static public API defaults; every other spec field remains exact."""
    spec = deepcopy(value)
    require(
        not spec.get("initContainers") and not spec.get("ephemeralContainers"),
        "CSI init/ephemeral containers unsupported",
    )
    defaults = {
        "restartPolicy": "Always",
        "dnsPolicy": "ClusterFirst",
        "schedulerName": "default-scheduler",
        "terminationGracePeriodSeconds": 30,
        "enableServiceLinks": True,
        "serviceAccountName": "default",
        "hostNetwork": False,
        "hostPID": False,
        "hostIPC": False,
        "securityContext": {},
        "volumes": [],
        "tolerations": [],
        "imagePullSecrets": [],
        "affinity": {},
        "priority": 0,
        "preemptionPolicy": "PreemptLowerPriority",
    }
    for key, default in defaults.items():
        spec.setdefault(key, default)
    alias = spec.pop("serviceAccount", spec["serviceAccountName"])
    require(alias == spec["serviceAccountName"], "CSI service account alias differs")
    spec["containers"] = [container_spec(c) for c in spec["containers"]]
    return spec


def container_spec(value):
    container = deepcopy(value)
    defaults = {
        "imagePullPolicy": "IfNotPresent",
        "terminationMessagePath": "/dev/termination-log",
        "terminationMessagePolicy": "File",
        "resources": {},
        "ports": [],
        "args": [],
        "command": [],
        "env": [],
        "envFrom": [],
        "volumeMounts": [],
    }
    for key, default in defaults.items():
        container.setdefault(key, default)
    for port in container["ports"]:
        port.setdefault("protocol", "TCP")
    for env in container["env"]:
        ref = env.get("valueFrom", {}).get("fieldRef")
        if ref is not None:
            ref.setdefault("apiVersion", "v1")
    return container


def template_configuration(template):
    value = deepcopy(template)
    metadata = value.setdefault("metadata", {})
    if metadata.get("creationTimestamp") is None:
        metadata.pop("creationTimestamp", None)
    require(
        set(metadata) <= {"labels", "annotations"}, "unsupported CSI template metadata"
    )
    metadata.setdefault("labels", {})
    metadata.setdefault("annotations", {})
    value["spec"] = pod_spec(value["spec"])
    return value


def deployment_parent(workload, replica_sets, pods):
    expected = template_configuration(workload["spec"]["template"])
    require(
        "pod-template-hash" not in expected["metadata"]["labels"],
        "reserved deployment hash in CSI template",
    )
    matches = []
    for value in replica_sets:
        if not owner(value, "Deployment", workload["metadata"]["uid"]) or not any(
            owner(pod, "ReplicaSet", value["metadata"]["uid"]) for pod in pods
        ):
            continue
        template = template_configuration(value["spec"]["template"])
        template["metadata"]["labels"].pop("pod-template-hash", None)
        if template == expected and not value["metadata"].get("deletionTimestamp"):
            matches.append(value)
    require(
        len(matches) == 1, "current CSI ReplicaSet template unavailable or ambiguous"
    )
    return matches[0]


def token_projection(volume):
    """Recognize only Kubernetes and exact OpenShift service-CA token projections."""
    name = volume["name"]
    require(
        re.fullmatch(r"kube-api-access-[a-z0-9]{5}", name) is not None,
        "unsupported injected CSI volume",
    )
    sources = volume["projected"]["sources"]
    require(len(sources) in {3, 4}, "unsupported service account projection")
    expiration = sources[0]["serviceAccountToken"]["expirationSeconds"]
    require(
        type(expiration) is int and expiration in (3600, 3607),
        "unsupported service account token lifetime",
    )
    expected = {
        "name": name,
        "projected": {
            "defaultMode": 420,
            "sources": [
                {
                    "serviceAccountToken": {
                        "expirationSeconds": expiration,
                        "path": "token",
                    }
                },
                {
                    "configMap": {
                        "name": "kube-root-ca.crt",
                        "items": [{"key": "ca.crt", "path": "ca.crt"}],
                    }
                },
                {
                    "downwardAPI": {
                        "items": [
                            {
                                "path": "namespace",
                                "fieldRef": {
                                    "apiVersion": "v1",
                                    "fieldPath": "metadata.namespace",
                                },
                            }
                        ]
                    }
                },
            ],
        },
    }
    if len(sources) == 4:
        expected["projected"]["sources"].append(
            {
                "configMap": {
                    "name": "openshift-service-ca.crt",
                    "items": [{"key": "service-ca.crt", "path": "service-ca.crt"}],
                }
            }
        )
    require(volume == expected, "service account projection differs")
    return name


def remove_token_injection(actual, expected):
    names = {v["name"] for v in expected.get("volumes", [])}
    extra = [v for v in actual.get("volumes", []) if v["name"] not in names]
    if not extra:
        return
    require(
        len(extra) == 1 and expected.get("automountServiceAccountToken") is not False,
        "unsupported injected CSI volumes",
    )
    name = token_projection(extra[0])
    mount = {
        "name": name,
        "readOnly": True,
        "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount",
    }
    for container in actual["containers"]:
        mounts = container.get("volumeMounts", [])
        require(mounts.count(mount) == 1, "service account mount differs")
        container["volumeMounts"] = [m for m in mounts if m != mount]
    actual["volumes"] = [v for v in actual["volumes"] if v["name"] != name]


def remove_toleration_injection(actual, expected, kind):
    if kind == "DaemonSet":
        defaults = [
            {"key": "node.kubernetes.io/" + key, "operator": "Exists", "effect": effect}
            for key, effect in (
                ("not-ready", "NoExecute"),
                ("unreachable", "NoExecute"),
                ("disk-pressure", "NoSchedule"),
                ("memory-pressure", "NoSchedule"),
                ("pid-pressure", "NoSchedule"),
                ("unschedulable", "NoSchedule"),
            )
        ]
        if expected.get("hostNetwork"):
            defaults.append(
                {
                    "key": "node.kubernetes.io/network-unavailable",
                    "operator": "Exists",
                    "effect": "NoSchedule",
                }
            )
    else:
        defaults = [
            {
                "key": "node.kubernetes.io/" + key,
                "operator": "Exists",
                "effect": "NoExecute",
                "tolerationSeconds": 300,
            }
            for key in ("not-ready", "unreachable")
        ]
    configured = expected.get("tolerations", [])
    actual["tolerations"] = [
        t for t in actual.get("tolerations", []) if t in configured or t not in defaults
    ]


def remove_node_assignment(actual, expected, kind):
    if kind == "DaemonSet":
        planned = expected.get("affinity", {}).get("nodeAffinity", {})
        require(
            not planned.get("requiredDuringSchedulingIgnoredDuringExecution"),
            "CSI DaemonSet required node affinity unsupported",
        )
        node_affinity = actual.get("affinity", {}).get("nodeAffinity", {})
        required = node_affinity.get("requiredDuringSchedulingIgnoredDuringExecution")
        if required is not None:
            require(
                required
                == {
                    "nodeSelectorTerms": [
                        {
                            "matchFields": [
                                {
                                    "key": "metadata.name",
                                    "operator": "In",
                                    "values": [actual["nodeName"]],
                                }
                            ]
                        }
                    ]
                },
                "CSI DaemonSet node assignment differs",
            )
            node_affinity.pop("requiredDuringSchedulingIgnoredDuringExecution")
            if not node_affinity:
                actual["affinity"].pop("nodeAffinity")
    if "nodeName" not in expected:
        actual.pop("nodeName", None)


def require_pod_configuration(workload, pod, kind):
    expected = template_configuration(workload["spec"]["template"])
    actual = deepcopy(pod["spec"])
    remove_token_injection(actual, expected["spec"])
    remove_toleration_injection(actual, expected["spec"], kind)
    remove_node_assignment(actual, expected["spec"], kind)
    require(
        pod_spec(actual) == expected["spec"], "running CSI pod configuration differs"
    )
    metadata = pod["metadata"]
    labels = deepcopy(metadata.get("labels", {}))
    for key in (
        ("pod-template-hash",)
        if kind == "Deployment"
        else ("controller-revision-hash", "pod-template-generation")
    ):
        if key not in expected["metadata"]["labels"]:
            labels.pop(key, None)
    annotations = deepcopy(metadata.get("annotations", {}))
    for key in (
        "k8s.v1.cni.cncf.io/network-status",
        "k8s.ovn.org/pod-networks",
        "openshift.io/scc",
    ):
        if key not in expected["metadata"]["annotations"]:
            annotations.pop(key, None)
    require(
        labels == expected["metadata"]["labels"]
        and annotations == expected["metadata"]["annotations"],
        "running CSI pod template metadata differs",
    )


def workload_identity(workload, kind, pods, replica_sets):
    count = ready_count(workload, kind)
    uid = workload["metadata"]["uid"]
    if kind == "Deployment":
        parent = deployment_parent(workload, replica_sets, pods)
        selected = [
            p for p in pods if owner(p, "ReplicaSet", parent["metadata"]["uid"])
        ]
    else:
        selected = [p for p in pods if owner(p, kind, uid)]
    require(
        len(selected) == len(pods) == count, "CSI pod inventory differs from workload"
    )
    for pod in selected:
        require_pod_configuration(workload, pod, kind)
    images = [image_ids(p) for p in selected]
    require(all(value == images[0] for value in images), "mixed CSI images")
    expected = {
        c["name"]: c for c in workload["spec"]["template"]["spec"]["containers"]
    }
    require(set(expected) == set(images[0]), "CSI workload containers differ")
    # An OCI index digest can differ from the running platform imageID digest.
    # Pin both independently; never infer equivalence from the desired image.
    require(
        all(
            re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", c["image"])
            for c in expected.values()
        ),
        "CSI configured image is not immutable",
    )
    return CsiWorkloadIdentity(
        uid=uid, spec_sha256=digest(workload["spec"]), images=images[0]
    )


def csi_identity(bundle):
    controller, node = bundle["controller"], bundle["node"]
    require(
        all(
            not bundle[k]["metadata"].get("deletionTimestamp")
            for k in ("namespace", "driver")
        ),
        "terminating CSI installation",
    )
    installation = CsiInstallationIdentity(
        version=OBSERVER_VERSION,
        namespace_uid=bundle["namespace"]["metadata"]["uid"],
        driver_uid=bundle["driver"]["metadata"]["uid"],
        driver_spec_sha256=digest(bundle["driver"]["spec"]),
        controller=workload_identity(
            controller, "Deployment", bundle["controller_pods"], bundle["replica_sets"]
        ),
        node=workload_identity(node, "DaemonSet", bundle["node_pods"], []),
    )
    require(
        installation.controller.images["csi-rbdplugin"]
        == installation.node.images["csi-rbdplugin"],
        "controller/node CSI images differ",
    )
    return installation


def server_version(value):
    require(isinstance(value, dict), "Ceph versions unavailable")
    for role in ("mon", "mgr", "osd", "overall"):
        versions = value.get(role)
        require(
            isinstance(versions, dict) and len(versions) == 1,
            "mixed or missing Ceph server versions",
        )
        require(
            all(type(n) is int and n > 0 for n in versions.values()),
            "invalid daemon count",
        )
    versions = value["overall"]
    version = next(iter(versions))
    require(
        all(
            isinstance(v, dict)
            and (not v or set(v) == {version})
            and all(type(n) is int and n > 0 for n in v.values())
            for v in value.values()
        ),
        "mixed Ceph server versions",
    )
    return version


def source(result, timestamps):
    require(result.status == "complete", "initialization source " + result.status)
    require(
        0 <= (datetime.now(UTC) - result.collected_at).total_seconds() <= 60,
        "initialization source stale or future-dated",
    )
    timestamps.append(result.collected_at)
    return result.value


def target_from_reads(kube_value, backend, timestamps):
    sc = kube_value["storage_class"]
    params = sc.get("parameters", {})
    fsid = source(backend.cluster_identity(), timestamps)
    pools = source(backend.pool_identity(), timestamps)
    require(isinstance(pools, list), "Ceph pool inventory unavailable")
    matches = [
        p
        for p in backend.pools
        if p.name == params.get("pool") and p.csi_cluster_id == params.get("clusterID")
    ]
    require(len(matches) == 1, "unsupported StorageClass pool mapping")
    pool = matches[0]
    rows = [p for p in pools if p.get("poolname") == pool.name]
    require(
        len(rows) == 1
        and rows[0]["poolnum"] == pool.pool_id
        and fsid == backend.ceph_fsid,
        "Ceph pool or cluster changed",
    )
    namespace = params.get("radosNamespace", "")
    require(namespace == pool.rbd_namespace, "RBD namespace differs")
    namespaces = source(backend.namespaces(pool.name), timestamps)
    require(isinstance(namespaces, list), "RBD namespace inventory unavailable")
    require(
        all(
            isinstance(n, dict) and set(n) == {"name"} and isinstance(n["name"], str)
            for n in namespaces
        ),
        "unsupported RBD namespace output",
    )
    names = [n["name"] for n in namespaces]
    require(len(names) == len(set(names)), "ambiguous RBD namespaces")
    require(not namespace or namespace in names, "RBD namespace missing")
    require(
        params.get("imageFormat") == "2"
        and params.get("thickProvision", "false") == "false",
        "unsupported image format/preallocation",
    )
    require(
        params.get("journalPool", pool.name) == pool.name,
        "separate journal pool unsupported",
    )
    return InitializationTarget(
        provider_id=kube_value["provider_id"],
        cluster_key=kube_value["cluster_key"],
        ceph_fsid=fsid,
        pool_id=rows[0]["poolnum"],
        rbd_namespace=namespace,
        storage_class_name=sc["metadata"]["name"],
        storage_class_uid=sc["metadata"]["uid"],
        provisioner=sc["provisioner"],
        storage_class_parameters=params,
        storage_class_options=StorageClassOptions(
            volume_binding_mode=sc.get("volumeBindingMode", "Immediate"),
            reclaim_policy=sc.get("reclaimPolicy", "Delete"),
            allow_volume_expansion=sc.get("allowVolumeExpansion", False),
            mount_options=sc.get("mountOptions", []),
        ),
        csi_version=OBSERVER_VERSION,
        csi_installation=csi_identity(kube_value),
        ceph_version=server_version(source(backend.server_versions(), timestamps)),
        rbd_command_version=source(backend.command_version(), timestamps),
    )


class ProductionObserver:
    def observe(self, profile, kube, backend):
        try:
            require(
                profile.target.csi_version == OBSERVER_VERSION,
                "unsupported CSI version contract",
            )
            timestamps = []
            first = source(
                kube.initialization_identity(profile.target.storage_class_name),
                timestamps,
            )
            target = target_from_reads(first, backend, timestamps)
            last = source(
                kube.initialization_identity(profile.target.storage_class_name),
                timestamps,
            )
            require(first == last, "Kubernetes installation changed during observation")
            require(
                source(backend.cluster_identity(), timestamps) == target.ceph_fsid,
                "Ceph identity changed during observation",
            )
            pools = source(backend.pool_identity(), timestamps)
            require(
                [p for p in pools if p.get("poolnum") == target.pool_id]
                == [
                    {
                        "poolnum": target.pool_id,
                        "poolname": first["storage_class"]["parameters"]["pool"],
                    }
                ],
                "Ceph pool identity changed during observation",
            )
            require(
                server_version(source(backend.server_versions(), timestamps))
                == target.ceph_version,
                "Ceph versions changed during observation",
            )
            observed = min(timestamps)
            require(
                0 <= (datetime.now(UTC) - observed).total_seconds() <= 60,
                "initialization sources stale or future-dated",
            )
            return InitializationObservation(
                version="native-initialization-observation-v1",
                metric=profile.metric,
                measurement_version=profile.measurement_version,
                profile_digest=digest(profile),
                target=target,
                observed_at=observed,
            )
        except Exception as error:
            raise AccountingUnavailable(
                "Native installed initialization identity unavailable or unsupported"
            ) from error
