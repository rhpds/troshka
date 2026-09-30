"""Project-namespace stuck Terminating / Multus sandbox pod heal."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from helpers.stuck_pod_heal import (
    ANN_ATTEMPTS,
    ANN_STUCK_NODES,
    _is_pod_stuck_creating,
    _is_pod_stuck_terminating,
    _plan_pod_heal,
    _should_skip_pod,
    heal_stuck_project_pods,
)


def _pod(
    *,
    name="gateway-troshka-aabbccdd-xxx",
    age_s=200,
    node="ocp-virt6-host4",
    phase="Pending",
    scheduled=True,
    creating=True,
    deleting_s=None,
    annotations=None,
    labels=None,
    owner_kind=None,
    owner_name=None,
):
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = "troshka-aabbccdd"
    pod.metadata.creation_timestamp = datetime.now(UTC) - timedelta(seconds=age_s)
    pod.metadata.deletion_timestamp = (
        datetime.now(UTC) - timedelta(seconds=deleting_s)
        if deleting_s is not None
        else None
    )
    pod.metadata.annotations = annotations or {}
    pod.metadata.labels = labels or {"app": "troshka-gateway-aabbccdd"}
    if owner_kind:
        owner = MagicMock()
        owner.kind = owner_kind
        owner.name = owner_name or "gateway-troshka-aabbccdd"
        owner.controller = True
        pod.metadata.owner_references = [owner]
    else:
        pod.metadata.owner_references = []
    pod.spec.node_name = node
    pod.spec.affinity = None
    pod.spec.restart_policy = "Always"
    pod.spec.service_account_name = "default"
    pod.spec.containers = [{"name": "gw", "image": "gateway:latest"}]
    pod.spec.volumes = []
    pod.status.phase = phase
    cond = MagicMock()
    cond.type = "PodScheduled"
    cond.status = "True" if scheduled else "False"
    pod.status.conditions = [cond]
    if creating:
        cs = MagicMock()
        cs.name = "gw"
        waiting = MagicMock()
        waiting.reason = "ContainerCreating"
        cs.state = MagicMock(waiting=waiting, running=None)
        pod.status.container_statuses = [cs]
    else:
        cs = MagicMock()
        cs.name = "gw"
        cs.state = MagicMock(waiting=None, running=MagicMock())
        pod.status.container_statuses = [cs]
    return pod


class TestSkipPods:
    def test_skips_virt_launcher(self):
        pod = _pod(
            name="virt-launcher-troshka-vm-f5131b65-abc",
            labels={"kubevirt.io/domain": "x"},
        )
        assert _should_skip_pod(pod) is True

    def test_skips_ops_pod(self):
        pod = _pod(name="troshka-aabbccdd-ops", labels={"app": "troshka-ops-pod"})
        assert _should_skip_pod(pod) is True

    def test_keeps_gateway(self):
        assert _should_skip_pod(_pod()) is False


class TestStuckTerminating:
    def test_detects_after_threshold(self):
        now = time.time()
        pod = _pod(deleting_s=180, creating=False, phase="Running")
        assert _is_pod_stuck_terminating(pod, now=now, threshold_s=120)

    def test_not_before_threshold(self):
        now = time.time()
        pod = _pod(deleting_s=30, creating=False, phase="Running")
        assert not _is_pod_stuck_terminating(pod, now=now, threshold_s=120)

    def test_not_when_not_deleting(self):
        now = time.time()
        assert not _is_pod_stuck_terminating(_pod(), now=now, threshold_s=120)


class TestStuckCreating:
    def test_detects_multus_empty_statuses(self):
        now = time.time()
        pod = _pod(age_s=200)
        pod.status.container_statuses = None
        assert _is_pod_stuck_creating(pod, now=now, threshold_s=180)

    def test_not_when_running(self):
        now = time.time()
        assert not _is_pod_stuck_creating(
            _pod(age_s=300, creating=False, phase="Running"),
            now=now,
            threshold_s=180,
        )


class TestPlanHeal:
    def test_first_hit_no_exclude(self):
        plan = _plan_pod_heal({}, "host4")
        assert plan["action"] == "reschedule"
        assert plan["exclude"] == []
        assert plan["attempts"] == 1

    def test_second_same_node_excludes(self):
        plan = _plan_pod_heal({ANN_STUCK_NODES: "host4:1", ANN_ATTEMPTS: "1"}, "host4")
        assert plan["exclude"] == ["host4"]

    def test_exhausted(self):
        plan = _plan_pod_heal({ANN_STUCK_NODES: "host4:2", ANN_ATTEMPTS: "3"}, "host4")
        assert plan["action"] == "exhausted"


class TestHealStuckProjectPods:
    def test_force_deletes_stuck_terminating_deployment_pod(self):
        core = MagicMock()
        apps = MagicMock()
        pod = _pod(
            deleting_s=180,
            creating=False,
            phase="Running",
            owner_kind="ReplicaSet",
            owner_name="gateway-troshka-aabbccdd-68b7bf9b6d",
        )
        core.list_namespaced_pod.return_value = MagicMock(items=[pod])

        details, errors = heal_stuck_project_pods(
            core, apps, "troshka-aabbccdd", now=time.time()
        )
        assert details
        assert errors == []
        core.delete_namespaced_pod.assert_called_once()
        kwargs = core.delete_namespaced_pod.call_args.kwargs
        assert kwargs.get("grace_period_seconds") == 0
        # Deployment-owned: no recreate of the Pod itself
        core.create_namespaced_pod.assert_not_called()

    def test_recreates_bare_pod_with_affinity_after_second_hit(self):
        core = MagicMock()
        apps = MagicMock()
        pod = _pod(
            name="pod-93482a8a",
            age_s=200,
            node="host4",
            labels={"app": "troshka-pod", "troshka-pod": "93482a8a"},
            annotations={ANN_STUCK_NODES: "host4:1", ANN_ATTEMPTS: "1"},
            owner_kind="TroshkaProject",
            owner_name="project-aabbccdd",
        )
        # After delete, recreate path uses read → already have pod; create retries
        core.list_namespaced_pod.return_value = MagicMock(items=[pod])
        core.read_namespaced_pod.side_effect = Exception("404")

        details, errors = heal_stuck_project_pods(
            core, apps, "troshka-aabbccdd", now=time.time()
        )
        assert errors == []
        assert details
        core.delete_namespaced_pod.assert_called()
        core.create_namespaced_pod.assert_called_once()
        body = core.create_namespaced_pod.call_args.kwargs.get("body")
        if body is None:
            body = core.create_namespaced_pod.call_args.args[-1]
        aff = body["spec"]["affinity"]
        vals = aff["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][
            "nodeSelectorTerms"
        ][0]["matchExpressions"][0]["values"]
        assert "host4" in vals

    def test_patches_deployment_affinity_on_repeated_node(self):
        core = MagicMock()
        apps = MagicMock()
        pod = _pod(
            age_s=200,
            node="host4",
            owner_kind="ReplicaSet",
            owner_name="gateway-troshka-aabbccdd-68b7bf9b6d",
            annotations={},
        )
        core.list_namespaced_pod.return_value = MagicMock(items=[pod])
        # ReplicaSet → Deployment owner
        rs = MagicMock()
        rs_owner = MagicMock()
        rs_owner.kind = "Deployment"
        rs_owner.name = "gateway-troshka-aabbccdd"
        rs_owner.controller = True
        rs.metadata.owner_references = [rs_owner]
        core.read_namespaced_pod  # unused
        apps.read_namespaced_replica_set.return_value = rs
        dep = MagicMock()
        dep.metadata.name = "gateway-troshka-aabbccdd"
        dep.metadata.annotations = {
            ANN_STUCK_NODES: "host4:1",
            ANN_ATTEMPTS: "1",
        }
        apps.read_namespaced_deployment.return_value = dep

        heal_stuck_project_pods(core, apps, "troshka-aabbccdd", now=time.time())
        apps.patch_namespaced_deployment.assert_called()
        body = apps.patch_namespaced_deployment.call_args.kwargs.get("body")
        if body is None:
            body = apps.patch_namespaced_deployment.call_args.args[-1]
        assert "host4" in str(body)

    def test_skips_virt_launcher(self):
        core = MagicMock()
        apps = MagicMock()
        pod = _pod(
            name="virt-launcher-x",
            age_s=200,
            labels={"kubevirt.io/domain": "kv-1"},
        )
        core.list_namespaced_pod.return_value = MagicMock(items=[pod])
        details, _ = heal_stuck_project_pods(
            core, apps, "troshka-aabbccdd", now=time.time()
        )
        assert details == []
        core.delete_namespaced_pod.assert_not_called()
