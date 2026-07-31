"""Tests for KubeVirtDriver (images/bmc/kubevirt_driver.py)."""

import json
import sys
import os
from unittest.mock import MagicMock, patch


def _load_driver_module():
    """Import kubevirt_driver from images/bmc/ without it being on sys.path."""
    import importlib.util

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "images",
        "bmc",
        "kubevirt_driver.py",
    )
    spec = importlib.util.spec_from_file_location("kubevirt_driver", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@patch("kubernetes.config.load_incluster_config")
@patch("kubernetes.client.CustomObjectsApi")
def _make_driver(mock_api_cls, mock_config):
    mod = _load_driver_module()
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    os.environ["SUSHY_NAMESPACE"] = "test-ns"
    os.environ["SUSHY_VM_MAP"] = json.dumps({"vm-uuid-1": "kv-vm-1"})
    drv = mod.KubeVirtDriver()
    return drv, mock_api, mod


class TestKvName:
    def test_maps_known_identity(self):
        drv, _, _ = _make_driver()
        assert drv._kv_name("vm-uuid-1") == "kv-vm-1"

    def test_strips_slashes(self):
        drv, _, _ = _make_driver()
        assert drv._kv_name("/vm-uuid-1/") == "kv-vm-1"

    def test_unmapped_identity_passthrough(self):
        drv, _, _ = _make_driver()
        assert drv._kv_name("unknown-vm") == "unknown-vm"


class TestGetPowerState:
    def test_on_when_running(self):
        drv, mock_api, _ = _make_driver()
        mock_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Running"}
        }
        assert drv.get_power_state("vm-uuid-1") == "On"

    def test_off_when_no_vmi(self):
        drv, mock_api, mod = _make_driver()
        from kubernetes import client

        mock_api.get_namespaced_custom_object.side_effect = client.ApiException(
            status=404
        )
        assert drv.get_power_state("vm-uuid-1") == "Off"

    def test_off_when_not_running(self):
        drv, mock_api, _ = _make_driver()
        mock_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Scheduling"}
        }
        assert drv.get_power_state("vm-uuid-1") == "Off"


class TestSetPowerState:
    def test_on(self):
        drv, mock_api, _ = _make_driver()
        drv.set_power_state("vm-uuid-1", "On")
        mock_api.patch_namespaced_custom_object.assert_called_once()
        body = mock_api.patch_namespaced_custom_object.call_args[1]["body"]
        assert body["spec"]["running"] is True

    def test_force_off_deletes_vmi(self):
        drv, mock_api, _ = _make_driver()
        drv.set_power_state("vm-uuid-1", "ForceOff")
        assert mock_api.patch_namespaced_custom_object.call_count == 1
        assert mock_api.delete_namespaced_custom_object.call_count == 1

    def test_force_restart(self):
        drv, mock_api, _ = _make_driver()
        drv.set_power_state("vm-uuid-1", "ForceRestart")
        assert mock_api.delete_namespaced_custom_object.call_count == 1
        assert mock_api.patch_namespaced_custom_object.call_count == 2

    def test_graceful_shutdown(self):
        drv, mock_api, _ = _make_driver()
        drv.set_power_state("vm-uuid-1", "GracefulShutdown")
        assert mock_api.delete_namespaced_custom_object.call_count == 1
        body = mock_api.patch_namespaced_custom_object.call_args[1]["body"]
        assert body["spec"]["running"] is False


class TestDeleteVmi:
    def test_ignores_api_exception(self):
        drv, mock_api, _ = _make_driver()
        from kubernetes import client

        mock_api.delete_namespaced_custom_object.side_effect = client.ApiException(
            status=404
        )
        drv._delete_vmi("vm-uuid-1")


class TestGetBootDevice:
    def test_hdd_from_disk(self):
        drv, mock_api, _ = _make_driver()
        mock_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "devices": {
                                "disks": [{"name": "root", "disk": {}, "bootOrder": 1}],
                                "interfaces": [],
                            }
                        }
                    }
                }
            }
        }
        assert drv.get_boot_device("vm-uuid-1") == "Hdd"

    def test_pxe_from_interface(self):
        drv, mock_api, _ = _make_driver()
        mock_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "devices": {
                                "disks": [],
                                "interfaces": [{"name": "nic0", "bootOrder": 1}],
                            }
                        }
                    }
                }
            }
        }
        assert drv.get_boot_device("vm-uuid-1") == "Pxe"

    def test_default_hdd_no_boot_orders(self):
        drv, mock_api, _ = _make_driver()
        mock_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {"domain": {"devices": {"disks": [], "interfaces": []}}}
                }
            }
        }
        assert drv.get_boot_device("vm-uuid-1") == "Hdd"


class TestStripBootOrders:
    def test_removes_boot_order(self):
        _, _, mod = _make_driver()
        disks = [{"name": "root", "disk": {}, "bootOrder": 1}]
        ifaces = [{"name": "nic0", "bootOrder": 2}]
        clean_d, clean_i = mod.KubeVirtDriver._strip_boot_orders(disks, ifaces)
        assert "bootOrder" not in clean_d[0]
        assert "bootOrder" not in clean_i[0]
        assert disks[0]["bootOrder"] == 1


class TestAssignBootOrders:
    def test_pxe_sets_interface_first(self):
        _, _, mod = _make_driver()
        disks = [{"name": "root", "disk": {}}]
        ifaces = [{"name": "nic0"}]
        mod.KubeVirtDriver._assign_boot_orders(disks, ifaces, "interface")
        assert ifaces[0]["bootOrder"] == 1
        assert disks[0]["bootOrder"] == 2

    def test_cdrom_sets_cdrom_first(self):
        _, _, mod = _make_driver()
        disks = [{"name": "root", "disk": {}}, {"name": "iso", "cdrom": {}}]
        ifaces = []
        mod.KubeVirtDriver._assign_boot_orders(disks, ifaces, "cdrom")
        assert disks[1]["bootOrder"] == 1
        assert disks[0]["bootOrder"] == 2

    def test_disk_default(self):
        _, _, mod = _make_driver()
        disks = [{"name": "root", "disk": {}}]
        ifaces = []
        mod.KubeVirtDriver._assign_boot_orders(disks, ifaces, "disk")
        assert disks[0]["bootOrder"] == 1


