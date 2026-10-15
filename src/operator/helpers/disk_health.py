"""Surface permanent image failures before VM startup can wait indefinitely."""

from kubernetes.client.exceptions import ApiException

CDI_GROUP = "cdi.kubevirt.io"


def datavolume_error(dv):
    """Return a terminal error, leaving transient connectivity errors retryable."""
    from handlers.vm import _is_terminal_dv_condition

    status = dv.get("status") or {}
    if status.get("phase") == "Succeeded":
        return None
    conditions = status.get("conditions") or []
    terminal = status.get("phase") in ("Failed", "Error") or _is_terminal_dv_condition(
        conditions
    )
    messages = [str(c.get("message") or c.get("reason") or "") for c in conditions]
    if terminal:
        return next((m for m in messages if m), "disk import or clone failed")
    transfer_running = any(
        condition.get("type") == "Running" and condition.get("status") == "True"
        for condition in conditions
    )
    for condition, message in zip(conditions, messages):
        # CDI can retain a previous importer's Bound error while its replacement
        # is successfully transferring. Running is the current importer state;
        # clone-validation/size failures above remain authoritative.
        if condition.get("type") == "Bound" and transfer_running:
            continue
        if condition.get("status") == "False" and any(
            marker in message
            for marker in (
                "NoSuchKey",
                "NoSuchBucket",
                "AccessDenied",
                "InvalidAccessKeyId",
            )
        ):
            return message
    return None


def deployment_disk_error(custom_api, namespace):
    """Inspect project imports/clones and their shared golden source volumes."""
    volumes = custom_api.list_namespaced_custom_object(
        group=CDI_GROUP, version="v1beta1", namespace=namespace, plural="datavolumes"
    ).get("items", [])
    checked = set()
    for dv in volumes:
        name = dv["metadata"]["name"]
        error = datavolume_error(dv)
        if error:
            return f"Disk {namespace}/{name}: {error}"
        if (dv.get("status") or {}).get("phase") == "Succeeded":
            continue
        source = dv.get("spec", {}).get("source", {}).get("pvc", {})
        source_name = source.get("name", "")
        source_namespace = source.get("namespace", namespace)
        key = (source_namespace, source_name)
        if not source_name.startswith("golden-") or key in checked:
            continue
        checked.add(key)
        error = _golden_error(custom_api, source_namespace, source_name)
        if error:
            return f"Disk {name}, source {source_namespace}/{source_name}: {error}"
    return None


def _golden_error(custom_api, namespace, name):
    try:
        golden = custom_api.get_namespaced_custom_object(
            group=CDI_GROUP,
            version="v1beta1",
            namespace=namespace,
            plural="datavolumes",
            name=name,
        )
    except ApiException as exc:
        if exc.status == 404:
            return "source image is missing"
        raise
    return datavolume_error(golden)
