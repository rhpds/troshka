"""
OCP Agent-Based Installer template customization.

No bootstrap VM needed — the CP nodes boot from an agent ISO and
self-assemble into a cluster. No nested virtualization, no libvirt on bastion.

Flow:
1. Bastion creates agent ISO: openshift-install agent create image
2. Bastion serves ISO via HTTP
3. CP nodes boot from ISO via Redfish virtual media (sushy-emulator)
4. Nodes discover each other, form cluster
5. openshift-install agent wait-for install-complete
"""

import ipaddress
import re
import uuid

_MAC_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$")


def _find_bastion_ip(topology):
    """Find the bastion's cluster network IP."""
    for node in topology.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        data = node.get("data", {})
        if data.get("name") == "bastion" or "bastions" in data.get("tags", {}).get(
            "AnsibleGroup", ""
        ):
            nics = data.get("nics", [])
            if nics and nics[0].get("ip"):
                return nics[0]["ip"]
    return None


def _find_cluster_cidr(topology):
    """Find the cluster network CIDR from topology."""
    default = "10.0.0.0/24"
    for node in topology.get("nodes", []):
        if node.get("type") != "networkNode":
            continue
        data = node.get("data", {})
        if data.get("subtype") == "network" and data.get("networkType") != "bmc":
            cidr = data.get("cidr", default)
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                return default
            return cidr
    return default


def _collect_ocp_mounts(topology):
    """Find storage nodes with ocpMount and return list of (disk_index, mount_path).

    disk_index is the 0-based position of the disk on its parent VM (vda=0, vdb=1, etc.).
    """
    mounts = []
    edges = topology.get("edges", [])
    nodes = {n["id"]: n for n in topology.get("nodes", [])}

    for node in topology.get("nodes", []):
        if node.get("type") != "storageNode":
            continue
        mount_path = node.get("data", {}).get("ocpMount")
        if not mount_path:
            continue
        for edge in edges:
            if edge.get("source") == node["id"]:
                vm_id = edge.get("target")
            elif edge.get("target") == node["id"]:
                vm_id = edge.get("source")
            else:
                continue
            vm_node = nodes.get(vm_id)
            if not vm_node or vm_node.get("type") != "vmNode":
                continue
            if vm_node.get("data", {}).get("os") != "rhcos":
                continue
            handle = edge.get("targetHandle") or edge.get("sourceHandle", "")
            dcs = vm_node.get("data", {}).get("diskControllers", [])
            disk_index = next(
                (i for i, dc in enumerate(dcs) if dc["id"] in handle),
                -1,
            )
            if disk_index >= 0:
                dev = f"/dev/vd{chr(ord('a') + disk_index)}"
                mounts.append({"device": dev, "mount": mount_path})
            break
    return mounts


def _generate_ocp_mount_script(topology):
    """Generate shell script lines to create MachineConfig extra manifests for ocp_mount disks.

    Follows the Red Hat Solution 4952011 pattern:
    - systemd-mkfs service to format the disk
    - systemd mount unit with prjquota
    - restorecon service for SELinux contexts
    """
    import base64 as _b64

    import yaml as _yaml

    mounts = _collect_ocp_mounts(topology)
    if not mounts:
        return ""

    lines = "    # Create extra manifests for disk mounts\n"
    lines += "    mkdir -p /home/cloud-user/ocp-install/openshift\n"
    for m in mounts:
        dev = m["device"]
        mount_path = m["mount"]
        mount_unit = mount_path.strip("/").replace("/", "-")
        dev_unit = dev.replace("/dev/", "dev-").replace("/", "-")
        mc = {
            "apiVersion": "machineconfiguration.openshift.io/v1",
            "kind": "MachineConfig",
            "metadata": {
                "labels": {"machineconfiguration.openshift.io/role": "master"},
                "name": f"98-{mount_unit}",
            },
            "spec": {
                "config": {
                    "ignition": {"version": "3.2.0"},
                    "systemd": {
                        "units": [
                            {
                                "name": f"systemd-mkfs@{dev_unit}.service",
                                "enabled": True,
                                "contents": (
                                    f"[Unit]\n"
                                    f"Description=Make File System on {dev}\n"
                                    f"DefaultDependencies=no\n"
                                    f"BindsTo={dev_unit}.device\n"
                                    f"After={dev_unit}.device var.mount\n"
                                    f"Before=systemd-fsck@{dev_unit}.service\n"
                                    f"\n"
                                    f"[Service]\n"
                                    f"Type=oneshot\n"
                                    f"RemainAfterExit=yes\n"
                                    f'ExecStart=-/bin/bash -c "/bin/rm -rf {mount_path}/*"\n'
                                    f"ExecStart=/usr/lib/systemd/systemd-makefs xfs {dev}\n"
                                    f"TimeoutSec=0\n"
                                    f"\n"
                                    f"[Install]\n"
                                    f"WantedBy={mount_unit}.mount\n"
                                ),
                            },
                            {
                                "name": f"{mount_unit}.mount",
                                "enabled": True,
                                "contents": (
                                    f"[Unit]\n"
                                    f"Description=Mount {dev} to {mount_path}\n"
                                    f"Before=local-fs.target\n"
                                    f"Requires=systemd-mkfs@{dev_unit}.service\n"
                                    f"After=systemd-mkfs@{dev_unit}.service\n"
                                    f"\n"
                                    f"[Mount]\n"
                                    f"What={dev}\n"
                                    f"Where={mount_path}\n"
                                    f"Type=xfs\n"
                                    f"Options=defaults,prjquota\n"
                                    f"\n"
                                    f"[Install]\n"
                                    f"WantedBy=local-fs.target\n"
                                ),
                            },
                            {
                                "name": f"restorecon-{mount_unit}.service",
                                "enabled": True,
                                "contents": (
                                    f"[Unit]\n"
                                    f"Description=Restore recursive SELinux security contexts\n"
                                    f"DefaultDependencies=no\n"
                                    f"After={mount_unit}.mount\n"
                                    f"Before=crio.service\n"
                                    f"\n"
                                    f"[Service]\n"
                                    f"Type=oneshot\n"
                                    f"RemainAfterExit=yes\n"
                                    f"ExecStart=/sbin/restorecon -R {mount_path}/\n"
                                    f"TimeoutSec=0\n"
                                    f"\n"
                                    f"[Install]\n"
                                    f"WantedBy=multi-user.target graphical.target\n"
                                ),
                            },
                        ]
                    },
                }
            },
        }
        mc_yaml = _yaml.dump(mc, default_flow_style=False)
        mc_b64 = _b64.b64encode(mc_yaml.encode()).decode()
        fname = f"98-{mount_unit}.yaml"
        lines += f"    echo '{mc_b64}' | base64 -d > /home/cloud-user/ocp-install/openshift/{fname}\n"
        lines += f"    echo 'Created MachineConfig for {mount_path} on {dev}'\n"
    return lines