class TestSetBootDevice:
    def test_boot_once_stores_override(self):
        drv, mock_api, _ = _make_driver()
        mock_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "devices": {
                                "disks": [{"name": "root", "disk": {}, "bootOrder": 1}],
                                "interfaces": [{"name": "nic0", "bootOrder": 2}],
                            }
                        }
                    }
                }
            }
        }
        drv.set_boot_device("vm-uuid-1", "Pxe", boot_enabled="Once")
        assert "kv-vm-1" in drv._boot_once_overrides
        drv._boot_once_overrides.clear()

    def test_continuous_clears_override(self):
        drv, mock_api, _ = _make_driver()
        drv._boot_once_overrides["kv-vm-1"] = {"disks": [], "interfaces": []}
        mock_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {"domain": {"devices": {"disks": [], "interfaces": []}}}
                }
            }
        }
        drv.set_boot_device("vm-uuid-1", "Hdd", boot_enabled="Continuous")
        assert "kv-vm-1" not in drv._boot_once_overrides


class TestGetBootOverrideEnabled:
    def test_once_when_in_overrides(self):
        drv, _, _ = _make_driver()
        drv._boot_once_overrides["kv-vm-1"] = {}
        assert drv.get_boot_override_enabled("vm-uuid-1") == "Once"
        drv._boot_once_overrides.clear()

    def test_continuous_when_not_in_overrides(self):
        drv, _, _ = _make_driver()
        assert drv.get_boot_override_enabled("vm-uuid-1") == "Continuous"


class TestRevertBootOnce:
    def test_restores_saved_boot_orders(self):
        drv, mock_api, _ = _make_driver()
        drv._boot_once_overrides["kv-vm-1"] = {
            "disks": [{"root": 1}],
            "interfaces": [{"nic0": 2}],
        }
        mock_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "devices": {
                                "disks": [{"name": "root", "disk": {}, "bootOrder": 5}],
                                "interfaces": [{"name": "nic0", "bootOrder": 5}],
                            }
                        }
                    }
                }
            }
        }
        drv.revert_boot_once("vm-uuid-1")
        assert "kv-vm-1" not in drv._boot_once_overrides
        mock_api.patch_namespaced_custom_object.assert_called_once()

    def test_noop_when_no_saved(self):
        drv, mock_api, _ = _make_driver()
        drv.revert_boot_once("vm-uuid-1")
        mock_api.patch_namespaced_custom_object.assert_not_called()


class TestGetBootMode:
    def test_uefi(self):
        drv, mock_api, _ = _make_driver()
        mock_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "firmware": {"bootloader": {"efi": {"secureBoot": False}}}
                        }
                    }
                }
            }
        }
        assert drv.get_boot_mode("vm-uuid-1") == "UEFI"

    def test_legacy(self):
        drv, mock_api, _ = _make_driver()
        mock_api.get_namespaced_custom_object.return_value = {
            "spec": {"template": {"spec": {"domain": {"firmware": {}}}}}
        }
        assert drv.get_boot_mode("vm-uuid-1") == "Legacy"


class TestGetTotalMemory:
    def test_mi(self):
        drv, mock_api, _ = _make_driver()
        mock_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {"resources": {"requests": {"memory": "8192Mi"}}}
                    }
                }
            }
        }
        assert drv.get_total_memory("vm-uuid-1") == 8192

    def test_gi(self):
        drv, mock_api, _ = _make_driver()
        mock_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {"domain": {"resources": {"requests": {"memory": "16Gi"}}}}
                }
            }
        }
        assert drv.get_total_memory("vm-uuid-1") == 16384


class TestGetTotalCpus:
    def test_returns_cores(self):
        drv, mock_api, _ = _make_driver()
        mock_api.get_namespaced_custom_object.return_value = {
            "spec": {"template": {"spec": {"domain": {"cpu": {"cores": 4}}}}}
        }
        assert drv.get_total_cpus("vm-uuid-1") == 4


class TestGetNics:
    def test_returns_interfaces(self):
        drv, mock_api, _ = _make_driver()
        mock_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "devices": {
                                "interfaces": [
                                    {"name": "nic0", "macAddress": "aa:bb:cc:dd:ee:ff"}
                                ]
                            }
                        }
                    }
                }
            }
        }
        nics = drv.get_nics("vm-uuid-1")
        assert nics == [{"id": "nic0", "mac": "aa:bb:cc:dd:ee:ff"}]


class TestGetSystems:
    def test_returns_uuids(self):
        drv, mock_api, _ = _make_driver()
        mock_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "kv-vm-1"},
                    "spec": {
                        "template": {
                            "spec": {"domain": {"firmware": {"uuid": "uuid-1234"}}}
                        }
                    },
                }
            ]
        }
        assert drv.get_systems() == ["uuid-1234"]


class TestGetBiosVersion:
    def test_static(self):
        drv, _, _ = _make_driver()
        assert drv.get_bios_version("vm-uuid-1") == "KubeVirt BIOS"


class TestSetBootMode:
    def test_noop(self):
        drv, mock_api, _ = _make_driver()
        drv.set_boot_mode("vm-uuid-1", "UEFI")
        mock_api.patch_namespaced_custom_object.assert_not_called()
