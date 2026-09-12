# src/troshkad/tests/test_default_route_mtu.py
from unittest.mock import patch
import troshkad

def test_default_route_mtu_parses_ip_output():
    route = "default via 10.0.0.1 dev enp1s0 proto dhcp\n"
    link = "2: enp1s0: <> mtu 8900 qdisc ...\n"
    with patch("troshkad.subprocess.check_output", side_effect=[route, link]):
        assert troshkad._default_route_mtu() == 8900

def test_default_route_mtu_returns_none_on_error():
    with patch("troshkad.subprocess.check_output", side_effect=Exception("boom")):
        assert troshkad._default_route_mtu() is None
