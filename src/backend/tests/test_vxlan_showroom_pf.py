from app.services.vxlan import _build_gateway_config, _inject_showroom_port_forward


def _showroom_topo():
    return {"nodes": [{"type": "containerNode", "data": {"isShowroom": True}}]}


def test_pf_targets_terminator_on_gateway_ip():
    # first_vni 5 -> octet3 5 -> terminator at 172.30.5.2:443 (cloud default)
    out = _inject_showroom_port_forward([], _showroom_topo(), 5)
    pf = next(p for p in out if str(p.get("extPort")) == "443")
    assert pf["intIp"] == "172.30.5.2"
    assert str(pf["intPort"]) == "443"
    assert pf["managedByShowroom"] is True


def test_pf_targets_terminator_on_cloud_provider():
    # route_web=False (ec2/gcp/azure): socat TLS terminator at .2:443 in netns.
    out = _inject_showroom_port_forward([], _showroom_topo(), 5, route_web=False)
    pf = next(p for p in out if str(p.get("extPort")) == "443")
    assert pf["intIp"] == "172.30.5.2"
    assert str(pf["intPort"]) == "443"
    assert pf["managedByShowroom"] is True


def test_pf_targets_showroom_container_on_route_provider():
    # route_web=True (ocpvirt/kubevirt): showroom is edge-terminated at the OCP
    # Route, so the forward targets the showroom container directly at .3:80 —
    # NOT the TLS terminator .2:443. This is the pre-TLS-edge behavior and MUST
    # stay unchanged for route providers.
    out = _inject_showroom_port_forward([], _showroom_topo(), 5, route_web=True)
    pf = next(p for p in out if str(p.get("extPort")) == "443")
    assert pf["intIp"] == "172.30.5.3"
    assert str(pf["intPort"]) == "80"
    assert pf["managedByShowroom"] is True


def test_build_gateway_config_binds_showroom_pf_to_eip_private_ip():
    """Host DNAT needs _private_ip on the managed 443 forward (inject clears extIpId)."""
    topology = {
        "externalIps": [
            {
                "id": "eip-1",
                "name": "IP-1",
                "ip": "1.2.3.4",
                "_private_ip": "10.100.1.97",
            }
        ],
        "nodes": [
            {
                "type": "networkNode",
                "data": {
                    "subtype": "gateway",
                    "gatewayMode": "nat-portforward",
                    "portForwards": [
                        {
                            "extPort": "6443",
                            "intIp": "10.0.0.10",
                            "intPort": "6443",
                            "proto": "tcp",
                            "extIpId": "eip-1",
                        }
                    ],
                },
            },
            {"type": "containerNode", "data": {"isShowroom": True}},
        ],
    }
    networks = [{"vni": 2151}]
    gw = _build_gateway_config(topology["nodes"], topology, networks)
    assert gw is not None
    showroom = next(pf for pf in gw["port_forwards"] if pf.get("managedByShowroom"))
    assert showroom["extPort"] == "443"
    assert showroom["intIp"] == "172.30.103.2"
    assert showroom["_private_ip"] == "10.100.1.97"
