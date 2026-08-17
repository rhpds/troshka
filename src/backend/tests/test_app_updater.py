from app.services import app_updater


def _reset():
    app_updater._resolved_mode = None


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
