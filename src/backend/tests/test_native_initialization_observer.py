"""Bounded real readers with API/command fixtures; no physical acceptance."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.schemas.kubevirt_storage import CollectionLimits, ReadResult
from app.schemas.kubevirt_storage_initialization import InitializationEvidence
from app.schemas.quota import AccountingUnavailable
from app.services import native_initialization as initialization
from app.services.kubevirt_storage_enrollment import digest
from app.services.kubevirt_storage_transport import PoolConfig, RbdReader, command_argv
from tests.test_native_initialization_evidence import synthetic_profile


def test_public_observer_commands_are_fixed_and_read_only():
    assert command_argv("server_versions") == ["ceph", "versions", "--format", "json"]
    assert command_argv("namespaces", pool="tenant") == [
        "rbd",
        "namespace",
        "list",
        "--pool",
        "tenant",
        "--format",
        "json",
    ]


def test_observer_is_independent_of_activation_selector():
    from app.services.native_initialization_observer import OBSERVER_VERSION

    assert OBSERVER_VERSION == "odf-rbd-installation-v1"
    with pytest.raises(AccountingUnavailable, match="physical acceptance"):
        initialization.implementation(
            InitializationEvidence.model_validate(synthetic_profile())
        )


@pytest.fixture
def installed(monkeypatch):
    import importlib.util
    import json
    import time
    from pathlib import Path

    from kubernetes import client

    from app.services.kubevirt_storage_transport import (
        CapabilityProfile,
        DirectTransport,
    )

    scripts = Path(__file__).resolve().parents[3] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "observer_cli", scripts / "import_kubevirt_storage.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    driver = "openshift-storage.rbd.csi.ceph.com"
    images = {"csi-rbdplugin": "sha256:" + "1" * 64, "sidecar": "sha256:" + "2" * 64}
    template = {
        "spec": {
            "containers": [
                {"name": n, "image": "registry.example/csi@" + v}
                for n, v in images.items()
            ]
        }
    }
    controller = {
        "metadata": {"uid": "ctrl-uid", "generation": 1},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "ctrl"}},
            "template": deepcopy(template),
        },
        "status": {
            "observedGeneration": 1,
            "replicas": 1,
            "updatedReplicas": 1,
            "readyReplicas": 1,
            "availableReplicas": 1,
        },
    }
    node = {
        "metadata": {"uid": "node-uid", "generation": 1},
        "spec": {
            "selector": {"matchLabels": {"app": "node"}},
            "template": deepcopy(template),
        },
        "status": {
            "observedGeneration": 1,
            "desiredNumberScheduled": 1,
            "currentNumberScheduled": 1,
            "updatedNumberScheduled": 1,
            "numberReady": 1,
            "numberAvailable": 1,
        },
    }

    def pod(kind, uid):
        return {
            "metadata": {
                "uid": uid + "-pod",
                "ownerReferences": [{"kind": kind, "uid": uid, "controller": True}],
            },
            "spec": deepcopy(template["spec"]),
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [
                    {
                        "name": n,
                        "imageID": "registry.example/csi@" + v,
                        "ready": True,
                        "state": {"running": {"startedAt": "2026-01-01"}},
                    }
                    for n, v in images.items()
                ],
            },
        }

    sc = {
        "metadata": {"name": "tenant-sc", "uid": "sc-uid"},
        "provisioner": driver,
        "parameters": {
            "clusterID": "openshift-storage",
            "pool": "tenant",
            "imageFormat": "2",
        },
        "volumeBindingMode": "Immediate",
        "reclaimPolicy": "Delete",
        "allowVolumeExpansion": True,
    }
    data = {
        "sc": sc,
        "controller": controller,
        "node": node,
        "driver": {"metadata": {"uid": "driver-uid"}, "spec": {"attachRequired": True}},
        "ctrl_pods": [pod("ReplicaSet", "rs-uid")],
        "node_pods": [pod("DaemonSet", "node-uid")],
        "rs": [
            {
                "spec": {"template": deepcopy(template)},
                "metadata": {
                    "uid": "rs-uid",
                    "ownerReferences": [
                        {"kind": "Deployment", "uid": "ctrl-uid", "controller": True}
                    ],
                },
            }
        ],
    }
    calls = []

    def read(key):
        def get(*args, **kwargs):
            calls.append((key, args, kwargs))
            assert 0 < kwargs["_request_timeout"] <= 15
            return deepcopy(data[key])

        return get

    def listing(key):
        def get(*args, **kwargs):
            assert kwargs["limit"] <= 1000
            return {"metadata": {}, "items": read(key)(*args, **kwargs)}

        return get

    apps = SimpleNamespace(
        read_namespaced_deployment=read("controller"),
        read_namespaced_daemon_set=read("node"),
        list_namespaced_replica_set=listing("rs"),
    )
    storage = SimpleNamespace(
        read_storage_class=read("sc"), read_csi_driver=read("driver")
    )
    monkeypatch.setattr(client, "AppsV1Api", lambda api: apps)
    monkeypatch.setattr(client, "StorageV1Api", lambda api: storage)
    reader = module.ProviderReader.__new__(module.ProviderReader)
    reader.provider_id = "00000000-0000-4000-8000-000000000003"
    reader.limits, reader.deadline = CollectionLimits(), time.monotonic() + 120
    reader.api = SimpleNamespace(sanitize_for_serialization=deepcopy)

    def namespace(name, **kwargs):
        return {
            "metadata": {
                "uid": "cluster-uid" if name == "kube-system" else "storage-ns-uid"
            }
        }

    reader.core = SimpleNamespace(
        read_namespace=namespace,
        list_namespaced_pod=lambda *a, **kw: listing(
            "ctrl_pods" if kw["label_selector"] == "app=ctrl" else "node_pods"
        )(*a, **kw),
    )
    timestamp = datetime.now(UTC) - timedelta(seconds=1)
    ceph = "ceph version 19.2.1 test (stable)"
    outputs = {
        "fsid": "ceph-fsid",
        "pools": [{"poolnum": 1, "poolname": "tenant"}],
        "namespaces": [],
        "version": ceph,
        "server_versions": {k: {ceph: 1} for k in ("mon", "mgr", "osd", "overall")},
    }
    transport = DirectTransport(CollectionLimits())
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        op = next(
            k
            for k in outputs
            if command_argv(k, **({"pool": "tenant"} if k == "namespaces" else {}))
            == argv
        )
        value = outputs[op]
        return ReadResult(
            status="complete",
            value=json.dumps(value) if isinstance(value, (dict, list)) else value,
            collected_at=timestamp,
        )

    monkeypatch.setattr(transport.process, "run", run)
    backend = RbdReader(
        transport,
        "ceph-fsid",
        [PoolConfig(pool_id=1, name="tenant", csi_cluster_id="openshift-storage")],
        CapabilityProfile(command_version=ceph),
    )
    profile_data = synthetic_profile()
    profile_data["target"] = {
        "provider_id": reader.provider_id,
        "cluster_key": "cluster-uid",
        "ceph_fsid": "ceph-fsid",
        "pool_id": 1,
        "rbd_namespace": "",
        "storage_class_name": "tenant-sc",
        "storage_class_uid": "sc-uid",
        "provisioner": driver,
        "storage_class_parameters": deepcopy(sc["parameters"]),
        "storage_class_options": {
            "volume_binding_mode": "Immediate",
            "reclaim_policy": "Delete",
            "allow_volume_expansion": True,
            "mount_options": [],
        },
        "csi_version": "odf-rbd-installation-v1",
        "ceph_version": ceph,
        "rbd_command_version": ceph,
        "csi_installation": {
            "version": "odf-rbd-installation-v1",
            "namespace_uid": "storage-ns-uid",
            "driver_uid": "driver-uid",
            "driver_spec_sha256": digest(data["driver"]["spec"]),
            "controller": {
                "uid": "ctrl-uid",
                "spec_sha256": digest(controller["spec"]),
                "images": images,
            },
            "node": {
                "uid": "node-uid",
                "spec_sha256": digest(node["spec"]),
                "images": images,
            },
        },
    }
    return SimpleNamespace(
        profile=InitializationEvidence.model_validate(profile_data),
        reader=reader,
        backend=backend,
        data=data,
        outputs=outputs,
        commands=commands,
        calls=calls,
        timestamp=timestamp,
    )


def test_real_readers_independently_observe_exact_installation(installed):
    q = installed
    value = initialization.observe(q.profile, q.reader, q.backend)
    assert value.target == q.profile.target
    assert value.observed_at == q.timestamp
    assert "headroom_bytes" not in value.model_dump(mode="json")
    assert ["ceph", "versions", "--format", "json"] in q.commands
    assert not any("df" in c for c in q.commands)
    assert len(q.calls) >= 12
    with pytest.raises(AccountingUnavailable, match="physical acceptance"):
        initialization.implementation(q.profile)


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_id", UUID("00000000-0000-4000-8000-000000000099")),
        ("cluster_key", "other-cluster"),
        ("storage_class_uid", "other-sc"),
        ("ceph_version", "invented-version"),
        ("rbd_command_version", "invented-client"),
        ("pool_id", 99),
        ("rbd_namespace", "other-namespace"),
    ],
)
def test_expected_target_cannot_supply_observed_fields(installed, field, value):
    q = installed
    profile = q.profile.model_copy(
        update={"target": q.profile.target.model_copy(update={field: value})}
    )
    with pytest.raises(AccountingUnavailable, match="target"):
        initialization.observe(profile, q.reader, q.backend)


@pytest.mark.parametrize(
    "mutation",
    [
        "mixed",
        "unready",
        "owner",
        "generation",
        "missing",
        "mutable",
        "sc-options",
        "preallocation",
        "server-mixed",
        "unsupported",
        "truncated",
    ],
)
def test_installed_mismatch_or_incomplete_sources_close_observation(
    installed, mutation, monkeypatch
):
    q = installed
    if mutation == "mixed":
        q.data["node_pods"][0]["status"]["containerStatuses"][0]["imageID"] = (
            "sha256:" + "9" * 64
        )
    elif mutation == "unready":
        q.data["ctrl_pods"][0]["status"]["conditions"] = []
    elif mutation == "owner":
        q.data["rs"][0]["metadata"]["ownerReferences"][0]["uid"] = "other"
    elif mutation == "generation":
        q.data["controller"]["status"]["observedGeneration"] = 0
    elif mutation == "missing":
        q.data["node_pods"] = []
    elif mutation == "mutable":
        q.data["node"]["spec"]["template"]["spec"]["containers"][0][
            "image"
        ] = "csi:latest"
    elif mutation == "sc-options":
        q.data["sc"]["volumeBindingMode"] = "WaitForFirstConsumer"
    elif mutation == "preallocation":
        q.data["sc"]["parameters"]["thickProvision"] = "true"
    elif mutation == "server-mixed":
        q.outputs["server_versions"]["osd"]["another version"] = 1
    elif mutation == "unsupported":
        q.data["sc"]["provisioner"] = "other.csi.example"
    else:
        monkeypatch.setattr(
            q.reader,
            "_identity_list",
            lambda *a: (_ for _ in ()).throw(ValueError("truncated")),
        )
    with pytest.raises(AccountingUnavailable):
        initialization.observe(q.profile, q.reader, q.backend)


@pytest.mark.parametrize("age", [61, -5])
def test_observer_rejects_stale_or_future_source(installed, age, monkeypatch):
    q = installed
    original = q.backend.cluster_identity
    monkeypatch.setattr(
        q.backend,
        "cluster_identity",
        lambda: original().model_copy(
            update={"collected_at": datetime.now(UTC) - timedelta(seconds=age)}
        ),
    )
    with pytest.raises(AccountingUnavailable):
        initialization.observe(q.profile, q.reader, q.backend)


def test_permission_failure_is_unavailable_without_reading_secrets(
    installed, monkeypatch
):
    q = installed
    monkeypatch.setattr(
        q.reader.core,
        "read_namespace",
        lambda *a, **kw: (_ for _ in ()).throw(PermissionError("forbidden")),
    )
    result = q.reader.initialization_identity("tenant-sc")
    assert result.status == "unavailable" and result.value is None
    with pytest.raises(AccountingUnavailable):
        initialization.observe(q.profile, q.reader, q.backend)


def test_running_platform_digest_is_independent_of_desired_index(installed):
    q = installed
    for pod in q.data["ctrl_pods"]:
        pod["status"]["containerStatuses"][1]["imageID"] = "sha256:" + "3" * 64
    installation = q.profile.target.csi_installation
    controller = installation.controller.model_copy(
        update={
            "images": {
                **installation.controller.images,
                "sidecar": "sha256:" + "3" * 64,
            }
        }
    )
    profile = q.profile.model_copy(
        update={
            "target": q.profile.target.model_copy(
                update={
                    "csi_installation": installation.model_copy(
                        update={"controller": controller}
                    )
                }
            )
        }
    )
    assert initialization.observe(profile, q.reader, q.backend).target == profile.target


def test_named_rbd_namespace_uses_exact_public_object_schema(installed):
    q = installed
    q.outputs["namespaces"] = [{"name": "tenant-ns"}]
    q.data["sc"]["parameters"]["radosNamespace"] = "tenant-ns"
    q.backend.pools = [
        q.backend.pools[0].model_copy(update={"rbd_namespace": "tenant-ns"})
    ]
    profile = q.profile.model_copy(
        update={
            "target": q.profile.target.model_copy(
                update={
                    "rbd_namespace": "tenant-ns",
                    "storage_class_parameters": deepcopy(q.data["sc"]["parameters"]),
                }
            )
        }
    )
    assert initialization.observe(profile, q.reader, q.backend).target == profile.target


def test_kubernetes_identity_replacement_during_reads_rejects(installed, monkeypatch):
    q = installed
    read = q.reader.initialization_identity
    count = 0

    def replaced(name):
        nonlocal count
        count += 1
        if count == 2:
            q.data["driver"]["metadata"]["uid"] = "replacement"
        return read(name)

    monkeypatch.setattr(q.reader, "initialization_identity", replaced)
    with pytest.raises(AccountingUnavailable):
        initialization.observe(q.profile, q.reader, q.backend)


def test_legacy_target_and_observation_serialized_shape_is_unchanged():
    profile_data = synthetic_profile()
    profile = InitializationEvidence.model_validate(profile_data)
    assert profile.model_dump(mode="json") == profile_data
    from app.schemas.kubevirt_storage import InitializationObservation

    original = {
        "version": "native-initialization-observation-v1",
        "metric": profile.metric,
        "measurement_version": profile.measurement_version,
        "profile_digest": digest(profile),
        "target": profile_data["target"],
        "observed_at": "2026-01-01T00:00:00Z",
        "headroom_bytes": None,
        "headroom_semantics": "old-semantics",
    }
    assert (
        InitializationObservation.model_validate(original).model_dump(mode="json")
        == original
    )


@pytest.mark.parametrize("role", ["ctrl_pods", "node_pods"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("args", ["--enable-new-initialization=true"]),
        ("env", [{"name": "CSI_CONFIG", "value": "different"}]),
        ("volumeMounts", [{"name": "alternate-config", "mountPath": "/etc/ceph"}]),
        ("securityContext", {"privileged": True}),
    ],
)
def test_same_image_running_configuration_drift_rejects(installed, role, field, value):
    q = installed
    q.data[role][0]["spec"]["containers"][0][field] = value
    with pytest.raises(AccountingUnavailable):
        initialization.observe(q.profile, q.reader, q.backend)


@pytest.mark.parametrize("change_pod", [False, True])
def test_old_same_image_replicaset_cannot_supply_current_deployment(
    installed, change_pod
):
    q = installed
    q.data["rs"][0]["spec"]["template"]["spec"]["containers"][0]["args"] = [
        "--old-config"
    ]
    if change_pod:
        q.data["ctrl_pods"][0]["spec"]["containers"][0]["args"] = ["--old-config"]
    with pytest.raises(AccountingUnavailable):
        initialization.observe(q.profile, q.reader, q.backend)


def service_account_volume():
    return {
        "name": "kube-api-access-abc12",
        "projected": {
            "defaultMode": 420,
            "sources": [
                {"serviceAccountToken": {"expirationSeconds": 3607, "path": "token"}},
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


def inject_service_account(pod):
    volume = service_account_volume()
    pod["spec"]["volumes"] = [volume]
    for container in pod["spec"]["containers"]:
        container["volumeMounts"] = [
            {
                "name": volume["name"],
                "readOnly": True,
                "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount",
            }
        ]


@pytest.mark.parametrize("role", ["ctrl_pods", "node_pods"])
def test_bounded_api_defaults_and_service_account_injection_match(installed, role):
    q = installed
    pod = q.data[role][0]
    pod["spec"].update(
        nodeName="node-one",
        serviceAccountName="default",
        serviceAccount="default",
        restartPolicy="Always",
        dnsPolicy="ClusterFirst",
        schedulerName="default-scheduler",
        terminationGracePeriodSeconds=30,
        enableServiceLinks=True,
        securityContext={},
        priority=0,
    )
    for container in pod["spec"]["containers"]:
        container.update(
            imagePullPolicy="IfNotPresent",
            terminationMessagePath="/dev/termination-log",
            terminationMessagePolicy="File",
            resources={},
        )
    inject_service_account(pod)
    assert (
        initialization.observe(q.profile, q.reader, q.backend).target
        == q.profile.target
    )


@pytest.mark.parametrize(
    "change",
    [
        "root-ca",
        "namespace",
        "mount",
        "token-audience",
        "security",
        "extra-field",
        "sidecar",
        "init",
    ],
)
@pytest.mark.parametrize("role", ["ctrl_pods", "node_pods"])
def test_unsupported_admission_injection_is_not_hidden(installed, change, role):
    q = installed
    pod = q.data[role][0]
    inject_service_account(pod)
    if change == "root-ca":
        pod["spec"]["volumes"][0]["projected"]["sources"][1]["configMap"][
            "name"
        ] = "other-config"
    elif change == "namespace":
        pod["spec"]["volumes"][0]["projected"]["sources"][2]["downwardAPI"]["items"][0][
            "fieldRef"
        ]["fieldPath"] = "metadata.name"
    elif change == "mount":
        pod["spec"]["containers"][0]["volumeMounts"][0]["readOnly"] = False
    elif change == "token-audience":
        pod["spec"]["volumes"][0]["projected"]["sources"][0]["serviceAccountToken"][
            "audience"
        ] = "alternate"
    elif change == "security":
        pod["spec"]["securityContext"] = {"seLinuxOptions": {"level": "unexpected"}}
    elif change == "extra-field":
        pod["spec"]["hostPID"] = True
    elif change == "sidecar":
        pod["spec"]["containers"].append({"name": "new-sidecar", "image": "csi:new"})
    else:
        pod["spec"]["initContainers"] = [{"name": "setup", "image": "csi:setup"}]
    with pytest.raises(AccountingUnavailable):
        initialization.observe(q.profile, q.reader, q.backend)


def test_daemonset_assignment_and_standard_tolerations_match(installed):
    q = installed
    pod = q.data["node_pods"][0]
    pod["spec"].update(
        nodeName="node-one",
        affinity={
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchFields": [
                                {
                                    "key": "metadata.name",
                                    "operator": "In",
                                    "values": ["node-one"],
                                }
                            ]
                        }
                    ]
                }
            }
        },
    )
    pod["spec"]["tolerations"] = [
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
    pod["metadata"]["labels"] = {
        "controller-revision-hash": "hash-one",
        "pod-template-generation": "1",
    }
    assert (
        initialization.observe(q.profile, q.reader, q.backend).target
        == q.profile.target
    )


@pytest.mark.parametrize("change", ["affinity", "toleration"])
def test_daemonset_arbitrary_assignment_changes_reject(installed, change):
    q = installed
    pod = q.data["node_pods"][0]
    if change == "affinity":
        pod["spec"].update(
            nodeName="node-one",
            affinity={
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchFields": [
                                    {
                                        "key": "metadata.name",
                                        "operator": "In",
                                        "values": ["other-node"],
                                    }
                                ]
                            }
                        ]
                    }
                }
            },
        )
    else:
        pod["spec"]["tolerations"] = [
            {"key": "unreviewed", "operator": "Exists", "effect": "NoSchedule"}
        ]
    with pytest.raises(AccountingUnavailable):
        initialization.observe(q.profile, q.reader, q.backend)


def test_deployment_hash_label_and_default_tolerations_match(installed):
    q = installed
    q.data["rs"][0]["spec"]["template"]["metadata"] = {
        "labels": {"pod-template-hash": "current-hash"}
    }
    pod = q.data["ctrl_pods"][0]
    pod["metadata"]["labels"] = {"pod-template-hash": "current-hash"}
    pod["metadata"]["annotations"] = {
        "openshift.io/scc": "privileged",
        "k8s.ovn.org/pod-networks": "status-only",
    }
    pod["spec"]["tolerations"] = [
        {
            "key": "node.kubernetes.io/" + key,
            "operator": "Exists",
            "effect": "NoExecute",
            "tolerationSeconds": 300,
        }
        for key in ("not-ready", "unreachable")
    ]
    assert (
        initialization.observe(q.profile, q.reader, q.backend).target
        == q.profile.target
    )


def test_inactive_old_replicaset_is_not_the_selected_running_parent(installed):
    q = installed
    old = deepcopy(q.data["rs"][0])
    old["metadata"]["uid"] = "inactive-old-rs"
    old["spec"]["template"]["spec"]["containers"][0]["args"] = ["--old-config"]
    q.data["rs"].append(old)
    assert (
        initialization.observe(q.profile, q.reader, q.backend).target
        == q.profile.target
    )
