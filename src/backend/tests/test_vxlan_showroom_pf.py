from app.services.vxlan import _inject_showroom_port_forward


def _showroom_topo():
    return {"nodes": [{"type": "containerNode", "data": {"isShowroom": True}}]}


def test_pf_targets_terminator_on_gateway_ip():
    # first_vni 5 -> octet3 5 -> terminator at 172.30.5.1:443
    out = _inject_showroom_port_forward([], _showroom_topo(), 5)
    pf = next(p for p in out if str(p.get("extPort")) == "443")
    assert pf["intIp"] == "172.30.5.1"
    assert str(pf["intPort"]) == "443"
    assert pf["managedByShowroom"] is True
