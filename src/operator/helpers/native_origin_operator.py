"""Read-only runtime checks for a backend-qualified singleton drain profile.

The backend must independently pin these values in retained drain authority.
This verifies runtime shape only; the marker is not deletion authority.
"""

from helpers import managed_power as power


def qualify(api, pins, identity):
    power.require(isinstance(pins, dict) and pins.get("version") == "single-operator-recreate-v1",
                  "origin_operator_qualification_missing")
    namespace = identity["namespace"]
    power.require(api.get("Namespace", None, "kube-system")["metadata"]["uid"] == pins.get("clusterUid"),
                  "origin_operator_cluster_changed")
    deployment = api.get("Deployment", namespace, pins["deploymentName"])
    _deployment(deployment, pins)
    selector = deployment["spec"]["selector"]
    power.require(selector.get("matchLabels") and not selector.get("matchExpressions"), "origin_operator_selector_unsupported")
    pods = [p for p in api.rows("Pod", namespace) if all(p["metadata"].get("labels", {}).get(k) == v
                                                     for k, v in selector["matchLabels"].items())]
    live = [p for p in pods if p.get("status", {}).get("phase") not in {"Succeeded", "Failed"}]
    power.require(len(live) == 1 and live[0]["metadata"].get("uid") == identity["uid"]
                  and not live[0]["metadata"].get("deletionTimestamp"), "origin_operator_overlap_or_replacement")
    pod = live[0]
    _owner(api, pod, deployment)
    containers = pod.get("status", {}).get("containerStatuses", [])
    power.require(len(containers) == 1 and containers[0].get("name") == "operator", "origin_operator_container_set_changed")
    container = containers[0]
    power.require(container.get("ready") and container.get("state", {}).get("running")
                  and container.get("containerID") == identity["containerId"]
                  and container.get("imageID") == pins.get("imageId"), "origin_operator_container_changed")
    power.require(api.get("Deployment", namespace, pins["deploymentName"]) == deployment,
                  "origin_operator_rollout_changed")


def _deployment(value, pins):
    meta, spec, status = value["metadata"], value["spec"], value.get("status", {})
    power.require(meta["uid"] == pins.get("deploymentUid") and not meta.get("deletionTimestamp")
                  and spec.get("replicas") == 1 and spec.get("strategy", {}).get("type") == "Recreate"
                  and status.get("observedGeneration") == meta.get("generation")
                  and all(status.get(k) == 1 for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")),
                  "origin_operator_rollout_not_singleton")


def _owner(api, pod, deployment):
    refs = pod["metadata"].get("ownerReferences", [])
    power.require(len(refs) == 1 and refs[0].get("kind") == "ReplicaSet", "origin_operator_pod_owner_unavailable")
    rs = api.get("ReplicaSet", pod["metadata"]["namespace"], refs[0]["name"])
    power.require(rs["metadata"]["uid"] == refs[0]["uid"], "origin_operator_replicaset_changed")
    refs = rs["metadata"].get("ownerReferences", [])
    power.require(len(refs) == 1 and refs[0].get("kind") == "Deployment"
                  and refs[0].get("uid") == deployment["metadata"]["uid"], "origin_operator_deployment_owner_changed")
