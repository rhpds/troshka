# src/backend/tests/test_pattern_capture_mtu.py
from app.services.pattern_service import carry_resolved_mtu


def test_capture_prefers_resolved_deployed_mtu():
    topo = {"nodes": [{"id": "net1", "type": "networkNode", "data": {"mtu": "auto"}}]}
    deployed = {"nodes": [{"id": "net1", "type": "networkNode", "data": {"mtu": 8850}}]}
    carry_resolved_mtu(topo, deployed)
    assert topo["nodes"][0]["data"]["mtu"] == 8850


def test_capture_ignores_when_no_resolved_value():
    topo = {"nodes": [{"id": "net1", "type": "networkNode", "data": {"mtu": "auto"}}]}
    deployed = {"nodes": [{"id": "net1", "type": "networkNode", "data": {}}]}
    carry_resolved_mtu(topo, deployed)
    assert topo["nodes"][0]["data"]["mtu"] == "auto"  # unchanged
