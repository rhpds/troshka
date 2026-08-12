import kopf
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("troshka-operator")

CRD_GROUP = "troshka.redhat.com"
CRD_VERSION = "v1alpha1"

import handlers.network  # noqa: F401,E402
import handlers.project  # noqa: F401,E402
import handlers.vm  # noqa: F401,E402
import handlers.container  # noqa: F401,E402


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    settings.posting.level = logging.WARNING
    settings.persistence.finalizer = "troshka.redhat.com/finalizer"
    settings.execution.max_workers = 100
    settings.batching.batch_window = 0.5
    logger.info("Troshka operator starting (max_workers=100)")

    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    custom_api = client.CustomObjectsApi()
    core_api = client.CoreV1Api()
    batch_api = client.BatchV1Api()

    # Ensure OBC exists for local pattern storage
    try:
        from helpers.obc import ensure_obc

        obc_config = ensure_obc(custom_api, core_api)
        if obc_config:
            logger.info(
                "OBC ready: bucket=%s", obc_config.get("bucket", "?")
            )
        else:
            logger.warning("OBC created but credentials not yet available")
    except Exception:
        logger.warning("OBC setup skipped (ODF may not be available)")

    try:
        projects = custom_api.list_cluster_custom_object(
            group=CRD_GROUP, version=CRD_VERSION, plural="troshkaprojects"
        )
        for proj in dict(projects).get("items", []):  # type: ignore[call-overload]
            status = proj.get("status", {})
            ns = proj.get("metadata", {}).get("namespace", "")
            cr_name = proj.get("metadata", {}).get("name", "")
            if status.get("phase") != "Error" or not status.get("recertConfig"):
                continue
            recert_cfg = status["recertConfig"]
            rhcos_pvc = recert_cfg.get("rhcosPvc", "")
            vm_part = rhcos_pvc.split("-disk-")[0] if "-disk-" in rhcos_pvc else "vm"
            job_name = f"recert-{vm_part}"
            try:
                batch_api.delete_namespaced_job(
                    name=job_name, namespace=ns, propagation_policy="Background"
                )
            except Exception:
                pass
            custom_api.patch_namespaced_custom_object_status(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=ns,
                plural="troshkaprojects",
                name=cr_name,
                body={
                    "status": {
                        "phase": "Deploying",
                        "error": None,
                        "recertAttempts": 0,
                    }
                },
            )
            logger.info("Startup: retrying failed recert for %s/%s", ns, cr_name)
    except Exception:
        logger.exception("Startup: failed to check recert recovery")
