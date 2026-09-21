"""Freeze / unfreeze project Ceph for consistent PVC capture.

Quiesces the namespace-scoped Rook cluster so mon and OSD block devices are idle
before VolumeSnapshot, then restores health on the source project afterward.
"""

from __future__ import annotations

import json
import logging
import time

from kubernetes import client
from kubernetes.client.exceptions import ApiException

logger = logging.getLogger(__name__)

OSD_FREEZE_FLAGS: tuple[str, ...] = (
    "noout",
    "norecover",
    "nobackfill",
    "noscrub",
    "nodeep-scrub",
)
OSD_DEPLOY_LABEL = "app=rook-ceph-osd"
MON_DEPLOY_LABEL = "app=rook-ceph-mon"
FREEZE_STATE_CONFIGMAP = "troshka-ceph-capture-freeze"
_MON_CONTAINER = "mon"
_POD_POLL_INTERVAL_S = 2
_POD_POLL_TIMEOUT_S = 300


def freeze_ceph_for_capture(namespace: str) -> None:
    """Set Ceph safety flags, flush OSDs, and stop mon/OSD pods for capture."""
    core_api, apps_api = _k8s_clients()
    for flag in OSD_FREEZE_FLAGS:
        _ceph_exec(core_api, namespace, ["osd", "set", flag])
    _ceph_exec(core_api, namespace, ["tell", "osd.*", "flush"])

    osd_replicas = _scale_deployments(apps_api, namespace, OSD_DEPLOY_LABEL, 0)
    _wait_for_pods(core_api, namespace, OSD_DEPLOY_LABEL, running=False)
    mon_replicas = _scale_deployments(apps_api, namespace, MON_DEPLOY_LABEL, 0)
    _wait_for_pods(core_api, namespace, MON_DEPLOY_LABEL, running=False)

    _save_freeze_state(core_api, namespace, {**osd_replicas, **mon_replicas})
    logger.info("Ceph frozen for capture in %s", namespace)


def unfreeze_ceph_after_capture(namespace: str) -> None:
    """Restart mon/OSD pods and clear freeze flags so the source cluster recovers."""
    core_api, apps_api = _k8s_clients()
    saved = _load_freeze_state(core_api, namespace)
    if saved:
        _restore_deployments(apps_api, namespace, saved)
    else:
        logger.warning(
            "No freeze state in %s/%s — scaling mon/OSD deployments to 1",
            namespace,
            FREEZE_STATE_CONFIGMAP,
        )
        _scale_deployments(apps_api, namespace, MON_DEPLOY_LABEL, 1)
        _scale_deployments(apps_api, namespace, OSD_DEPLOY_LABEL, 1)

    _wait_for_pods(core_api, namespace, MON_DEPLOY_LABEL, running=True)
    _wait_for_pods(core_api, namespace, OSD_DEPLOY_LABEL, running=True)

    for flag in reversed(OSD_FREEZE_FLAGS):
        _ceph_exec(core_api, namespace, ["osd", "unset", flag])

    _clear_freeze_state(core_api, namespace)
    logger.info("Ceph unfrozen after capture in %s", namespace)


def _k8s_clients() -> tuple[client.CoreV1Api, client.AppsV1Api]:
    return client.CoreV1Api(), client.AppsV1Api()


def _ceph_exec(core_api: client.CoreV1Api, namespace: str, args: list[str]) -> str:
    from kubernetes.stream import stream

    mon_pod = _running_mon_pod_name(core_api, namespace)
    command = ["ceph", *args]
    logger.debug("Ceph exec in %s/%s: %s", namespace, mon_pod, " ".join(command))
    resp = stream(
        core_api.connect_get_namespaced_pod_exec,
        mon_pod,
        namespace,
        command=command,
        container=_MON_CONTAINER,
        stderr=True,
        stdout=True,
        stdin=False,
        tty=False,
        _preload_content=False,
    )
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    while resp.is_open():
        resp.update(timeout=30)
        if resp.peek_stdout():
            stdout_chunks.append(resp.read_stdout())
        if resp.peek_stderr():
            stderr_chunks.append(resp.read_stderr())
    resp.close()
    exit_code = resp.returncode
    stdout = "".join(stdout_chunks).strip()
    stderr = "".join(stderr_chunks).strip()
    if exit_code:
        detail = stderr or stdout or f"exit {exit_code}"
        raise RuntimeError(f"ceph {' '.join(args)} failed in {namespace}: {detail}")
    return stdout