def _find_sno_node_ip(topology):
    """Find the SNO node's cluster IP — the single controller VM."""
    for node in topology.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        tags = node.get("data", {}).get("tags", {})
        if tags.get("AnsibleGroup") != "controllers":
            continue
        nics = node.get("data", {}).get("nics", [])
        ip = nics[0].get("ip") if nics else None
        if ip:
            return ip
    return None


def customize_topology(topology: dict, template_id: str, config: dict) -> dict:
    """Apply OCP Agent-Based configuration to a base topology."""
    cluster_name = config.get("cluster_name", "ocp")
    base_domain = config.get("base_domain", "ocp.local")
    ocp_version = config.get("ocp_version", "4.20")
    common_password = config.get("common_password", "")
    if not common_password:
        raise ValueError(
            "No password provided. Set common_password in the API request "
            "or in the template YAML."
        )
    pull_secret_json = config.get("pull_secret_json", "")
    ssh_pub_key = config.get("ssh_pub_key", "")
    bastion_image = config.get("bastion_image")
    bastion_iso = config.get("bastion_iso")
    bastion_bmc_ip = config.get("bastion_bmc_ip", "192.168.100.50")
    auto_install_ocp = config.get("auto_install_ocp", True)
    ssh_key_ids = config.get("ssh_key_ids", [])
    ssh_keys = config.get("ssh_keys", [])
    resolved = config.get("resolved", {})
    pull_through_registry = resolved.get("pull_through_registry")

    # Read VIPs from template ocp section if available, otherwise derive from topology
    ocp_cfg = resolved.get("ocp", {})
    api_vip = ocp_cfg.get("api_vip", "")
    ingress_vip = ocp_cfg.get("ingress_vip", "")

    if not api_vip or not ingress_vip:
        cluster_cidr = _find_cluster_cidr(topology)
        net = ipaddress.ip_network(cluster_cidr, strict=False)
        if not api_vip:
            api_vip = str(net.network_address + 2)
        if not ingress_vip:
            ingress_vip = str(net.network_address + 3)
        for n in topology.get("nodes", []):
            if (
                n.get("type") == "vmNode"
                and n.get("data", {}).get("tags", {}).get("AnsibleGroup")
                == "controllers"
            ):
                cp_ip = n.get("data", {}).get("nics", [{}])[0].get("ip")
                if cp_ip:
                    if not ocp_cfg.get("api_vip"):
                        api_vip = cp_ip
                    if not ocp_cfg.get("ingress_vip"):
                        ingress_vip = str(
                            ipaddress.IPv4Address(int(ipaddress.IPv4Address(cp_ip)) + 1)
                        )
                    break

    dns_api = api_vip
    dns_ingress = ingress_vip

    _setup_dns_records(
        topology, cluster_name, base_domain, dns_api, dns_ingress, resolved
    )
    _attach_bastion_image(topology, bastion_image)
    _attach_bastion_iso(topology, bastion_iso)
    _setup_bastion_cloud_init(
        topology,
        common_password,
        ssh_pub_key,
        ssh_key_ids,
        ssh_keys,
        bastion_iso,
        pull_secret_json,
        cluster_name,
        base_domain,
        ocp_version,
        auto_install_ocp,
        template_id,
        api_vip,
        ingress_vip,
        bastion_bmc_ip,
        pull_through_registry=pull_through_registry,
    )

    return topology


def _setup_dns_records(
    topology, cluster_name, base_domain, api_vip, ingress_vip, resolved=None
):
    resolved = resolved or {}
    for node in topology.get("nodes", []):
        if (
            node.get("type") == "networkNode"
            and node.get("data", {}).get("subtype") == "network"
            and node.get("data", {}).get("networkType") != "bmc"
        ):
            node["data"]["dns"] = True
            node["data"]["dnsDomain"] = base_domain
            records = [
                {"name": f"api.{cluster_name}.{base_domain}", "ip": api_vip},
                {"name": f"api-int.{cluster_name}.{base_domain}", "ip": api_vip},
                {"name": f".apps.{cluster_name}.{base_domain}", "ip": ingress_vip},
            ]
            bastion_ip = _find_bastion_ip(topology)
            # Add extra DNS records from template (e.g. infra.domain → bastion)
            for extra in resolved.get("dns_records", []):
                target = extra.get("target", "")
                ip = extra.get("ip", "")
                if target == "bastion" and bastion_ip:
                    ip = bastion_ip
                if ip:
                    records.append({"name": extra["name"], "ip": ip})
            node["data"]["dnsRecords"] = records
            break


def _attach_bastion_image(topology, bastion_image):
    """Reuse from IPI template."""
    if not bastion_image:
        return
    for node in topology.get("nodes", []):
        if node.get("type") == "storageNode" and (
            node.get("data", {}).get("name") in ("bastion-disk", "bastion-disk0")
        ):
            node["data"]["source"] = "library"
            node["data"]["libraryItemId"] = bastion_image["id"]
            node["data"]["libraryItemName"] = bastion_image["name"]
            node["data"]["libraryItemSize"] = bastion_image["size_gb"]
            node["data"]["size"] = max(
                bastion_image["size_gb"], node["data"].get("size", 0)
            )
            break


def _attach_bastion_iso(topology, bastion_iso):
    """Reuse from IPI template."""
    if not bastion_iso:
        return
    bastion_vm = None
    for node in topology.get("nodes", []):
        if (
            node.get("type") == "vmNode"
            and node.get("data", {}).get("name") == "bastion"
        ):
            bastion_vm = node
            break
    if not bastion_vm:
        return

    iso_node_id = str(uuid.uuid4())
    bast_x = bastion_vm["position"]["x"]
    bast_y = bastion_vm["position"]["y"]
    dc2 = {"id": f"dp-{str(uuid.uuid4())}", "name": "cdrom0", "bus": "sata"}
    bastion_vm["data"]["diskControllers"].append(dc2)

    iso_node = {
        "id": iso_node_id,
        "type": "storageNode",
        "position": {"x": bast_x - 190, "y": bast_y + 170},
        "data": {
            "label": "rhel-dvd",
            "name": "rhel-dvd",
            "size": (
                bastion_iso["size_bytes"] // (1024**3)
                if bastion_iso.get("size_bytes")
                else 10
            ),
            "format": "iso",
            "icon": "\U0001f4bf",
            "source": "library",
            "libraryItemId": bastion_iso["id"],
            "libraryItemName": bastion_iso["name"],
        },
    }
    iso_edge = {
        "id": str(uuid.uuid4()),
        "source": iso_node_id,
        "target": bastion_vm["id"],
        "sourceHandle": "right",
        "targetHandle": f"dp-{dc2['id']}-left",
        "type": "smoothstep",
        "style": {
            "stroke": "rgba(251,191,36,0.6)",
            "strokeWidth": 2,
            "strokeDasharray": "4 4",
        },
        "animated": False,
        "className": "edge-storage-pulse",
    }
    topology["nodes"].append(iso_node)
    topology["edges"].append(iso_edge)


