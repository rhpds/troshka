import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.core.auth import get_current_user
from app.core.database import get_db
from app.main import app
from app.services import app_updater
from tests.conftest import get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)


def _reset():
    app_updater._resolved_mode = None
    app_updater._snapshot = {}


def test_is_argo_managed_true():
    assert (
        app_updater._is_argo_managed({"argocd.argoproj.io/instance": "troshka"}) is True
    )


def test_is_argo_managed_false():
    assert app_updater._is_argo_managed({"app": "troshka-backend"}) is False


def test_resolve_mode_explicit_override(monkeypatch):
    _reset()
    monkeypatch.setattr(app_updater, "_configured_mode", lambda: "disabled")
    assert app_updater.resolve_mode() == "disabled"


def test_resolve_mode_dev_when_oauth_off(monkeypatch):
    _reset()
    monkeypatch.setattr(app_updater, "_configured_mode", lambda: "auto")
    monkeypatch.setattr(app_updater, "_oauth_enabled", lambda: False)
    assert app_updater.resolve_mode() == "dev"


def test_resolve_mode_image_when_deployed_no_argo(monkeypatch):
    _reset()
    monkeypatch.setattr(app_updater, "_configured_mode", lambda: "auto")
    monkeypatch.setattr(app_updater, "_oauth_enabled", lambda: True)
    monkeypatch.setattr(app_updater, "_read_own_deployment_labels", lambda: {})
    assert app_updater.resolve_mode() == "image"


def test_resolve_mode_disabled_when_argo(monkeypatch):
    _reset()
    monkeypatch.setattr(app_updater, "_configured_mode", lambda: "auto")
    monkeypatch.setattr(app_updater, "_oauth_enabled", lambda: True)
    monkeypatch.setattr(
        app_updater,
        "_read_own_deployment_labels",
        lambda: {"argocd.argoproj.io/instance": "troshka"},
    )
    assert app_updater.resolve_mode() == "disabled"


def test_resolve_mode_disabled_and_uncached_on_label_error(monkeypatch):
    _reset()
    monkeypatch.setattr(app_updater, "_configured_mode", lambda: "auto")
    monkeypatch.setattr(app_updater, "_oauth_enabled", lambda: True)

    def _boom():
        raise RuntimeError("in-cluster API unavailable")

    monkeypatch.setattr(app_updater, "_read_own_deployment_labels", _boom)
    assert app_updater.resolve_mode() == "disabled"
    # Must NOT cache the failure-derived mode; next call should recompute.
    assert app_updater._resolved_mode is None


def test_build_image_snapshot_up_to_date(monkeypatch):
    monkeypatch.setattr(
        app_updater,
        "_read_own_digests",
        lambda: {"backend": "sha256:aaa", "frontend": "sha256:bbb"},
    )
    monkeypatch.setattr(app_updater, "_read_rolling_out", lambda: False)
    digests = {"troshka-backend": "sha256:aaa", "troshka-frontend": "sha256:bbb"}
    monkeypatch.setattr(
        app_updater,
        "_fetch_registry_digest",
        lambda image, tag: digests[image.rsplit("/", 1)[1]],
    )
    snap = app_updater._build_image_snapshot()
    assert snap["up_to_date"] is True
    assert snap["components"]["backend"]["current"] == "sha256:aaa"


def test_build_image_snapshot_backend_outdated(monkeypatch):
    monkeypatch.setattr(
        app_updater,
        "_read_own_digests",
        lambda: {"backend": "sha256:old", "frontend": "sha256:bbb"},
    )
    monkeypatch.setattr(app_updater, "_read_rolling_out", lambda: False)
    digests = {"troshka-backend": "sha256:NEW", "troshka-frontend": "sha256:bbb"}
    monkeypatch.setattr(
        app_updater,
        "_fetch_registry_digest",
        lambda image, tag: digests[image.rsplit("/", 1)[1]],
    )
    snap = app_updater._build_image_snapshot()
    assert snap["up_to_date"] is False


def test_build_image_snapshot_missing_digest_not_flagged(monkeypatch):
    # transient failure -> registry digest None -> must NOT flag an update
    monkeypatch.setattr(
        app_updater,
        "_read_own_digests",
        lambda: {"backend": "sha256:aaa", "frontend": "sha256:bbb"},
    )
    monkeypatch.setattr(app_updater, "_read_rolling_out", lambda: False)
    monkeypatch.setattr(app_updater, "_fetch_registry_digest", lambda image, tag: None)
    snap = app_updater._build_image_snapshot()
    assert snap["up_to_date"] is True


def test_extract_tag_from_ref_cases():
    assert (
        app_updater._extract_tag_from_ref("quay.io/redhat-gpte/troshka-backend:latest")
        == "latest"
    )
    assert (
        app_updater._extract_tag_from_ref(
            "quay.io/redhat-gpte/troshka-backend:production"
        )
        == "production"
    )
    # Registry port colon must not be mistaken for a tag.
    assert (
        app_updater._extract_tag_from_ref("quay.io:443/x/troshka-backend:latest")
        == "latest"
    )
    # Digest pin -> no tag.
    assert app_updater._extract_tag_from_ref("quay.io/x/name@sha256:abc") is None
    # No tag at all.
    assert app_updater._extract_tag_from_ref("quay.io/x/name") is None


