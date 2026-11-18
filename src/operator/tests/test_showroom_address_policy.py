import pytest
from helpers.topology import _showroom_ip_for_cidr


def test_showroom_uses_nine_only():
    assert _showroom_ip_for_cidr("10.0.0.0/24", {"10.0.0.6"}) == "10.0.0.9"
    with pytest.raises(ValueError, match="reserved"):
        _showroom_ip_for_cidr("10.0.0.0/24", {"10.0.0.9"})
