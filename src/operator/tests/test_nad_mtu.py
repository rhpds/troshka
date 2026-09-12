import json
from helpers.k8s import build_nad


def test_nad_includes_mtu_when_set():
    cr = {
        "kind": "TroshkaNetwork",
        "metadata": {"name": "net1", "namespace": "troshka-x", "uid": "abc123"},
        "spec": {"mtu": 8850},
    }
    cfg = json.loads(build_nad(cr)["spec"]["config"])
    assert cfg["mtu"] == 8850


def test_nad_omits_mtu_when_absent():
    cr = {
        "kind": "TroshkaNetwork",
        "metadata": {"name": "net1", "namespace": "troshka-x", "uid": "abc123"},
        "spec": {},
    }
    cfg = json.loads(build_nad(cr)["spec"]["config"])
    assert "mtu" not in cfg
