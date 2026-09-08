import json
import os

_IMAGE_TAG = os.environ.get("IMAGE_TAG", "latest")
SUSHY_IMAGE = f"quay.io/redhat-gpte/troshka-bmc:{_IMAGE_TAG}"

# Deployment-level annotation recording which node set the sushy Deployment was
# built for. The project reconcile (``_ensure_bmc_deployment``) compares this
# against the current desired signature to decide whether the running emulator
# is stale (e.g. created from a single VM by the per-VM path and now missing the
# other control-plane nodes) and needs patching to serve the full set.
BMC_SIGNATURE_ANNOTATION = "troshka.redhat.com/bmc-signature"


def _bmc_maps(bmc_vms):
    """Build (vm_map, bmc_ips, system_ips) from the full BMC-enabled VM list.

    ``vm_map``: {system_id -> kubevirt VM name} (system_id = domainUuid when
    known, else the derived kv name). ``bmc_ips``: every VM's BMC IP.
    ``system_ips``: {system_id -> BMC IP}, so the emulator can return a per-IP
    scoped Systems collection (one system per BMC IP) exactly like troshkad's
    per-VM sushy instances — keeping the shared install client (``Members[0]``)
    correct for multi-node clusters.
    """
    vm_map = {}
    bmc_ips = []
    system_ips = {}
    for vm in bmc_vms:
        domain_uuid = vm.get("domainUuid", "")
        kv_name = f"troshka-vm-{vm.get('vmId', '')[:8]}"
        system_id = domain_uuid if domain_uuid else kv_name
        vm_map[system_id] = kv_name
        if vm.get("bmcIp"):
            bmc_ips.append(vm["bmcIp"])
            system_ips[system_id] = vm["bmcIp"]
    return vm_map, bmc_ips, system_ips


def bmc_signature(bmc_vms):
    """Stable signature of the served (system_id, BMC IP) set for drift checks."""
    _, _, system_ips = _bmc_maps(bmc_vms)
    return ",".join(f"{sid}={system_ips[sid]}" for sid in sorted(system_ips))


def build_bmc_deployment(
    project_name, namespace, bmc_vms, bmc_network_nad, credentials
):
    dep_name = f"bmc-{project_name}"

    vm_map, bmc_ips, system_ips = _bmc_maps(bmc_vms)

    env = [
        {"name": "SUSHY_VM_MAP", "value": json.dumps(vm_map)},
        {"name": "SUSHY_NAMESPACE", "value": namespace},
        {"name": "SUSHY_LISTEN_PORT", "value": "8000"},
    ]

    storage_class = os.environ.get("SUSHY_STORAGE_CLASS", "")
    if storage_class:
        env.append({"name": "SUSHY_STORAGE_CLASS", "value": storage_class})

    if bmc_ips:
        env.append({"name": "SUSHY_BMC_IPS", "value": ",".join(bmc_ips)})

    if system_ips:
        env.append({"name": "SUSHY_SYSTEM_IPS", "value": json.dumps(system_ips)})

    if credentials.get("username") and credentials.get("password"):
        env.append(
            {
                "name": "SUSHY_USERNAME",
                "value": credentials["username"],
            }
        )
        env.append(
            {
                "name": "SUSHY_PASSWORD",
                "value": credentials["password"],
            }
        )

    labels = {
        "app": "troshka-bmc",
        "troshka-project": project_name,
    }

    annotations = {
        "k8s.v1.cni.cncf.io/networks": bmc_network_nad,
    }

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": dep_name,
            "namespace": namespace,
            "labels": labels,
            "annotations": {BMC_SIGNATURE_ANNOTATION: bmc_signature(bmc_vms)},
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {
                    "labels": labels,
                    "annotations": annotations,
                },
                "spec": {
                    "serviceAccountName": "troshka-bmc",
                    "containers": [
                        {
                            "name": "sushy",
                            "image": SUSHY_IMAGE,
                            "imagePullPolicy": "Always",
                            "ports": [
                                {
                                    "containerPort": 8000,
                                    "protocol": "TCP",
                                },
                                {
                                    "containerPort": 8443,
                                    "protocol": "TCP",
                                },
                            ],
                            "env": env,
                            "securityContext": {
                                "capabilities": {"add": ["NET_ADMIN", "NET_RAW"]},
                                "privileged": True,
                            },
                            "resources": {
                                "requests": {
                                    "cpu": "100m",
                                    "memory": "128Mi",
                                },
                                "limits": {
                                    "cpu": "500m",
                                    "memory": "256Mi",
                                },
                            },
                        }
                    ],
                },
            },
        },
    }
