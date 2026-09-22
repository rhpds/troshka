from app.services.vxlan import _inject_showroom_port_forward


def _showroom_topo():
    return {"nodes": [{"type": "containerNode", "data": {"isShowroom": True}}]}


def test_pf_targets_terminator_on_gateway_ip():
    # first_vni 5 -> octet3 5 -> terminator at 172.30.5.1:443 (cloud default)
    out = _inject_showroom_port_forward([], _showroom_topo(), 5)
    pf = next(p for p in out if str(p.get("extPort")) == "443")
    assert pf["intIp"] == "172.30.5.1"
    assert str(pf["intPort"]) == "443"
    assert pf["managedByShowroom"] is True


def test_pf_targets_terminator_on_cloud_provider():
    # route_web=False (ec2/gcp/azure): socat TLS terminator at .1:443.
    out = _inject_showroom_port_forward([], _showroom_topo(), 5, route_web=False)
    pf = next(p for p in out if str(p.get("extPort")) == "443")
    assert pf["intIp"] == "172.30.5.1"
    assert str(pf["intPort"]) == "443"
    assert pf["managedByShowroom"] is True


def test_pf_targets_showroom_container_on_route_provider():
    # route_web=True (ocpvirt/kubevirt): showroom is edge-terminated at the OCP
    # Route, so the forward targets the showroom container directly at .3:80 —
    # NOT the TLS terminator .1:443. This is the pre-TLS-edge behavior and MUST
    # stay unchanged for route providers.
    out = _inject_showroom_port_forward([], _showroom_topo(), 5, route_web=True)
    pf = next(p for p in out if str(p.get("extPort")) == "443")
    assert pf["intIp"] == "172.30.5.3"
    assert str(pf["intPort"]) == "80"
    assert pf["managedByShowroom"] is True
