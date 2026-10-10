import json
from helpers.k8s import build_nad, network_allow_nested_macs


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


def test_nad_disables_port_security_when_allow_nested_macs():
    cr = {
        "kind": "TroshkaNetwork",
        "metadata": {"name": "net1", "namespace": "troshka-x", "uid": "abc123"},
        "spec": {"allowNestedMacs": True},
    }
    nad = build_nad(cr)
    cfg = json.loads(nad["spec"]["config"])
    assert cfg["portSecurity"] is False
    assert nad["metadata"]["annotations"]["k8s.ovn.org/port-security"] == "false"


def test_nad_disables_port_security_for_migration_network():
    cr = {
        "kind": "TroshkaNetwork",
        "metadata": {"name": "net1", "namespace": "troshka-x", "uid": "abc123"},
        "spec": {"networkType": "migration"},
    }
    nad = build_nad(cr)
    cfg = json.loads(nad["spec"]["config"])
    assert cfg["portSecurity"] is False


def test_network_allow_nested_macs_helper():
    assert network_allow_nested_macs({"allowNestedMacs": True})
    assert network_allow_nested_macs({"networkType": "migration"})
    assert not network_allow_nested_macs({"networkType": "standard"})
    assert not network_allow_nested_macs({})
