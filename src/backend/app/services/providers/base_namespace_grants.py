"""Deployment-owned mode and read-only checks for the two base namespace grants.

Observed binding shape is not effective-permission or writer-isolation evidence.
"""

from kubernetes import client

from app.core.config import config


class BaseNamespaceGrantsError(RuntimeError):
    """A required externally administered namespace/grant could not be verified."""


class BaseNamespaceGrantsModeError(ValueError):
    """Deployment-owned base grants mode is invalid."""


def base_namespace_grants_mode() -> str:
    mode = config.get("kubevirt.base_namespace_grants_mode", "managed")
    if not isinstance(mode, str) or mode not in ("managed", "external"):
        raise BaseNamespaceGrantsModeError(
            "kubevirt.base_namespace_grants_mode must be managed or external"
        )
    return mode


def _read_resource(read, **kwargs) -> dict:
    """Do not expose API response bodies or transport/credential details."""
    try:
        resource = read(**kwargs)
        result = client.ApiClient().sanitize_for_serialization(resource)
        if not isinstance(result, dict):
            raise TypeError("unexpected resource representation")
        return result
    except Exception:
        raise BaseNamespaceGrantsError(
            "Cannot verify externally administered base namespace grants"
        ) from None


def _metadata_matches(resource: dict, *, name: str, namespace=None) -> bool:
    metadata = resource.get("metadata") or {}
    return (
        isinstance(metadata, dict)
        and metadata.get("name") == name
        and metadata.get("namespace") == namespace
        and not metadata.get("deletionTimestamp")
    )


def verify_existing_namespace(core_api, namespace: str) -> None:
    resource = _read_resource(core_api.read_namespace, name=namespace)
    if not _metadata_matches(resource, name=namespace):
        raise BaseNamespaceGrantsError(
            "Existing namespace is missing, changed or deleting"
        )


def _subjects_match(actual, expected: list) -> bool:
    if (
        not isinstance(actual, list)
        or len(actual) != len(expected)
        or not isinstance(actual[0], dict)
    ):
        return False
    # The only expected subject is a core ServiceAccount. The API may omit its
    # empty apiGroup; a nonempty group or any extra subject/field is rejected.
    subject = dict(actual[0])
    group = subject.pop("apiGroup", "")
    return group in (None, "") and subject == expected[0]


def verify_base_binding(rbac_api, namespace: str, expected: dict) -> None:
    name = expected["metadata"]["name"]
    actual = _read_resource(
        rbac_api.read_namespaced_role_binding, name=name, namespace=namespace
    )
    if not (
        _metadata_matches(actual, name=name, namespace=namespace)
        and actual.get("roleRef") == expected["roleRef"]
        and _subjects_match(actual.get("subjects"), expected["subjects"])
    ):
        raise BaseNamespaceGrantsError(
            "Externally administered base binding does not match"
        )