def _setup_bastion_cloud_init(
    topology,
    password,
    ssh_pub_key,
    ssh_key_ids,
    ssh_keys,
    bastion_iso,
    pull_secret_json,
    cluster_name,
    base_domain,
    ocp_version,
    auto_install_ocp,
    template_id,
    api_vip,
    ingress_vip,
    bastion_bmc_ip,
    pull_through_registry=None,
):
    for node in topology.get("nodes", []):
        if (
            node.get("type") != "vmNode"
            or node.get("data", {}).get("name") != "bastion"
        ):
            continue

        # When auto_install_ocp is false, agnosticd handles everything
        # via Ansible roles — set password + SSH keys so exec API can connect
        if not auto_install_ocp:
            node["data"]["cloudInit"] = True
            cloud_user_pw = node["data"].get("ciCloudUserPassword") or password
            if cloud_user_pw:
                node["data"]["ciCloudUserPassword"] = cloud_user_pw
            if ssh_key_ids:
                node["data"]["ciSshKeyIds"] = ssh_key_ids
            if ssh_keys:
                node["data"]["ciSshKeys"] = ssh_keys

            # Inject pull-through registry config for podman on bastion
            if pull_through_registry and pull_through_registry.get("enabled"):
                ptr_url = pull_through_registry["url"]
                if "ciUserData" not in node["data"]:
                    node["data"]["ciUserData"] = "runcmd:\n"
                node["data"]["ciUserData"] += (
                    "  - |\n"
                    "    mkdir -p /etc/containers/registries.conf.d\n"
                    "    cat > /etc/containers/registries.conf.d/rhdp-cache.conf << 'EOF'\n"
                )
                for source, org in pull_through_registry.get("orgs", {}).items():
                    node["data"]["ciUserData"] += (
                        f"    [[registry]]\n"
                        f'      prefix = "{source}"\n'
                        f'      location = "{ptr_url}/{org}"\n'
                        f"\n"
                    )
                node["data"]["ciUserData"] += "    EOF\n"
            break

        node["data"]["cloudInit"] = True
        node["data"]["ciPackages"] = [
            "git",
            "ansible-core",
            "python3-pip",
            "bind-utils",
            "nmstate",
            "@Server with GUI",
            "firefox",
            "ptyxis",
            "gnome-shell-extension-dash-to-dock",
            "google-noto-sans-fonts",
            "google-noto-sans-mono-fonts",
            "dejavu-sans-fonts",
            "desktop-backgrounds-gnome",
        ]
        cloud_user_pw = node["data"].get("ciCloudUserPassword") or password
        if cloud_user_pw:
            node["data"]["ciCloudUserPassword"] = cloud_user_pw
        if ssh_key_ids:
            node["data"]["ciSshKeyIds"] = ssh_key_ids
        if ssh_keys:
            node["data"]["ciSshKeys"] = ssh_keys

        # DVD mount + yum repos (mounts: runs before packages:)
        if bastion_iso:
            node["data"]["ciUserData"] = (
                "mounts:\n"
                '  - [/dev/sr0, /mnt/rhel-dvd, iso9660, "ro,nofail", "0", "0"]\n'
                "yum_repos:\n"
                "  rhel-dvd-baseos:\n"
                "    name: RHEL DVD BaseOS\n"
                "    baseurl: file:///mnt/rhel-dvd/BaseOS\n"
                "    enabled: true\n"
                "    gpgcheck: false\n"
                "  rhel-dvd-appstream:\n"
                "    name: RHEL DVD AppStream\n"
                "    baseurl: file:///mnt/rhel-dvd/AppStream\n"
                "    enabled: true\n"
                "    gpgcheck: false\n"
                "runcmd:\n"
                "  - nmcli con up cluster-nic 2>/dev/null || true\n"
            )

        # Ensure ciUserData exists (may not be set if no bastion_iso)
        if "ciUserData" not in node["data"]:
            node["data"]["ciUserData"] = "runcmd:\n"

        # Guard: skip all remaining runcmd blocks if cluster already installed (pattern deploy)
        _guard = "    [ -f /home/cloud-user/ocp-install/auth/kubeconfig ] && exit 0\n"

        # Pull secret
        if pull_secret_json:
            node["data"]["ciUserData"] += (
                "  - |\n"
                + _guard
                + "    cat > /home/cloud-user/pull-secret.json << 'PULLSECRETEOF'\n"
                f"    {pull_secret_json}\n"
                "    PULLSECRETEOF\n"
                "    chown cloud-user:cloud-user /home/cloud-user/pull-secret.json\n"
                "    chmod 600 /home/cloud-user/pull-secret.json\n"
            )

        # Pull-through registry mirror config for podman
        if pull_through_registry and pull_through_registry.get("enabled"):
            ptr_url = pull_through_registry["url"]
            node["data"]["ciUserData"] += (
                "  - |\n"
                + _guard
                + "    mkdir -p /etc/containers/registries.conf.d\n"
                + "    cat > /etc/containers/registries.conf.d/rhdp-cache.conf << 'EOF'\n"
            )
            for source, org in pull_through_registry.get("orgs", {}).items():
                node["data"]["ciUserData"] += (
                    f"    [[registry]]\n"
                    f'      prefix = "{source}"\n'
                    f'      location = "{ptr_url}/{org}"\n'
                    f"\n"
                )
            node["data"]["ciUserData"] += "    EOF\n"

        # Build install-config.yaml and agent-config.yaml
        install_config = _build_install_config(
            topology,
            template_id,
            cluster_name,
            base_domain,
            api_vip,
            ingress_vip,
            password,
            pull_secret_json,
            ssh_pub_key,
            pull_through_registry=pull_through_registry,
        )
        agent_config = _build_agent_config(
            topology,
            cluster_name,
            base_domain,
            api_vip,
            ingress_vip,
        )

        # Install script
        # Collect BMC IPs only for hub cluster VMs (controllers/workers)
        bmc_ips = []
        for tnode in topology.get("nodes", []):
            td = tnode.get("data", {})
            group = td.get("tags", {}).get("AnsibleGroup", "")
            if (
                tnode.get("type") == "vmNode"
                and td.get("bmcEnabled")
                and td.get("bmcIp")
                and group in ("controllers", "workers")
            ):
                ip = str(ipaddress.IPv4Address(td["bmcIp"]))
                bmc_ips.append(ip)
        bmc_ips_str = " ".join(bmc_ips)

        # Read BMC password from the BMC network node (may differ from common)
        bmc_pw = password
        for tnode in topology.get("nodes", []):
            td = tnode.get("data", {})
            if td.get("networkType") == "bmc" and td.get("bmcPassword"):
                bmc_pw = td["bmcPassword"]
                break

        node["data"]["ciUserData"] += _build_install_script(
            ocp_version,
            auto_install_ocp,
            bmc_pw,
            bmc_ips_str,
            cluster_name,
            base_domain,
            topology=topology,
        )

        # Write install-config.yaml
        if install_config:
            indented_ic = "\n".join(
                "    " + line for line in install_config.split("\n")
            )
            node["data"]["ciUserData"] += (
                "  - |\n" + _guard + "    mkdir -p /home/cloud-user/ocp-install\n"
                "    cat > /home/cloud-user/ocp-install/install-config.yaml << 'ICEOF'\n"
                f"{indented_ic}\n"
                "    ICEOF\n"
                "    chown -R cloud-user:cloud-user /home/cloud-user/ocp-install\n"
            )

        # Write agent-config.yaml
        if agent_config:
            indented_ac = "\n".join("    " + line for line in agent_config.split("\n"))
            node["data"]["ciUserData"] += (
                "  - |\n"
                + _guard
                + "    cat > /home/cloud-user/ocp-install/agent-config.yaml << 'ACEOF'\n"
                f"{indented_ac}\n"
                "    ACEOF\n"
                "    chown -R cloud-user:cloud-user /home/cloud-user/ocp-install\n"
            )

        # Write ImageTagMirrorSet for pull-through registry (tag-based pulls like catalog sources)
        if pull_through_registry and pull_through_registry.get("enabled"):
            ptr_url = pull_through_registry["url"]
            itms_yaml = (
                "apiVersion: config.openshift.io/v1\n"
                "kind: ImageTagMirrorSet\n"
                "metadata:\n"
                "  name: pull-through-registry-tags\n"
                "spec:\n"
                "  imageTagMirrors:\n"
            )
            for source, org in pull_through_registry.get("orgs", {}).items():
                itms_yaml += (
                    f"    - source: {source}\n"
                    f"      mirrors:\n"
                    f"        - {ptr_url}/{org}\n"
                )
            indented_itms = "\n".join("    " + line for line in itms_yaml.split("\n"))
            node["data"]["ciUserData"] += (
                "  - |\n"
                + _guard
                + "    mkdir -p /home/cloud-user/ocp-install/openshift\n"
                "    cat > /home/cloud-user/ocp-install/openshift/itms-pull-through.yaml << 'ITMSEOF'\n"
                f"{indented_itms}\n"
                "    ITMSEOF\n"
                "    chown -R cloud-user:cloud-user /home/cloud-user/ocp-install/openshift\n"
            )

        # Launch OCP installer in background
        node["data"][
            "ciUserData"
        ] += "  - sudo -u cloud-user nohup /home/cloud-user/install-ocp.sh > /home/cloud-user/install.log 2>&1 &\n"

        # Desktop setup script — written as a file to avoid nested quoting issues
        node["data"]["ciUserData"] += (
            "  - |\n" + _guard + "    cat > /root/setup-desktop.sh << 'DESKTOPEOF'\n"
            "    #!/bin/bash\n"
            "    set -x\n"
            "    dnf remove -y gnome-initial-setup gnome-software gnome-tour subscription-manager-cockpit 2>/dev/null\n"
            "    sed -i 's|^ExecStart=.*gsd-subman|#ExecStart=/usr/libexec/gsd-subman|' /lib/systemd/user/org.gnome.SettingsDaemon.Subscription.service 2>/dev/null\n"
            "    systemctl disable --now rhsmcertd 2>/dev/null\n"
            "    systemctl mask rhsmcertd 2>/dev/null\n"
            "    mkdir -p /etc/skel/.config\n"
            "    echo yes > /etc/skel/.config/gnome-initial-setup-done\n"
            "    for u in root cloud-user; do\n"
            "      d=$(eval echo ~$u)\n"
            "      mkdir -p $d/.config\n"
            "      echo yes > $d/.config/gnome-initial-setup-done\n"
            "      chown -R $u:$u $d/.config\n"
            "    done\n"
            "    if rpm -q ptyxis >/dev/null 2>&1; then\n"
            "      TERM_APP=org.gnome.Ptyxis.desktop\n"
            "    else\n"
            "      TERM_APP=org.gnome.Terminal.desktop\n"
            "    fi\n"
            "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/shell/favorite-apps \"['$TERM_APP', 'firefox.desktop']\"\n"
            "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/desktop/interface/overlay-scrolling false\n"
            "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/desktop/screensaver/lock-enabled false\n"
            '    sudo -u cloud-user dbus-run-session dconf write /org/gnome/desktop/session/idle-delay "uint32 0"\n'
            "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/settings-daemon/plugins/power/sleep-inactive-ac-type \"'nothing'\"\n"
            "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/settings-daemon/plugins/power/idle-dim false\n"
            "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/desktop/interface/color-scheme \"'prefer-dark'\"\n"
            "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/desktop/interface/gtk-theme \"'Adwaita-dark'\"\n"
            "    sudo -u cloud-user dbus-run-session gnome-extensions enable dash-to-dock@micxgx.gmail.com 2>/dev/null\n"
            "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/shell/extensions/dash-to-dock/dock-fixed false\n"
            "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/shell/extensions/dash-to-dock/autohide true\n"
            "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/shell/extensions/dash-to-dock/intellihide true\n"
            "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/shell/extensions/dash-to-dock/show-trash false\n"
            "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/shell/extensions/dash-to-dock/show-mounts false\n"
            "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/mutter/dynamic-workspaces false\n"
            "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/desktop/wm/preferences/num-workspaces 1\n"
            "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/desktop/wm/preferences/button-layout \"'appmenu:minimize,maximize,close'\"\n"
            "    # Ptyxis terminal: Hurtado palette (dark theme with readable Ansible output)\n"
            "    if rpm -q ptyxis >/dev/null 2>&1; then\n"
            '      PROFILE_UUID=$(sudo -u cloud-user dbus-run-session dconf read /org/gnome/Ptyxis/default-profile-uuid 2>/dev/null | tr -d "\'")\n'
            '      if [ -z "$PROFILE_UUID" ]; then\n'
            "        PROFILE_UUID=$(cat /proc/sys/kernel/random/uuid | tr -d '-' | head -c 32)\n"
            "        sudo -u cloud-user dbus-run-session dconf write /org/gnome/Ptyxis/default-profile-uuid \"'$PROFILE_UUID'\"\n"
            "        sudo -u cloud-user dbus-run-session dconf write /org/gnome/Ptyxis/profile-uuids \"['$PROFILE_UUID']\"\n"
            "      fi\n"
            "      sudo -u cloud-user dbus-run-session dconf write /org/gnome/Ptyxis/Profiles/$PROFILE_UUID/palette \"'Hurtado'\"\n"
            "    fi\n"
            "    MONITORS_XML='<monitors version=\"2\"><configuration><logicalmonitor><x>0</x><y>0</y><scale>1</scale><primary>yes</primary><monitor><monitorspec><connector>Virtual-1</connector><vendor>unknown</vendor><product>unknown</product><serial>unknown</serial></monitorspec><mode><width>1920</width><height>1080</height><rate>60</rate></mode></monitor></logicalmonitor></configuration></monitors>'\n"
            "    for u in root cloud-user; do\n"
            "      d=$(eval echo ~$u)\n"
            "      mkdir -p $d/.config\n"
            '      echo "$MONITORS_XML" > $d/.config/monitors.xml\n'
            "      chown -R $u:$u $d/.config\n"
            "    done\n"
            "    mkdir -p /var/lib/gdm/.config\n"
            '    echo "$MONITORS_XML" > /var/lib/gdm/.config/monitors.xml\n'
            "    chown -R gdm:gdm /var/lib/gdm/.config\n"
            "    sed -i '/^\\[daemon\\]/a AutomaticLoginEnable=True' /etc/gdm/custom.conf\n"
            "    sed -i '/^AutomaticLoginEnable/a AutomaticLogin=cloud-user' /etc/gdm/custom.conf\n"
            "    grep -q KUBECONFIG /home/cloud-user/.bashrc || echo 'export KUBECONFIG=/home/cloud-user/ocp-install/auth/kubeconfig' >> /home/cloud-user/.bashrc\n"
            "    systemctl set-default graphical.target\n"
            "    systemctl isolate graphical.target\n"
            "    DESKTOPEOF\n"
            "    chmod 755 /root/setup-desktop.sh\n"
            "    [ -f /var/log/desktop-install.log ] || nohup /root/setup-desktop.sh > /var/log/desktop-install.log 2>&1 &\n"
        )

        # Firefox enterprise policies
        console_url = (
            f"https://console-openshift-console.apps.{cluster_name}.{base_domain}"
        )
        node["data"]["ciUserData"] += (
            "  - |\n" + _guard + "    mkdir -p /etc/firefox/policies\n"
            "    cat > /etc/firefox/policies/policies.json << 'FPEOF'\n"
            "    {\n"
            '      "policies": {\n'
            f'        "Homepage": {{"URL": "{console_url}", "Locked": true, "StartPage": "homepage"}},\n'
            '        "OverrideFirstRunPage": "",\n'
            '        "OverridePostUpdatePage": "",\n'
            '        "UserMessaging": {"WhatsNew": false, "ExtensionRecommendations": false, "FeatureRecommendations": false, "UrlbarInterventions": false, "SkipOnboarding": true, "MoreFromMozilla": false},\n'
            '        "DisableTelemetry": true,\n'
            '        "Certificates": {"ImportEnterpriseRoots": true},\n'
            '        "NoDefaultBookmarks": true,\n'
            '        "DontCheckDefaultBrowser": true,\n'
            '        "DisableAppUpdate": true\n'
            "      }\n"
            "    }\n"
            "    FPEOF\n"
        )
        # Firefox default prefs (suppress crash recovery, update prompts)
        node["data"]["ciUserData"] += (
            "  - |\n"
            + _guard
            + "    FIREFOX_DIR=$(find /usr/lib64/firefox /usr/lib/firefox -maxdepth 0 2>/dev/null | head -1)\n"
            '    if [ -n "$FIREFOX_DIR" ]; then\n'
            "      mkdir -p $FIREFOX_DIR/defaults/pref\n"
            "      cat > $FIREFOX_DIR/defaults/pref/autoconfig.js << 'ACEOF'\n"
            '    pref("browser.sessionstore.resume_from_crash", false);\n'
            '    pref("browser.shell.checkDefaultBrowser", false);\n'
            '    pref("browser.startup.homepage_override.mstone", "ignore");\n'
            '    pref("browser.disableResetPrompt", true);\n'
            '    pref("browser.slowStartup.notificationDisabled", true);\n'
            '    pref("browser.laterrun.enabled", false);\n'
            "    ACEOF\n"
            "    fi\n"
        )

        # Static IP on BMC NIC — use MAC matching for firmware-agnostic naming
        bmc_ip = str(ipaddress.IPv4Address(bastion_bmc_ip))
        nics = node["data"].get("nics", [])
        cluster_mac = nics[0]["mac"] if len(nics) > 0 else ""
        bmc_mac = nics[1]["mac"] if len(nics) > 1 else ""
        node["data"]["ciNetworkConfig"] = (
            "version: 2\n"
            "ethernets:\n"
            "  cluster-nic:\n"
            f"    match:\n"
            f'      macaddress: "{cluster_mac}"\n'
            "    dhcp4: true\n"
            "  bmc-nic:\n"
            f"    match:\n"
            f'      macaddress: "{bmc_mac}"\n'
            "    addresses:\n"
            f"      - {bmc_ip}/24\n"
        )
        break


