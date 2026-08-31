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


def test_comparison_tag_uses_rolling_tag_for_commit_sha():
    assert app_updater._is_commit_sha_tag("d196e20f") is True
    assert app_updater._is_commit_sha_tag("9baa7735") is True
    assert app_updater._is_commit_sha_tag("latest") is False
    assert app_updater._is_commit_sha_tag("production") is False
    assert app_updater._comparison_tag("d196e20f") == "latest"
    assert app_updater._comparison_tag("production") == "production"


def test_build_image_snapshot_commit_pin_compares_against_latest(monkeypatch):
    monkeypatch.setattr(app_updater, "_read_deployment_tag", lambda suffix: "d196e20f")
    monkeypatch.setattr(
        app_updater,
        "_read_own_digests",
        lambda: {"backend": "sha256:old", "frontend": "sha256:bbb"},
    )
    monkeypatch.setattr(app_updater, "_read_rolling_out", lambda: False)
    seen_tags = []

    def _fake_fetch(image, tag):
        seen_tags.append(tag)
        if image.endswith("troshka-backend"):
            return "sha256:NEW"
        return "sha256:bbb"

    monkeypatch.setattr(app_updater, "_fetch_registry_digest", _fake_fetch)
    snap = app_updater._build_image_snapshot()
    assert seen_tags == ["latest", "latest"]
    assert snap["up_to_date"] is False
    assert snap["components"]["backend"]["compare_tag"] == "latest"
    assert snap["components"]["backend"]["deploy_tag"] == "d196e20f"


def test_apply_update_image_repins_commit_sha_deployments(monkeypatch):
    _reset()
    monkeypatch.setattr(app_updater, "resolve_mode", lambda: "image")
    monkeypatch.setattr(app_updater, "_read_deployment_tag", lambda suffix: "d196e20f")
    repinned = []
    restarted = []
    monkeypatch.setattr(
        app_updater,
        "_patch_deployment_image",
        lambda suffix, image: repinned.append((suffix, image)),
    )
    monkeypatch.setattr(
        app_updater, "_patch_restart", lambda name: restarted.append(name)
    )
    result = app_updater.apply_update()
    assert result == {"status": "rolling_out"}
    assert len(repinned) == 2
    assert restarted == []


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
    monkeypatch.setattr(app_updater, "_dev_stale_key", lambda: "dev:abc123")
    status = app_updater.get_status()
    assert status["mode"] == "dev"
    assert status["up_to_date"] is False
    assert status["stale_key"] == "dev:abc123"
    assert status["rolling_out"] is False


def test_dev_stale_key_reflects_source_hash():
    key = app_updater._dev_stale_key()
    assert key.startswith("dev:")
    assert len(key) > 4


def test_dev_up_to_date_true_when_hash_matches():
    # Live source hash equals the snapshot captured at import -> up to date.
    assert app_updater._dev_up_to_date() is True


def test_dev_up_to_date_false_when_source_changed(monkeypatch):
    # A different snapshot hash means the running code no longer matches source.
    monkeypatch.setattr(app_updater, "_SOURCE_HASH_AT_START", "different-hash")
    assert app_updater._dev_up_to_date() is False


def test_compute_source_hash_is_stable():
    # Content-based: deterministic for an unchanged tree, no mtime dependence.
    assert app_updater._compute_source_hash() == app_updater._compute_source_hash()


def test_selector_from_match_labels():
    # Must handle the dedicated-CI label convention, not just app=.
    assert (
        app_updater._selector_from_match_labels(
            {"app.kubernetes.io/name": "troshka-backend"}
        )
        == "app.kubernetes.io/name=troshka-backend"
    )
    assert app_updater._selector_from_match_labels({}) == ""
    assert app_updater._selector_from_match_labels(None) == ""


def test_config_accessors_tolerate_missing_block(monkeypatch):
    # Deployed ConfigMaps often omit the app_update block; accessors must return
    # defaults, not raise AttributeError on the missing top-level key.
    monkeypatch.setattr(app_updater.config, "get", lambda k, d=None: d)
    assert app_updater._configured_mode() == "auto"
    assert app_updater._registry() == "quay.io"
    assert app_updater._repo() == "redhat-gpte"
    assert app_updater._tag() == "production"
    assert app_updater._poll_interval() == 300


def test_resolve_mode_image_when_config_block_missing(monkeypatch):
    # A missing app_update block must NOT collapse to "disabled" — that would
    # silently turn the feature off on every deployed instance.
    _reset()
    monkeypatch.setattr(app_updater.config, "get", lambda k, d=None: d)
    monkeypatch.setattr(app_updater, "_oauth_enabled", lambda: True)
    monkeypatch.setattr(app_updater, "_read_own_deployment_labels", lambda: {})
    assert app_updater.resolve_mode() == "image"


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


def test_apply_update_dev_spawns_restart(monkeypatch, tmp_path):
    _reset()
    monkeypatch.setattr(app_updater, "resolve_mode", lambda: "dev")
    monkeypatch.setattr(app_updater, "_RESTART_LOCK", tmp_path / "restart.lock")
    calls = {}

    def fake_posix_spawn(path, argv, env, file_actions=None, setsid=False):
        calls["path"] = path
        calls["args"] = argv
        calls["detached"] = setsid
        return 4242

    # Production prefers os.posix_spawn (avoids fork() abort on macOS), falling
    # back to subprocess.Popen only when posix_spawn is unavailable.
    monkeypatch.setattr(app_updater.os, "posix_spawn", fake_posix_spawn)
    monkeypatch.setattr(app_updater.os, "open", lambda *a, **k: 3)
    monkeypatch.setattr(app_updater.os, "close", lambda fd: None)
    result = app_updater.apply_update(initiated_by="admin@test", client_ip="127.0.0.1")
    assert result == {"status": "restarting"}
    assert calls["args"][0].endswith("dev-services.sh")
    assert calls["args"][1:] == ["restart", "backend"]
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
        "app.api.updates.app_updater.apply_update",
        lambda **kwargs: {"status": "restarting"},
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
