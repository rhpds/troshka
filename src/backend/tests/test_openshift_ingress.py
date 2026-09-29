"""Tests for admin IngressController setting and apps-domain resolution."""

from unittest.mock import MagicMock, patch

from app.models.system_config import SystemConfig
from app.services.openshift_ingress import (
    resolve_apps_domain,
    route_public_host,
    uses_non_default_ingress,
)
from app.services.system_settings import (
    DEFAULT_INGRESS_CONTROLLER,
    INGRESS_CONTROLLER_KEY,
    get_ingress_controller_name,
    set_ingress_controller_name,
)
from tests.conftest import TestSession


def _clean_ic_row(db):
    row = db.query(SystemConfig).filter_by(key=INGRESS_CONTROLLER_KEY).first()
    if row:
        db.delete(row)
        db.commit()


def test_default_when_unset():
    db = TestSession()
    try:
        _clean_ic_row(db)
        assert get_ingress_controller_name(db) == DEFAULT_INGRESS_CONTROLLER
    finally:
        db.close()


def test_set_and_get():
    db = TestSession()
    try:
        _clean_ic_row(db)
        assert set_ingress_controller_name(db, "ingress-rhdp-net") == "ingress-rhdp-net"
        assert get_ingress_controller_name(db) == "ingress-rhdp-net"
    finally:
        db.close()


def test_empty_resets_to_default():
    db = TestSession()
    try:
        _clean_ic_row(db)
        set_ingress_controller_name(db, "ingress-rhdp-net")
        assert set_ingress_controller_name(db, "  ") == DEFAULT_INGRESS_CONTROLLER
        assert get_ingress_controller_name(db) == DEFAULT_INGRESS_CONTROLLER
    finally:
        db.close()


def test_update_existing_row():
    db = TestSession()
    try:
        _clean_ic_row(db)
        set_ingress_controller_name(db, "ingress-rhdp-net")
        set_ingress_controller_name(db, "default")
        rows = db.query(SystemConfig).filter_by(key=INGRESS_CONTROLLER_KEY).all()
        assert len(rows) == 1
        assert rows[0].value == "default"
    finally:
        db.close()


def test_route_public_host_shape():
    assert (
        route_public_host("showroom", "troshka-abcd1234", "apps.ocpv06.rhdp.net")
        == "showroom-troshka-abcd1234.apps.ocpv06.rhdp.net"
    )


def test_prefers_named_ingress_controller():
    custom = MagicMock()
    custom.get_namespaced_custom_object.return_value = {
        "status": {"domain": "apps.ocpv06.rhdp.net"}
    }
    with patch(
        "app.services.openshift_ingress.get_ingress_controller_name",
        return_value="ingress-rhdp-net",
    ):
        assert resolve_apps_domain(custom, "") == "apps.ocpv06.rhdp.net"
    kwargs = custom.get_namespaced_custom_object.call_args.kwargs
    assert kwargs["name"] == "ingress-rhdp-net"
    assert kwargs["plural"] == "ingresscontrollers"


def test_non_default_unreadable_returns_empty():
    custom = MagicMock()
    custom.get_namespaced_custom_object.side_effect = Exception("forbidden")
    with patch(
        "app.services.openshift_ingress.get_ingress_controller_name",
        return_value="ingress-rhdp-net",
    ):
        assert resolve_apps_domain(custom, "https://api.x.example.com:6443") == ""
    custom.get_cluster_custom_object.assert_not_called()


def test_default_falls_back_to_cluster_ingress():
    custom = MagicMock()
    custom.get_namespaced_custom_object.side_effect = Exception("forbidden")
    custom.get_cluster_custom_object.return_value = {
        "spec": {"domain": "apps.ocpv06.dal10.infra.demo.redhat.com"}
    }
    with patch(
        "app.services.openshift_ingress.get_ingress_controller_name",
        return_value="default",
    ):
        assert (
            resolve_apps_domain(custom, "") == "apps.ocpv06.dal10.infra.demo.redhat.com"
        )


def test_default_falls_back_to_api_url():
    custom = MagicMock()
    custom.get_namespaced_custom_object.side_effect = Exception("forbidden")
    custom.get_cluster_custom_object.side_effect = Exception("forbidden")
    with patch(
        "app.services.openshift_ingress.get_ingress_controller_name",
        return_value="default",
    ):
        assert (
            resolve_apps_domain(
                custom, "https://api.ocpv06.dal10.infra.demo.redhat.com:6443"
            )
            == "apps.ocpv06.dal10.infra.demo.redhat.com"
        )


def test_uses_non_default_flag():
    with patch(
        "app.services.openshift_ingress.get_ingress_controller_name",
        return_value="ingress-rhdp-net",
    ):
        assert uses_non_default_ingress() is True
    with patch(
        "app.services.openshift_ingress.get_ingress_controller_name",
        return_value="default",
    ):
        assert uses_non_default_ingress() is False


def test_ensure_guid_noop_for_default():
    core = MagicMock()
    with patch(
        "app.services.openshift_ingress.uses_non_default_ingress",
        return_value=False,
    ):
        from app.services.openshift_ingress import ensure_namespace_guid_label

        ensure_namespace_guid_label(core, "ns", "abc")
    core.read_namespace.assert_not_called()
    core.patch_namespace.assert_not_called()


def test_ensure_guid_patches_missing():
    core = MagicMock()
    ns = MagicMock()
    ns.metadata.labels = {"app": "troshka"}
    core.read_namespace.return_value = ns
    with patch(
        "app.services.openshift_ingress.uses_non_default_ingress",
        return_value=True,
    ):
        from app.services.openshift_ingress import ensure_namespace_guid_label

        ensure_namespace_guid_label(core, "troshka-abcd", "abcd1234")
    core.patch_namespace.assert_called_once()
    body = core.patch_namespace.call_args[0][1]
    assert body["metadata"]["labels"]["guid"] == "abcd1234"
