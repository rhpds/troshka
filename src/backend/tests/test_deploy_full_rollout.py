"""Exercise the release gate against fake registry and Kubernetes responses."""

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("scenario", ["current", "stale-worker", "missing-digest"])
def test_full_deploy_checks_worker_and_fails_closed(monkeypatch, scenario):
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "deploy_release", root / "scripts/deploy_release.py"
    )
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    calls = []

    def fake_run(args, timeout=180, stdin=None):
        calls.append(args)
        if args[0] == "skopeo":
            return "" if scenario == "missing-digest" else "sha256:expected"
        if "deployment" in args and "get" in args:
            name = args[args.index("deployment") + 1]
            role = "frontend" if name == "troshka-frontend" else "backend"
            return json.dumps(
                {
                    "metadata": {"name": name, "namespace": "troshka", "generation": 1},
                    "spec": {
                        "replicas": 1,
                        "selector": {"matchLabels": {"app": name}},
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "name": "app",
                                        "image": f"{release.REGISTRY}{role}:production",
                                    }
                                ]
                            }
                        },
                    },
                    "status": {
                        "observedGeneration": 1,
                        "updatedReplicas": 1,
                        "availableReplicas": 1,
                    },
                }
            )
        if "pods" in args and "get" in args:
            pods = []
            for name in ("backend", "frontend", "worker", "tunnel"):
                digest = (
                    "old"
                    if scenario == "stale-worker" and name == "worker"
                    else "expected"
                )
                pods.append(
                    {
                        "metadata": {
                            "name": f"troshka-{name}-pod",
                            "namespace": "troshka",
                            "labels": {"app": f"troshka-{name}"},
                        },
                        "status": {
                            "containerStatuses": [
                                {
                                    "name": "app",
                                    "ready": True,
                                    "imageID": f"registry/image@sha256:{digest}",
                                }
                            ]
                        },
                    }
                )
            return json.dumps({"items": pods})
        if "exec" in args:
            if "load_template_schema" in args[-1]:
                return (
                    root / "src/backend/schemas/troshka-template.schema.json"
                ).read_text()
            return "200"
        if "wait" in args or "rollout" in args:
            return ""
        raise AssertionError(f"Unexpected command: {args}")

    monkeypatch.setattr(release, "run", fake_run)
    if scenario == "missing-digest":
        with pytest.raises(RuntimeError, match="Missing digest"):
            release.release_digests("vtest")
    elif scenario == "stale-worker":
        with pytest.raises(
            RuntimeError, match="troshka-worker-pod/app: release digest mismatch"
        ):
            release.application(
                "unused", {"backend": "sha256:expected", "frontend": "sha256:expected"}
            )
    else:
        release.application(
            "unused", {"backend": "sha256:expected", "frontend": "sha256:expected"}
        )
        assert any(
            args[:4] == ["oc", "rollout", "status", "deployment/troshka-worker"]
            for args in calls
        )
        assert any(
            args[:4] == ["oc", "rollout", "status", "deployment/troshka-tunnel"]
            for args in calls
        )