def test_read_deployment_tag_parses_latest(monkeypatch):
    class _Container:
        image = "quay.io/redhat-gpte/troshka-backend:latest"

    class _PodSpec:
        containers = [_Container()]

    class _Template:
        spec = _PodSpec()

    class _DepSpec:
        template = _Template()

    class _Dep:
        spec = _DepSpec()

    class _FakeApps:
        def read_namespaced_deployment(self, name, namespace):
            return _Dep()

    fake_client = SimpleNamespace(AppsV1Api=lambda: _FakeApps())
    fake_config = SimpleNamespace(load_incluster_config=lambda: None)
    fake_kubernetes = SimpleNamespace(client=fake_client, config=fake_config)
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)
    monkeypatch.setitem(sys.modules, "kubernetes.client", fake_client)
    monkeypatch.setitem(sys.modules, "kubernetes.config", fake_config)
    assert app_updater._read_deployment_tag("troshka-backend") == "latest"


def test_build_image_snapshot_uses_deployment_tag(monkeypatch):
    monkeypatch.setattr(app_updater, "_read_deployment_tag", lambda suffix: "latest")
    monkeypatch.setattr(
        app_updater,
        "_read_own_digests",
        lambda: {"backend": "sha256:aaa", "frontend": "sha256:bbb"},
    )
    monkeypatch.setattr(app_updater, "_read_rolling_out", lambda: False)
    seen_tags = []

    def _fake_fetch(image, tag):
        seen_tags.append(tag)
        return "sha256:aaa"

    monkeypatch.setattr(app_updater, "_fetch_registry_digest", _fake_fetch)
    app_updater._build_image_snapshot()
    assert seen_tags == ["latest", "latest"]


def test_fetch_registry_digest_none_when_header_missing(monkeypatch):
    # No Docker-Content-Digest header -> must NOT fall back to config digest.
    class _FakeHeaders:
        def get(self, key):
            return None

    class _FakeResp:
        headers = _FakeHeaders()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        app_updater.urllib.request, "urlopen", lambda req, timeout=15: _FakeResp()
    )
    result = app_updater._fetch_registry_digest(
        "redhat-gpte/troshka-backend", "production"
    )
    assert result is None


def test_get_status_disabled(monkeypatch):
    _reset()
    monkeypatch.setattr(app_updater, "resolve_mode", lambda: "disabled")
    assert app_updater.get_status() == {"mode": "disabled"}


def test_get_status_dev(monkeypatch):
    _reset()
    monkeypatch.setattr(app_updater, "resolve_mode", lambda: "dev")
    monkeypatch.setattr(app_updater, "_dev_up_to_date", lambda: False)
    status = app_updater.get_status()
    assert status["mode"] == "dev"
    assert status["up_to_date"] is False
    assert status["rolling_out"] is False


def test_get_status_image(monkeypatch):
    _reset()
    monkeypatch.setattr(app_updater, "resolve_mode", lambda: "image")
    app_updater._snapshot = {
        "up_to_date": False,
        "rolling_out": False,
        "components": {},
    }
    status = app_updater.get_status()
    assert status["mode"] == "image"
    assert status["up_to_date"] is False


def test_apply_update_dev_spawns_restart(monkeypatch):
    _reset()
    monkeypatch.setattr(app_updater, "resolve_mode", lambda: "dev")
    calls = {}

    class FakePopen:
        def __init__(self, args, cwd=None, start_new_session=False):
            calls["args"] = args
            calls["cwd"] = cwd
            calls["detached"] = start_new_session

    monkeypatch.setattr(app_updater.subprocess, "Popen", FakePopen)
    result = app_updater.apply_update()
    assert result == {"status": "restarting"}
    assert calls["args"] == ["./dev-services.sh", "restart", "backend"]
    assert calls["detached"] is True


def test_apply_update_image_patches_both_deployments(monkeypatch):
    _reset()
    monkeypatch.setattr(app_updater, "resolve_mode", lambda: "image")
    patched = []
    monkeypatch.setattr(
        app_updater, "_patch_restart", lambda name: patched.append(name)
    )
    result = app_updater.apply_update()
    assert result == {"status": "rolling_out"}
    assert set(patched) == {"troshka-backend", "troshka-frontend"}


def test_status_endpoint_returns_snapshot(monkeypatch):
    monkeypatch.setattr(
        "app.services.app_updater.get_status",
        lambda: {
            "mode": "dev",
            "up_to_date": False,
            "rolling_out": False,
            "components": {},
        },
    )
    resp = client.get("/api/v1/update/status")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "dev"


def test_apply_endpoint_dispatches(monkeypatch):
    monkeypatch.setattr("app.services.app_updater.resolve_mode", lambda: "dev")
    monkeypatch.setattr(
        "app.services.app_updater.apply_update", lambda: {"status": "restarting"}
    )
    resp = client.post("/api/v1/update/apply")
    assert resp.status_code == 200
    assert resp.json()["status"] == "restarting"


def test_apply_endpoint_disabled_returns_400(monkeypatch):
    monkeypatch.setattr("app.services.app_updater.resolve_mode", lambda: "disabled")
    resp = client.post("/api/v1/update/apply")
    assert resp.status_code == 400


def test_status_endpoint_forbidden_for_non_admin():
    # require_role("admin") calls get_current_user; override it to a plain user.
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        role="user", email="user@example.com"
    )
    try:
        resp = client.get("/api/v1/update/status")
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_apply_endpoint_forbidden_for_non_admin():
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        role="user", email="user@example.com"
    )
    try:
        resp = client.post("/api/v1/update/apply")
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)