def _build_install_config(
    topology,
    template_id,
    cluster_name,
    base_domain,
    api_vip,
    ingress_vip,
    password,
    pull_secret_json,
    ssh_pub_key,
    pull_through_registry=None,
):
    # Count actual CP and worker nodes from topology
    cp_nodes_count = sum(
        1
        for n in topology.get("nodes", [])
        if n.get("type") == "vmNode"
        and n.get("data", {}).get("tags", {}).get("AnsibleGroup") == "controllers"
    )
    worker_nodes_count = sum(
        1
        for n in topology.get("nodes", [])
        if n.get("type") == "vmNode"
        and n.get("data", {}).get("tags", {}).get("AnsibleGroup") == "workers"
    )
    num_masters = (
        cp_nodes_count if cp_nodes_count > 0 else (1 if template_id == "ocp-sno" else 3)
    )
    num_workers = worker_nodes_count

    ic_lines = [
        "apiVersion: v1",
        f"baseDomain: {base_domain}",
        "metadata:",
        f"  name: {cluster_name}",
        "compute:",
        "  - name: worker",
        f"    replicas: {num_workers}",
        "    architecture: amd64",
        "controlPlane:",
        "  name: master",
        f"  replicas: {num_masters}",
        "  architecture: amd64",
        "networking:",
        "  networkType: OVNKubernetes",
        "  clusterNetwork:",
        "    - cidr: 10.128.0.0/14",
        "      hostPrefix: 23",
        "  serviceNetwork:",
        "    - 172.30.0.0/16",
        "  machineNetwork:",
        f"    - cidr: {_find_cluster_cidr(topology)}",
    ]
    if num_masters == 1 and num_workers == 0:
        ic_lines.extend(
            [
                "platform:",
                "  none: {}",
            ]
        )
    else:
        ic_lines.extend(
            [
                "platform:",
                "  baremetal:",
                "    apiVIPs:",
                f"      - {api_vip}",
                "    ingressVIPs:",
                f"      - {ingress_vip}",
                "    hosts:",
            ]
        )
    is_sno = num_masters == 1 and num_workers == 0
    if not is_sno:
        for node in topology.get("nodes", []):
            if node.get("type") != "vmNode":
                continue
            td = node.get("data", {})
            if not td.get("bmcEnabled") or not td.get("bmcIp"):
                continue
            group = td.get("tags", {}).get("AnsibleGroup", "")
            if group not in ("controllers", "workers"):
                continue
            vm_name = td.get("name", "")
            boot_mac = td.get("nics", [{}])[0].get("mac", "")
            if not _NAME_RE.match(vm_name) or not _MAC_RE.match(boot_mac):
                continue
            role = "master" if group == "controllers" else "worker"
            ic_lines.extend(
                [
                    f"      - name: {vm_name}",
                    f"        role: {role}",
                    f"        bootMACAddress: {boot_mac}",
                ]
            )
    if pull_secret_json:
        ic_lines.append(f"pullSecret: '{pull_secret_json}'")
    if ssh_pub_key:
        ic_lines.append(f"sshKey: '{ssh_pub_key}'")

    if pull_through_registry and pull_through_registry.get("enabled"):
        ptr_url = pull_through_registry["url"]
        ic_lines.append("imageDigestSources:")
        for source, org in pull_through_registry.get("orgs", {}).items():
            ic_lines.extend(
                [
                    "- mirrors:",
                    f"  - {ptr_url}/{org}",
                    f"  source: {source}",
                ]
            )

    return "\n".join(ic_lines)


