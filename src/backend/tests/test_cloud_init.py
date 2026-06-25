from app.services.cloud_init import generate_userdata


def test_vm_gets_chrony_client_config():
    vm_data = {
        "name": "bastion",
        "cloudInit": True,
        "gateway_ip": "192.168.1.1",
    }
    userdata = generate_userdata(vm_data)
    assert "chrony" in userdata
    assert "server 192.168.1.1 iburst prefer" in userdata
    assert "makestep 1 -1" in userdata


def test_vm_without_gateway_ip_no_chrony_override():
    vm_data = {
        "name": "standalone",
        "cloudInit": True,
    }
    userdata = generate_userdata(vm_data)
    assert "makestep 1 -1" not in userdata
