"""Periodic refresh of cluster capability limits for the Troshka UI."""

import logging
import threading
import time

import kopf
from kubernetes import client, config

logger = logging.getLogger("troshka-operator")

_REFRESH_INTERVAL_SECS = 300


def _capability_refresh_loop():
    from helpers.cluster_capabilities import refresh_cluster_capabilities

    while True:
        try:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            custom_api = client.CustomObjectsApi()
            core_api = client.CoreV1Api()
            refresh_cluster_capabilities(custom_api, core_api)
        except Exception as exc:
            logger.warning("Cluster capability refresh failed: %s", exc)
        time.sleep(_REFRESH_INTERVAL_SECS)


@kopf.on.startup()
def start_capability_refresh(**_):
    thread = threading.Thread(
        target=_capability_refresh_loop,
        daemon=True,
        name="troshka-capability-refresh",
    )
    thread.start()
    logger.info("Cluster capability refresh thread started")
