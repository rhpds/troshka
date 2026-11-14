"""Runtime helper comparison preserves exact Kubernetes token injection checks."""

from copy import deepcopy

import pytest

from app.services import native_managed_power as native
from tests.test_native_initialization_observer import inject_service_account


@pytest.mark.parametrize(
    "change", [None, "mount", "source", "extra", "command", "disabled"]
)
def test_helper_runtime_token_projection(change):
    expected = {
        "serviceAccountName": "troshka-vnc",
        "containers": [{"name": "vnc", "image": "vnc:test"}],
    }
    pod = {"spec": deepcopy(expected)}
    inject_service_account(pod)
    if change == "mount":
        pod["spec"]["containers"][0]["volumeMounts"][0]["readOnly"] = False
    elif change == "source":
        pod["spec"]["volumes"][0]["projected"]["sources"][1]["configMap"][
            "name"
        ] = "foreign"
    elif change == "extra":
        pod["spec"]["volumes"].append({"name": "foreign", "hostPath": {"path": "/"}})
    elif change == "command":
        pod["spec"]["containers"][0]["command"] = ["unexpected"]
    elif change == "disabled":
        expected["automountServiceAccountToken"] = False
        pod["spec"]["automountServiceAccountToken"] = False
    original = deepcopy(pod)
    pod.update(
        metadata={"ownerReferences": [{"uid": "rs"}]},
        status={
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    )
    deployment = {
        "metadata": {"uid": "deployment", "generation": 1},
        "spec": {"template": {"spec": expected}},
        "status": {
            "observedGeneration": 1,
            "replicas": 1,
            "readyReplicas": 1,
            "availableReplicas": 1,
            "updatedReplicas": 1,
        },
    }
    rs = {
        "metadata": {"uid": "rs", "ownerReferences": [{"uid": "deployment"}]},
        "spec": {"replicas": 1, "template": {"spec": deepcopy(expected)}},
    }
    rows = {"ReplicaSet": [rs], "Pod": [pod]}
    module = native.helpers()
    if change is None:
        native.verify_helper_running(deployment, rows, 1, module)
    else:
        with pytest.raises(
            module.ManagedPowerError, match="managed_helper_configuration_changed"
        ):
            native.verify_helper_running(deployment, rows, 1, module)
    assert pod["spec"] == original["spec"]