def _build_agent_config(
    topology, cluster_name, base_domain, api_vip="10.0.0.2", ingress_vip="10.0.0.3"
):
    """Build agent-config.yaml with BMC host details for Redfish virtual media boot."""
    cluster_cidr = _find_cluster_cidr(topology)
    net = ipaddress.ip_network(cluster_cidr, strict=False)
    gateway_ip = str(net.network_address + 1)
    prefix_len = net.prefixlen

    rendezvous_ip = ""
    hosts_yaml = ""
    for node in topology.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        td = node.get("data", {})
        if not td.get("bmcEnabled") or not td.get("bmcIp"):
            continue
        group = td.get("tags", {}).get("AnsibleGroup", "")
        if group not in ("controllers", "workers"):
            continue
        vm_name = td.get("name", "")
        td["bmcIp"]
        cluster_ip = td.get("nics", [{}])[0].get("ip", "")
        boot_mac = td.get("nics", [{}])[0].get("mac", "")
        if not _NAME_RE.match(vm_name) or not _MAC_RE.match(boot_mac):
            continue
        role = "master" if group == "controllers" else "worker"
        if not rendezvous_ip and cluster_ip:
            rendezvous_ip = cluster_ip

        hosts_yaml += (
            f"    - hostname: {vm_name}\n"
            f"      role: {role}\n"
            f"      interfaces:\n"
            f"        - name: cluster-nic\n"
            f"          macAddress: {boot_mac}\n"
            f"      networkConfig:\n"
            f"        interfaces:\n"
            f"          - name: cluster-nic\n"
            f"            type: ethernet\n"
            f"            state: up\n"
            f"            identifier: mac-address\n"
            f"            mac-address: {boot_mac}\n"
            f"            ipv4:\n"
            f"              enabled: true\n"
            f"              address:\n"
            f"                - ip: {cluster_ip}\n"
            f"                  prefix-length: {prefix_len}\n"
            f"              dhcp: false\n"
            f"        dns-resolver:\n"
            f"          config:\n"
            f"            server:\n"
            f"              - {gateway_ip}\n"
            f"        routes:\n"
            f"          config:\n"
            f"            - destination: 0.0.0.0/0\n"
            f"              next-hop-address: {gateway_ip}\n"
            f"              next-hop-interface: cluster-nic\n"
        )

    if not rendezvous_ip:
        rendezvous_ip = str(net.network_address + 10)

    ac_lines = [
        "apiVersion: v1beta1",
        "kind: AgentConfig",
        "metadata:",
        f"  name: {cluster_name}",
        f"rendezvousIP: {rendezvous_ip}",
        "additionalNTPSources:",
        "  - clock.redhat.com",
        "  - pool.ntp.org",
        "hosts:",
    ]
    ac_lines.append(hosts_yaml.rstrip())

    return "\n".join(ac_lines)