def _running_mon_pod_name(core_api: client.CoreV1Api, namespace: str) -> str:
    pods = core_api.list_namespaced_pod(
        namespace=namespace,
        label_selector=MON_DEPLOY_LABEL,
    )
    for pod in pods.items:
        if pod.status and pod.status.phase == "Running" and pod.metadata and pod.metadata.name:
            return pod.metadata.name
    raise RuntimeError(f"no running rook-ceph-mon pod in {namespace}")


def _scale_deployments(
    apps_api: client.AppsV1Api,
    namespace: str,
    label_selector: str,
    replicas: int,
) -> dict[str, int]:
    """Scale matching deployments and return their pre-patch replica counts."""
    prior: dict[str, int] = {}
    deps = apps_api.list_namespaced_deployment(
        namespace=namespace,
        label_selector=label_selector,
    )
    for dep in deps.items or []:
        name = dep.metadata.name
        if not name:
            continue
        prior[name] = dep.spec.replicas if dep.spec and dep.spec.replicas is not None else 1
        apps_api.patch_namespaced_deployment(
            name=name,
            namespace=namespace,
            body={"spec": {"replicas": replicas}},
        )
        logger.info(
            "Scaled deployment %s in %s to replicas=%s (was %s)",
            name,
            namespace,
            replicas,
            prior[name],
        )
    return prior


def _restore_deployments(
    apps_api: client.AppsV1Api,
    namespace: str,
    deployment_replicas: dict[str, int],
) -> None:
    for name, replicas in deployment_replicas.items():
        apps_api.patch_namespaced_deployment(
            name=name,
            namespace=namespace,
            body={"spec": {"replicas": replicas}},
        )
        logger.info("Restored deployment %s in %s to replicas=%s", name, namespace, replicas)


def _wait_for_pods(
    core_api: client.CoreV1Api,
    namespace: str,
    label_selector: str,
    *,
    running: bool,
) -> None:
    deadline = time.time() + _POD_POLL_TIMEOUT_S
    while time.time() < deadline:
        pods = core_api.list_namespaced_pod(
            namespace=namespace,
            label_selector=label_selector,
        )
        phases = [
            (pod.metadata.name, pod.status.phase if pod.status else None)
            for pod in pods.items or []
            if pod.metadata and pod.metadata.name
        ]
        if running:
            if phases and all(phase == "Running" for _, phase in phases):
                return
        elif not phases or all(phase not in ("Running", "Pending") for _, phase in phases):
            return
        time.sleep(_POD_POLL_INTERVAL_S)
    state = ", ".join(f"{n}={p}" for n, p in phases) if phases else "none"
    want = "Running" if running else "stopped"
    raise TimeoutError(
        f"timed out waiting for pods ({label_selector}) to be {want} in {namespace}: {state}"
    )


def _save_freeze_state(
    core_api: client.CoreV1Api,
    namespace: str,
    deployment_replicas: dict[str, int],
) -> None:
    body = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=FREEZE_STATE_CONFIGMAP, namespace=namespace),
        data={"deployments": json.dumps(deployment_replicas)},
    )
    try:
        core_api.create_namespaced_config_map(namespace=namespace, body=body)
    except ApiException as e:
        if e.status != 409:
            raise
        core_api.patch_namespaced_config_map(
            name=FREEZE_STATE_CONFIGMAP,
            namespace=namespace,
            body=body,
        )


def _load_freeze_state(
    core_api: client.CoreV1Api,
    namespace: str,
) -> dict[str, int] | None:
    try:
        cm = core_api.read_namespaced_config_map(
            name=FREEZE_STATE_CONFIGMAP,
            namespace=namespace,
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise
    raw = (cm.data or {}).get("deployments")
    if not raw:
        return None
    return json.loads(raw)


def _clear_freeze_state(core_api: client.CoreV1Api, namespace: str) -> None:
    try:
        core_api.delete_namespaced_config_map(
            name=FREEZE_STATE_CONFIGMAP,
            namespace=namespace,
        )
    except ApiException as e:
        if e.status != 404:
            raise
