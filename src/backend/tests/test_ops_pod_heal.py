"""Stuck ContainerCreating heal for the KubeVirt ops pod."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from app.services.ocp.ops_pod_heal import (
    ANN_ATTEMPTS,
    ANN_STUCK_NODES,
    _format_stuck_node_counts,
    _is_ops_pod_stuck_creating,
    _node_hostname_not_in_affinity,
    _parse_stuck_node_counts,
    heal_stuck_ops_pod,
    plan_ops_pod_heal,
)


def _create_body(core):
    args, kwargs = core.create_namespaced_pod.call_args
    if "body" in kwargs:
        return kwargs["body"]
    # create_namespaced_pod(namespace=..., body=...) or (namespace, body)
    return args[-1]


def _pod(
    *,
    age_s=200,
    node="ocp-virt6-host6",
    creating=True,
    scheduled=True,
    phase="Pending",
    annotations=None,
    affinity=None,
):
    pod = MagicMock()
    pod.metadata.name = "troshka-aabbccdd-ops"
    pod.metadata.creation_timestamp = datetime.now(UTC) - timedelta(seconds=age_s)
    pod.metadata.annotations = annotations or {}
    pod.metadata.namespace = "troshka-aabbccdd"
    pod.metadata.uid = "uid-1"
    pod.metadata.resource_version = "99"
    pod.metadata.labels = {"app": "troshka-ops-pod"}
    pod.spec.node_name = node
    pod.spec.affinity = affinity
    pod.spec.restart_policy = "Always"
    pod.spec.service_account_name = "default"
    pod.spec.containers = [{"name": "ops", "image": "ops:latest"}]
    pod.spec.volumes = []
    pod.status.phase = phase
    cond = MagicMock()
    cond.type = "PodScheduled"
    cond.status = "True" if scheduled else "False"
    pod.status.conditions = [cond]
    if creating:
        cs = MagicMock()
        cs.name = "ops"
        waiting = MagicMock()
        waiting.reason = "ContainerCreating"
        cs.state = MagicMock(waiting=waiting, running=None)
        pod.status.container_statuses = [cs]
    else:
        cs = MagicMock()
        cs.name = "ops"
        cs.state = MagicMock(waiting=None, running=MagicMock())
        pod.status.container_statuses = [cs]
    return pod


class TestOpsPodStuckDetection:
    def test_detects_stuck_after_threshold(self):
        now = time.time()
        assert _is_ops_pod_stuck_creating(_pod(age_s=200), now=now, threshold_s=180)

    def test_not_stuck_before_threshold(self):
        now = time.time()
        assert not _is_ops_pod_stuck_creating(_pod(age_s=60), now=now, threshold_s=180)

    def test_not_stuck_when_running(self):
        now = time.time()
        assert not _is_ops_pod_stuck_creating(
            _pod(age_s=300, creating=False, phase="Running"),
            now=now,
            threshold_s=180,
        )

    def test_not_stuck_when_unscheduled(self):
        now = time.time()
        assert not _is_ops_pod_stuck_creating(
            _pod(age_s=300, scheduled=False), now=now, threshold_s=180
        )

    def test_stuck_pending_without_container_statuses(self):
        """Multus sandbox DeadlineExceeded can leave statuses empty."""
        now = time.time()
        pod = _pod(age_s=200)
        pod.status.container_statuses = None
        assert _is_ops_pod_stuck_creating(pod, now=now, threshold_s=180)


class TestStuckNodeCounts:
    def test_parse_and_format(self):
        assert _parse_stuck_node_counts({}) == {}
        assert _parse_stuck_node_counts({ANN_STUCK_NODES: "host6:2,host7:1"}) == {
            "host6": 2,
            "host7": 1,
        }
        assert _format_stuck_node_counts({"host6": 2, "host7": 1}) == "host6:2,host7:1"

    def test_not_in_affinity(self):
        aff = _node_hostname_not_in_affinity(["host6", "host7"])
        terms = aff["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][
            "nodeSelectorTerms"
        ]
        expr = terms[0]["matchExpressions"][0]
        assert expr["key"] == "kubernetes.io/hostname"
        assert expr["operator"] == "NotIn"
        assert expr["values"] == ["host6", "host7"]


class TestPlanOpsPodHeal:
    def test_first_hit_no_exclude(self):
        plan = plan_ops_pod_heal({}, "host6")
        assert plan["action"] == "reschedule"
        assert plan["attempts"] == 1
        assert plan["exclude"] == []
        assert plan["annotations"][ANN_ATTEMPTS] == "1"
        assert "host6:1" in plan["annotations"][ANN_STUCK_NODES]

    def test_second_same_node_excludes(self):
        plan = plan_ops_pod_heal(
            {ANN_STUCK_NODES: "host6:1", ANN_ATTEMPTS: "1"}, "host6"
        )
        assert plan["action"] == "reschedule"
        assert plan["attempts"] == 2
        assert plan["exclude"] == ["host6"]

    def test_exhausted_after_max_attempts(self):
        plan = plan_ops_pod_heal(
            {ANN_STUCK_NODES: "host6:2,host7:1", ANN_ATTEMPTS: "3"}, "host7"
        )
        assert plan["action"] == "exhausted"
        assert "host7" in plan["message"]


class TestMaybeHealStuckOpsPod:
    def test_noop_for_troshkad_host(self):
        from app.services.deploy_service import _maybe_heal_stuck_ops_pod

        host = MagicMock(host_type="baremetal")
        assert _maybe_heal_stuck_ops_pod(host, "aabbccdd-1111") == {"action": "ok"}

    def test_forwards_to_heal_on_kubevirt(self):
        from unittest.mock import patch

        from app.services.deploy_service import _maybe_heal_stuck_ops_pod

        host = MagicMock(host_type="kubevirt-cluster")
        with (
            patch(
                "app.services.deploy_service._kubevirt_ops_pod_ctx",
                return_value=(MagicMock(), "ns", "pod"),
            ),
            patch(
                "app.services.ocp.ops_pod_heal.heal_stuck_ops_pod",
                return_value={"action": "reschedule", "detail": "off host6"},
            ) as heal,
        ):
            result = _maybe_heal_stuck_ops_pod(host, "aabbccdd-1111")
        assert result["action"] == "reschedule"
        heal.assert_called_once()


class TestHealStuckOpsPod:
    def test_noop_when_not_stuck(self):
        core = MagicMock()
        core.read_namespaced_pod.return_value = _pod(age_s=30)
        result = heal_stuck_ops_pod(core, "ns", "troshka-aabbccdd-ops", now=time.time())
        assert result["action"] == "ok"
        core.delete_namespaced_pod.assert_not_called()

    def test_deletes_and_recreates_with_updated_annotations(self):
        core = MagicMock()
        core.read_namespaced_pod.return_value = _pod(age_s=200, node="host6")
        result = heal_stuck_ops_pod(core, "ns", "troshka-aabbccdd-ops", now=time.time())
        assert result["action"] == "reschedule"
        assert "host6" in result["detail"]
        core.delete_namespaced_pod.assert_called_once()
        body = _create_body(core)
        assert body["metadata"]["annotations"][ANN_ATTEMPTS] == "1"
        assert "host6:1" in body["metadata"]["annotations"][ANN_STUCK_NODES]
        assert "status" not in body
        assert "uid" not in body["metadata"]
        assert "resourceVersion" not in body["metadata"]
        assert "nodeName" not in body["spec"]

    def test_adds_not_in_after_second_same_node(self):
        core = MagicMock()
        core.read_namespaced_pod.return_value = _pod(
            age_s=200,
            node="host6",
            annotations={ANN_STUCK_NODES: "host6:1", ANN_ATTEMPTS: "1"},
        )
        result = heal_stuck_ops_pod(core, "ns", "troshka-aabbccdd-ops", now=time.time())
        assert result["action"] == "reschedule"
        body = _create_body(core)
        values = body["spec"]["affinity"]["nodeAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"]
        assert "host6" in values

    def test_exhausted_does_not_delete(self):
        core = MagicMock()
        core.read_namespaced_pod.return_value = _pod(
            age_s=200,
            node="host6",
            annotations={ANN_STUCK_NODES: "host6:2,host7:1", ANN_ATTEMPTS: "3"},
        )
        result = heal_stuck_ops_pod(core, "ns", "troshka-aabbccdd-ops", now=time.time())
        assert result["action"] == "exhausted"
        assert "stuck" in result["message"].lower()
        core.delete_namespaced_pod.assert_not_called()
        core.create_namespaced_pod.assert_not_called()