def _build_install_script(
    ocp_version,
    auto_install,
    bmc_password="",
    bmc_ips_str="",
    cluster_name="ocp",
    base_domain="ocp.local",
    topology=None,
):
    return (
        "  - |\n"
        "    cat > /home/cloud-user/install-ocp.sh << 'SCRIPTEOF'\n"
        "    #!/bin/bash\n"
        "    set -e\n"
        "    cd /home/cloud-user\n"
        "    \n"
        "    # Skip if cluster is already installed (pattern deploy)\n"
        "    if [ -f /home/cloud-user/ocp-install/auth/kubeconfig ]; then\n"
        "      echo 'Cluster already installed, skipping.'\n"
        "      exit 0\n"
        "    fi\n"
        "    \n"
        "    # Wait for network\n"
        "    echo 'Waiting for network...'\n"
        "    for i in $(seq 1 15); do\n"
        "      ping -c1 -W2 8.8.8.8 &>/dev/null && break\n"
        "      sleep 2\n"
        "    done\n"
        "    if ! ping -c1 -W2 8.8.8.8 &>/dev/null; then\n"
        "      echo 'ERROR: No network connectivity after 30 seconds'\n"
        "      exit 1\n"
        "    fi\n"
        "    echo 'Network OK'\n"
        "    \n"
        f"    OCP_VERSION={ocp_version}\n"
        "    \n"
        "    # Download openshift-install and oc if not present\n"
        "    if [ ! -f openshift-install ]; then\n"
        '      echo "Downloading openshift-install $OCP_VERSION..."\n'
        "      curl -L -o /tmp/openshift-install.tar.gz https://mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp/stable-$OCP_VERSION/openshift-install-linux.tar.gz\n"
        "      tar xzf /tmp/openshift-install.tar.gz && rm -f /tmp/openshift-install.tar.gz\n"
        '      echo "Downloading oc client..."\n'
        "      curl -L -o /tmp/openshift-client.tar.gz https://mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp/stable-$OCP_VERSION/openshift-client-linux.tar.gz\n"
        "      tar xzf /tmp/openshift-client.tar.gz && rm -f /tmp/openshift-client.tar.gz\n"
        "      sudo mv oc kubectl /usr/bin/\n"
        '      echo "Downloaded openshift-install and oc"\n'
        "    fi\n"
        "    \n"
        "    echo ''\n"
        "    echo '================================================'\n"
        "    echo 'OCP Agent-Based Installer Ready'\n"
        "    echo '================================================'\n"
        "    echo ''\n"
        "    echo 'install-config.yaml:  ~/ocp-install/install-config.yaml'\n"
        "    echo 'agent-config.yaml:    ~/ocp-install/agent-config.yaml'\n"
        "    echo 'Pull secret:          ~/pull-secret.json'\n"
        "    echo 'openshift-install:    ~/openshift-install'\n"
        "    echo ''\n"
        "    echo 'To create agent ISO and install:'\n"
        "    echo '  cd ~/ocp-install'\n"
        "    echo '  ~/openshift-install agent create image --dir .'\n"
        "    echo '  # Serve ISO and boot nodes via BMC'\n"
        "    echo '  ~/openshift-install agent wait-for install-complete --dir . --log-level debug'\n"
        "    echo ''\n"
        "    echo ''\n"
        + (
            "    # Auto-run agent-based installer\n"
            "    INSTALL_START=$(date +%s)\n"
            '    echo "Install started at $(date)"\n'
            f"    BMC_PASS='{bmc_password}'\n"
            "    echo 'Creating agent ISO...'\n"
            "    cd /home/cloud-user/ocp-install\n"
            "    cp install-config.yaml install-config.yaml.bak\n"
            "    cp agent-config.yaml agent-config.yaml.bak\n"
            "    [ -d openshift ] && cp -a openshift openshift.bak\n"
            + _generate_ocp_mount_script(topology or {})
            + '    /home/cloud-user/openshift-install agent create image --dir . --log-level debug 2>&1 | awk \'{print strftime("[%H:%M:%S]") " " $0; fflush()}\' | tee /home/cloud-user/create-image.log\n'
            "    \n"
            "    echo 'Agent ISO created. Serving via HTTP and booting nodes...'\n"
            "    # Open firewall for ISO serving (script runs as cloud-user)\n"
            "    sudo firewall-cmd --add-port=8080/tcp --permanent 2>/dev/null && sudo firewall-cmd --reload 2>/dev/null || true\n"
            "    cd /home/cloud-user/ocp-install\n"
            "    nohup python3 -m http.server 8080 > /tmp/http-server.log 2>&1 &\n"
            "    HTTP_PID=$!\n"
            '    echo "HTTP server PID: $HTTP_PID"\n'
            "    \n"
            "    # Boot each CP node via Redfish virtual media\n"
            "    BASTION_IP=$(hostname -I | awk '{print $1}')\n"
            '    ISO_URL="http://${BASTION_IP}:8080/agent.x86_64.iso"\n'
            '    echo "ISO URL: $ISO_URL"\n'
            "    \n"
            f"    for BMC_IP in {bmc_ips_str}; do\n"
            '      echo "Mounting ISO on BMC $BMC_IP..."\n'
            "      # Get system UUID from sushy\n"
            "      SYS_ID=$(curl -s -u admin:$BMC_PASS http://${BMC_IP}:8000/redfish/v1/Systems | python3 -c \"import json,sys; print(json.load(sys.stdin)['Members'][0]['@odata.id'].split('/')[-1])\")\n"
            '      echo "  System: $SYS_ID"\n'
            "      # Insert virtual media (Systems path, HTTP, with auth)\n"
            '      curl -s -u admin:$BMC_PASS -X POST "http://${BMC_IP}:8000/redfish/v1/Systems/${SYS_ID}/VirtualMedia/Cd/Actions/VirtualMedia.InsertMedia" \\\n'
            "        -H 'Content-Type: application/json' \\\n"
            '        -d "{\\"Image\\": \\"${ISO_URL}\\", \\"Inserted\\": true, \\"WriteProtected\\": true}" || true\n'
            "      # Reboot — UEFI boot order is hd,cdrom so empty disk falls through to ISO\n"
            "      # After agent writes CoreOS to disk, next reboot boots from disk first\n"
            '      curl -s -u admin:$BMC_PASS -X POST "http://${BMC_IP}:8000/redfish/v1/Systems/${SYS_ID}/Actions/ComputerSystem.Reset" \\\n'
            "        -H 'Content-Type: application/json' \\\n"
            '        -d \'{"ResetType": "ForceRestart"}\' || true\n'
            '      echo "Booted $BMC_IP from ISO"\n'
            "    done\n"
            "    \n"
            "    \n"
            "    echo 'Waiting for cluster installation to complete...'\n"
            '    /home/cloud-user/openshift-install agent wait-for install-complete --dir /home/cloud-user/ocp-install --log-level debug 2>&1 | awk \'{print strftime("[%H:%M:%S]") " " $0; fflush()}\'\n'
            "    OCP_EXIT=${PIPESTATUS[0]}\n"
            "    INSTALL_END=$(date +%s)\n"
            "    ELAPSED=$(( INSTALL_END - INSTALL_START ))\n"
            "    echo ''\n"
            "    echo '================================================'\n"
            "    if [ $OCP_EXIT -ne 0 ]; then\n"
            '    echo "Install FAILED at $(date) (exit code $OCP_EXIT)"\n'
            '    echo "Total time: $(( ELAPSED / 60 )) min $(( ELAPSED % 60 )) sec"\n'
            "    echo '================================================'\n"
            "    kill $HTTP_PID 2>/dev/null\n"
            "    exit 1\n"
            "    fi\n"
            '    echo "Install completed at $(date)"\n'
            '    echo "Total time: $(( ELAPSED / 60 )) min $(( ELAPSED % 60 )) sec"\n'
            "    echo '================================================'\n"
            "    # Eject agent ISO via Redfish virtual media\n"
            "    echo 'Ejecting agent ISO from nodes...'\n"
            f"    for BMC_IP in {bmc_ips_str}; do\n"
            "      SYS_ID=$(curl -s -u admin:$BMC_PASS http://${BMC_IP}:8000/redfish/v1/Systems | python3 -c \"import json,sys; print(json.load(sys.stdin)['Members'][0]['@odata.id'].split('/')[-1])\" 2>/dev/null)\n"
            "      curl -s -u admin:$BMC_PASS -X POST \"http://${BMC_IP}:8000/redfish/v1/Systems/${SYS_ID}/VirtualMedia/Cd/Actions/VirtualMedia.EjectMedia\" -H 'Content-Type: application/json' -d '{}' >/dev/null 2>&1\n"
            "    done\n"
            "    # Write static MOTD with cluster credentials\n"
            "    KUBEADMIN_PW=$(cat /home/cloud-user/ocp-install/auth/kubeadmin-password)\n"
            f"    printf '\\nOpenShift Console: https://console-openshift-console.apps.{cluster_name}.{base_domain}\\nUsername:          kubeadmin\\nPassword:          %s\\n\\n' \"$KUBEADMIN_PW\" | sudo tee /etc/motd >/dev/null\n"
            "    # Trust the OCP CA so Firefox doesn't show cert warnings\n"
            "    export KUBECONFIG=/home/cloud-user/ocp-install/auth/kubeconfig\n"
            "    oc get secret -n openshift-ingress router-certs-default -o jsonpath='{.data.tls\\.crt}' 2>/dev/null | base64 -d | sudo tee /etc/pki/ca-trust/source/anchors/ocp-ingress.pem >/dev/null && sudo update-ca-trust\n"
            "    # Save kubeadmin password into Firefox password manager via Selenium\n"
            "    pip3 install -q selenium 2>/dev/null\n"
            "    curl -sL https://github.com/mozilla/geckodriver/releases/download/v0.37.0/geckodriver-v0.37.0-linux64.tar.gz | sudo tar xz -C /usr/local/bin/\n"
            "    # Create Firefox profile if it doesn't exist\n"
            "    if ! ls /home/cloud-user/.mozilla/firefox/*.default-default/ >/dev/null 2>&1; then\n"
            "      export DISPLAY=:0 WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000 MOZ_ENABLE_WAYLAND=1\n"
            "      timeout 10 firefox --headless >/dev/null 2>&1 || true\n"
            "      sleep 2\n"
            "    fi\n"
            "    cat > /home/cloud-user/ocp-autologin.py << 'SELENEOF'\n"
            "    import time, glob, os, sys\n"
            "    from selenium import webdriver\n"
            "    from selenium.webdriver.common.by import By\n"
            "    from selenium.webdriver.firefox.options import Options\n"
            "    from selenium.webdriver.support.ui import WebDriverWait\n"
            "    from selenium.webdriver.support import expected_conditions as EC\n"
            "    console_url = sys.argv[1]\n"
            "    pw = open(os.path.expanduser('~cloud-user/ocp-install/auth/kubeadmin-password')).read().strip()\n"
            "    profile = glob.glob('/home/cloud-user/.mozilla/firefox/*.default-default')[0]\n"
            "    opts = Options()\n"
            "    opts.add_argument('-profile')\n"
            "    opts.add_argument(profile)\n"
            "    opts.add_argument('-remote-allow-system-access')\n"
            "    opts.accept_insecure_certs = True\n"
            "    opts.set_preference('signon.rememberSignons', True)\n"
            "    opts.set_preference('signon.autofillForms', True)\n"
            "    opts.set_preference('signon.storeWhenAutocompleteOff', True)\n"
            "    opts.set_preference('browser.startup.page', 1)\n"
            "    driver = webdriver.Firefox(options=opts)\n"
            "    try:\n"
            "        driver.get(console_url)\n"
            "        wait = WebDriverWait(driver, 30)\n"
            "        u = wait.until(EC.presence_of_element_located((By.ID, 'inputUsername')))\n"
            "        p = driver.find_element(By.ID, 'inputPassword')\n"
            "        u.clear(); u.send_keys('kubeadmin')\n"
            "        p.clear(); p.send_keys(pw)\n"
            "        driver.find_element(By.CSS_SELECTOR, 'button[type=submit]').click()\n"
            "        time.sleep(3)\n"
            "        driver.set_context('chrome')\n"
            "        for _ in range(15):\n"
            "            try:\n"
            "                driver.find_element(By.CSS_SELECTOR, 'popupnotification[id*=password] button.popup-notification-primary-button').click()\n"
            "                print('Password saved to Firefox'); break\n"
            "            except Exception: pass\n"
            "            time.sleep(0.5)\n"
            "        driver.set_context('content')\n"
            "        time.sleep(1)\n"
            "    finally:\n"
            "        driver.quit()\n"
            "    SELENEOF\n"
            f"    sudo -u cloud-user bash -c 'export DISPLAY=:0 WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000 MOZ_ENABLE_WAYLAND=1; python3 /home/cloud-user/ocp-autologin.py https://console-openshift-console.apps.{cluster_name}.{base_domain}' 2>&1 || true\n"
            "    # Cleanup: remove cached ISO, temp files, and pull secret from disk\n"
            "    rm -f /home/cloud-user/pull-secret.json\n"
            "    rm -rf /home/cloud-user/.cache/agent/ /tmp/http-server.log /tmp/cookies /tmp/*.zip /var/tmp/dnf-*\n"
            "    dnf clean all 2>/dev/null\n"
            "    # Kill the HTTP server used to serve the agent ISO\n"
            "    kill $HTTP_PID 2>/dev/null\n"
            if auto_install
            else ""
        )
        + "    SCRIPTEOF\n"
        "    chown cloud-user:cloud-user /home/cloud-user/install-ocp.sh\n"
        "    chmod 755 /home/cloud-user/install-ocp.sh\n"
    )
