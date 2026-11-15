"""Collection identity uses its protected transport, never provider credentials."""

import time

import pytest

from app.schemas.kubevirt_storage import CollectionLimits
from app.services.native_provider_reader import ProviderReader


def transport(insecure=False):
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": "cluster",
                "cluster": {
                    "server": "https://collector.invalid:6443",
                    "insecure-skip-tls-verify": insecure,
                },
            }
        ],
        "users": [{"name": "reader", "user": {"token": "test-collector-token"}}],
        "contexts": [
            {"name": "context", "context": {"cluster": "cluster", "user": "reader"}}
        ],
        "current-context": "context",
    }


def test_reader_uses_exact_protected_transport():
    reader = ProviderReader.from_kubeconfig(
        "provider-id", transport(), CollectionLimits(), time.monotonic() + 30
    )
    try:
        assert reader.provider_id == "provider-id"
        assert reader.api.configuration.host == "https://collector.invalid:6443"
        assert reader.api.configuration.verify_ssl is True
        assert (
            reader.api.configuration.auth_settings()["BearerToken"]["value"]
            == "Bearer test-collector-token"
        )
    finally:
        reader.close()


def test_reader_rejects_insecure_collection_transport():
    with pytest.raises(ValueError, match="TLS"):
        ProviderReader.from_kubeconfig(
            "provider-id", transport(True), CollectionLimits(), time.monotonic() + 30
        )
