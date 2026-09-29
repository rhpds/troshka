"""Tests for the golden-cache crashloop watchdog (reaper).

The reaper periodically deletes golden DataVolumes in troshka-cache whose CDI
importer is crash-looping on a dead source (e.g. a NoSuchKey 404), which would
otherwise linger and crash-loop indefinitely (a real case ran 40 days / 11k
restarts). The reap DECISION is a pure predicate, unit-tested here.
"""

from handlers.project import _should_reap_golden

_GRACE = 900


def test_reap_crashlooping_old_unreferenced():
    # The target case: not Succeeded, importer crash-looping, older than grace,
    # not referenced by any active clone → reap.
    assert _should_reap_golden("ImportInProgress", True, 1000, False, _GRACE) is True


def test_keep_succeeded_golden():
    assert _should_reap_golden("Succeeded", True, 1000, False, _GRACE) is False


def test_keep_young_golden_within_grace():
    # A just-created golden legitimately importing must never be reaped.
    assert _should_reap_golden("ImportInProgress", True, 100, False, _GRACE) is False


def test_keep_referenced_by_active_clone():
    # Never yank a golden a live deploy is cloning from.
    assert _should_reap_golden("ImportInProgress", True, 1000, True, _GRACE) is False


def test_keep_healthy_importing_golden():
    # Old but importer NOT crash-looping (slow but progressing) → keep.
    assert _should_reap_golden("ImportInProgress", False, 1000, False, _GRACE) is False


def test_reap_stuck_goldens_deletes_only_dead_golden(monkeypatch):
    """Orchestration: a crash-looping, old, unreferenced golden is deleted; a
    Succeeded golden and a non-golden DV are left alone."""
    import time
    from unittest.mock import MagicMock

    import handlers.project as p

    old = time.time() - 3600  # 1h ago (past 15m grace)

    def _dv(name, phase, created_ts):
        from datetime import datetime, timezone

        iso = datetime.fromtimestamp(created_ts, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return {
            "metadata": {"name": name, "creationTimestamp": iso},
            "status": {"phase": phase},
        }

    custom_api = MagicMock()
    custom_api.list_namespaced_custom_object.return_value = {
        "items": [
            _dv("golden-dead", "ImportInProgress", old),
            _dv("golden-ok", "Succeeded", old),
            _dv("vm-123-disk-abc", "ImportInProgress", old),  # not a golden
        ]
    }
    # No clone references any golden.
    custom_api.list_cluster_custom_object.return_value = {"items": []}
    core_api = MagicMock()

    # Only golden-dead's importer is crash-looping.
    monkeypatch.setattr(
        p,
        "_golden_importer_crashlooping",
        lambda core, ns, name: name == "golden-dead",
    )
    deleted = []
    monkeypatch.setattr(
        p,
        "delete_golden_import",
        lambda ca, co, ns, name: deleted.append(name),
        raising=False,
    )
    # delete_golden_import is imported inside reap_stuck_goldens from helpers;
    # patch there too.
    import helpers.kubevirt as kv

    monkeypatch.setattr(
        kv, "delete_golden_import", lambda ca, co, ns, name: deleted.append(name)
    )

    reaped = p.reap_stuck_goldens(custom_api, core_api, now=time.time())
    assert reaped == 1
    assert deleted == ["golden-dead"]


def test_container_status_crashlooping_by_reason():
    from handlers.project import _container_status_crashlooping
    from types import SimpleNamespace

    waiting = SimpleNamespace(reason="CrashLoopBackOff")
    state = SimpleNamespace(waiting=waiting)
    cs = SimpleNamespace(restart_count=0, state=state)
    assert _container_status_crashlooping(cs) is True


def test_container_status_crashlooping_by_restarts():
    from handlers.project import _container_status_crashlooping
    from types import SimpleNamespace

    cs = SimpleNamespace(restart_count=3, state=None)
    assert _container_status_crashlooping(cs) is True


def test_container_status_not_crashlooping():
    from handlers.project import _container_status_crashlooping
    from types import SimpleNamespace

    cs = SimpleNamespace(restart_count=1, state=None)
    assert _container_status_crashlooping(cs) is False


def test_resolve_golden_s3_config_obc():
    from handlers.project import _resolve_golden_s3_config

    obc = {"bucket": "obc", "credentialsSecret": "s3-obc-credentials"}  # pragma: allowlist secret
    s3 = {"obcConfig": obc}
    cfg, secret = _resolve_golden_s3_config(
        {"patternImage": {"source": "obc"}}, s3, {}, False
    )
    assert cfg is obc
    assert secret == "s3-obc-credentials"  # pragma: allowlist secret


def test_resolve_golden_s3_config_central():
    from handlers.project import _resolve_golden_s3_config

    central = {"bucket": "central"}
    cfg, secret = _resolve_golden_s3_config({}, {"bucket": "local"}, central, True)
    assert cfg is central
    assert secret == "s3-central-credentials"  # pragma: allowlist secret


def test_resolve_golden_s3_config_default():
    from handlers.project import _resolve_golden_s3_config

    local = {"bucket": "local"}
    cfg, secret = _resolve_golden_s3_config({}, local, None, False)
    assert cfg is local
    assert secret == "s3-credentials"  # pragma: allowlist secret
