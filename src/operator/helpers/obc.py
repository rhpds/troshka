"""ObjectBucketClaim management for local pattern storage.

Each KubeVirt cluster gets a single OBC (troshka-patterns) backed by the
cluster's Ceph RGW. The OBC auto-provisions a Secret + ConfigMap with
S3 credentials and bucket name.
"""

import base64
import logging

from kubernetes import client
from kubernetes.client.exceptions import ApiException

logger = logging.getLogger(__name__)

OBC_NAME = "troshka-patterns"
RGW_ENDPOINT = (
    "http://rook-ceph-rgw-ocs-storagecluster-cephobjectstore"
    ".openshift-storage.svc:80"
)
RGW_STORAGE_CLASS = "ocs-storagecluster-ceph-rgw"


def ensure_obc(
    custom_api: client.CustomObjectsApi,
    core_api: client.CoreV1Api,
    namespace: str = "troshka-operator",
) -> dict | None:
    """Create OBC if it doesn't exist, return S3 config dict."""
    try:
        custom_api.get_namespaced_custom_object(
            "objectbucket.io",
            "v1alpha1",
            namespace,
            "objectbucketclaims",
            OBC_NAME,
        )
    except ApiException as e:
        if e.status == 404:
            obc = {
                "apiVersion": "objectbucket.io/v1alpha1",
                "kind": "ObjectBucketClaim",
                "metadata": {"name": OBC_NAME, "namespace": namespace},
                "spec": {
                    "generateBucketName": OBC_NAME,
                    "storageClassName": RGW_STORAGE_CLASS,
                },
            }
            custom_api.create_namespaced_custom_object(
                "objectbucket.io",
                "v1alpha1",
                namespace,
                "objectbucketclaims",
                obc,
            )
            logger.info("Created OBC %s in %s", OBC_NAME, namespace)
        else:
            raise
    return get_obc_s3_config(core_api, namespace)


def get_obc_s3_config(
    core_api: client.CoreV1Api,
    namespace: str = "troshka-operator",
) -> dict | None:
    """Read OBC credentials from auto-generated Secret and ConfigMap."""
    try:
        secret = core_api.read_namespaced_secret(OBC_NAME, namespace)
        cm = core_api.read_namespaced_config_map(OBC_NAME, namespace)
    except ApiException:
        return None

    secret_data = secret.data or {}
    cm_data = cm.data or {}
    return {
        "bucket": cm_data.get("BUCKET_NAME", ""),
        "endpoint": RGW_ENDPOINT,
        "region": cm_data.get("BUCKET_REGION", "us-east-1") or "us-east-1",
        "access_key_id": base64.b64decode(
            secret_data.get("AWS_ACCESS_KEY_ID", "")
        ).decode(),
        "secret_access_key": base64.b64decode(
            secret_data.get("AWS_SECRET_ACCESS_KEY", "")
        ).decode(),
        "credentials_secret": OBC_NAME,
    }
