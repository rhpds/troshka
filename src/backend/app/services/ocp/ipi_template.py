"""
OCP IPI (Installer Provisioned Infrastructure) template customization.

Takes a base topology from topology_templates.py and adds OCP-specific
configuration: DNS records, bastion cloud-init, install-config.yaml,
install script, BMC NIC config.
"""
import ipaddress
import uuid


def customize_topology(topology: dict, template_id: str, config: dict) -> dict:
    """Apply OCP IPI configuration to a base topology.

    config keys:
        cluster_name, base_domain, ocp_version, bastion_password,
        pull_secret_json, ssh_pub_key, bastion_image, bastion_iso,
        bastion_bmc_ip, auto_install_ocp
    """
    cluster_name = config.get("cluster_name", "ocp")
    base_domain = config.get("base_domain", "ocp.local")
    ocp_version = config.get("ocp_version", "4.20")
    bastion_password = config.get("bastion_password", "")
    pull_secret_json = config.get("pull_secret_json", "")
    ssh_pub_key = config.get("ssh_pub_key", "")
    bastion_image = config.get("bastion_image")
    bastion_iso = config.get("bastion_iso")
    bastion_bmc_ip = config.get("bastion_bmc_ip", "192.168.100.50")
    auto_install_ocp = config.get("auto_install_ocp", True)
    ssh_key_ids = config.get("ssh_key_ids", [])
    ssh_keys = config.get("ssh_keys", [])

    api_vip = "10.0.0.2"
    ingress_vip = "10.0.0.3"

    _setup_dns_records(topology, cluster_name, base_domain, api_vip, ingress_vip)
    _attach_bastion_image(topology, bastion_image)
    _attach_bastion_iso(topology, bastion_iso)
    _setup_bastion_cloud_init(
        topology, bastion_password, ssh_pub_key, ssh_key_ids, ssh_keys,
        bastion_iso, pull_secret_json, cluster_name, base_domain,
        ocp_version, auto_install_ocp, template_id, api_vip,
        ingress_vip, bastion_bmc_ip,
    )

    return topology


def _setup_dns_records(topology, cluster_name, base_domain, api_vip, ingress_vip):
    for node in topology.get("nodes", []):
        if (node.get("type") == "networkNode"
                and node.get("data", {}).get("subtype") == "network"
                and node.get("data", {}).get("networkType") != "bmc"):
            node["data"]["dns"] = True
            node["data"]["dnsDomain"] = base_domain
            node["data"]["dnsRecords"] = [
                {"name": f"api.{cluster_name}.{base_domain}", "ip": api_vip},
                {"name": f"api-int.{cluster_name}.{base_domain}", "ip": api_vip},
                {"name": f".apps.{cluster_name}.{base_domain}", "ip": ingress_vip},
            ]
            break


def _attach_bastion_image(topology, bastion_image):
    if not bastion_image:
        return
    for node in topology.get("nodes", []):
        if node.get("type") == "storageNode" and node.get("data", {}).get("name") == "bastion-disk":
            node["data"]["source"] = "library"
            node["data"]["libraryItemId"] = bastion_image["id"]
            node["data"]["libraryItemName"] = bastion_image["name"]
            node["data"]["libraryItemSize"] = bastion_image["size_gb"]
            node["data"]["size"] = max(bastion_image["size_gb"], node["data"].get("size", 0))
            break


def _attach_bastion_iso(topology, bastion_iso):
    if not bastion_iso:
        return
    bastion_vm = None
    for node in topology.get("nodes", []):
        if node.get("type") == "vmNode" and node.get("data", {}).get("name") == "bastion":
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
            "size": bastion_iso["size_bytes"] // (1024 ** 3) if bastion_iso.get("size_bytes") else 10,
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
        "style": {"stroke": "rgba(251,191,36,0.6)", "strokeWidth": 2, "strokeDasharray": "4 4"},
        "animated": False,
        "className": "edge-storage-pulse",
    }
    topology["nodes"].append(iso_node)
    topology["edges"].append(iso_edge)


def _setup_bastion_cloud_init(
    topology, password, ssh_pub_key, ssh_key_ids, ssh_keys,
    bastion_iso, pull_secret_json, cluster_name, base_domain,
    ocp_version, auto_install_ocp, template_id, api_vip,
    ingress_vip, bastion_bmc_ip,
):
    for node in topology.get("nodes", []):
        if node.get("type") != "vmNode" or node.get("data", {}).get("name") != "bastion":
            continue

        node["data"]["cloudInit"] = True
        if password:
            node["data"]["ciCloudUserPassword"] = password
        if ssh_key_ids:
            node["data"]["ciSshKeyIds"] = ssh_key_ids
        if ssh_keys:
            node["data"]["ciSshKeys"] = ssh_keys

        # DVD mount + yum repos + packages
        if bastion_iso:
            node["data"]["ciUserData"] = (
                "runcmd:\n"
                "  - nmcli con up \"cloud-init ens3\" 2>/dev/null || true\n"
                "  - mkdir -p /mnt/rhel-dvd\n"
                "  - |\n"
                "    # Find the DVD ISO device (not the seed ISO)\n"
                "    for dev in /dev/sr0 /dev/sr1 /dev/cdrom; do\n"
                "      if blkid $dev 2>/dev/null | grep -qi 'LABEL=.*RHEL\\|TYPE=.*iso9660'; then\n"
                "        if ! blkid $dev 2>/dev/null | grep -qi 'LABEL=.*cidata'; then\n"
                "          echo \"$dev /mnt/rhel-dvd iso9660 ro,nofail 0 0\" >> /etc/fstab\n"
                "          mount /mnt/rhel-dvd\n"
                "          break\n"
                "        fi\n"
                "      fi\n"
                "    done\n"
                "  - |\n"
                "    cat > /etc/yum.repos.d/rhel-dvd.repo << 'EOF'\n"
                "    [rhel-dvd-baseos]\n"
                "    name=RHEL DVD BaseOS\n"
                "    baseurl=file:///mnt/rhel-dvd/BaseOS\n"
                "    enabled=1\n"
                "    gpgcheck=0\n"
                "    [rhel-dvd-appstream]\n"
                "    name=RHEL DVD AppStream\n"
                "    baseurl=file:///mnt/rhel-dvd/AppStream\n"
                "    enabled=1\n"
                "    gpgcheck=0\n"
                "    EOF\n"
                "  - dnf install -y git ansible-core python3-pip bind-utils tmux qemu-kvm libvirt\n"
                "  - for s in virtqemud virtnetworkd virtstoraged virtnodedevd virtsecretd virtinterfaced virtlogd virtproxyd; do systemctl enable --now ${s}.socket 2>/dev/null; done\n"
                "  - usermod -aG libvirt cloud-user\n"
                "  - echo 0 > /sys/kernel/mm/ksm/run\n"
                "  - sysctl -w kernel.watchdog_thresh=60\n"
                "  - mkdir -p /home/cloud-user/.config/libvirt\n"
                "  - echo 'uri_default = \"qemu:///system\"' > /home/cloud-user/.config/libvirt/libvirt.conf\n"
                "  - chown -R cloud-user:cloud-user /home/cloud-user/.config\n"
            )

        # Pull secret
        if pull_secret_json:
            node["data"]["ciUserData"] += (
                "  - |\n"
                "    cat > /home/cloud-user/pull-secret.json << 'PULLSECRETEOF'\n"
                f"    {pull_secret_json}\n"
                "    PULLSECRETEOF\n"
                "    chown cloud-user:cloud-user /home/cloud-user/pull-secret.json\n"
                "    chmod 600 /home/cloud-user/pull-secret.json\n"
            )

        # install-config.yaml
        install_config = _build_install_config(
            topology, template_id, cluster_name, base_domain,
            api_vip, ingress_vip, password, pull_secret_json, ssh_pub_key,
        )

        # install-ocp.sh script
        node["data"]["ciUserData"] += _build_install_script(ocp_version, auto_install_ocp)

        # Write install-config.yaml to bastion
        if install_config:
            indented_ic = "\n".join("    " + line for line in install_config.split("\n"))
            node["data"]["ciUserData"] += (
                "  - |\n"
                "    mkdir -p /home/cloud-user/ocp-install\n"
                "    cat > /home/cloud-user/ocp-install/install-config.yaml << 'ICEOF'\n"
                f"{indented_ic}\n"
                "    ICEOF\n"
                "    chown -R cloud-user:cloud-user /home/cloud-user/ocp-install\n"
            )

        # Launch installer in tmux
        node["data"]["ciUserData"] += (
            "  - su - cloud-user -c 'setsid tmux new-session -d -s setup /home/cloud-user/install-ocp.sh'\n"
        )

        # Bridge on ens3 so the nested bootstrap VM can join the cluster network
        bmc_ip = str(ipaddress.IPv4Address(bastion_bmc_ip))
        node["data"]["ciNetworkConfig"] = (
            "version: 2\n"
            "ethernets:\n"
            "  ens3:\n"
            "    dhcp4: false\n"
            "  ens4:\n"
            "    addresses:\n"
            f"      - {bmc_ip}/24\n"
            "bridges:\n"
            "  br-cluster:\n"
            "    interfaces: [ens3]\n"
            "    dhcp4: true\n"
        )
        break


def _build_install_config(topology, template_id, cluster_name, base_domain,
                          api_vip, ingress_vip, bmc_password, pull_secret_json, ssh_pub_key):
    num_workers = 0 if template_id == "ocp-compact" else 2

    # Collect BMC hosts from topology
    bmc_hosts_yaml = ""
    for node in topology.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        td = node.get("data", {})
        if not td.get("bmcEnabled") or not td.get("bmcIp"):
            continue
        vm_name = td.get("name", "")
        if "bootstrap" in vm_name:
            continue
        bmc_ip_addr = td["bmcIp"]
        boot_mac = td.get("nics", [{}])[0].get("mac", "")
        role = "master" if "cp-" in vm_name or "sno" in vm_name else "worker"
        bmc_hosts_yaml += (
            f"      - name: {vm_name}\n"
            f"        role: {role}\n"
            f"        bmc:\n"
            f"          address: redfish-virtualmedia://{bmc_ip_addr}:8000/redfish/v1/Systems/\n"
            f"          username: admin\n"
            f"          password: {bmc_password or 'password'}\n"
            f"          disableCertificateVerification: true\n"
            f"        bootMACAddress: {boot_mac}\n"
        )

    ic_lines = [
        "apiVersion: v1",
        f"baseDomain: {base_domain}",
        "metadata:",
        f"  name: {cluster_name}",
        "compute:",
        "  - name: worker",
        f"    replicas: {num_workers}",
        "controlPlane:",
        "  name: master",
        "  replicas: 3",
        "networking:",
        "  networkType: OVNKubernetes",
        "  clusterNetwork:",
        "    - cidr: 10.128.0.0/14",
        "      hostPrefix: 23",
        "  serviceNetwork:",
        "    - 172.30.0.0/16",
        "  machineNetwork:",
        "    - cidr: 10.0.0.0/24",
        "platform:",
        "  baremetal:",
        "    apiVIPs:",
        f"      - {api_vip}",
        "    ingressVIPs:",
        f"      - {ingress_vip}",
        "    provisioningNetwork: Disabled",
        "    externalBridge: br-cluster",
        "    bootstrapExternalStaticIP: 10.0.0.99",
        "    bootstrapExternalStaticGateway: 10.0.0.1",
        "    bootstrapExternalStaticDNS: 10.0.0.1",
        "    hosts:",
    ]
    ic_lines.append(bmc_hosts_yaml.rstrip())
    if pull_secret_json:
        ic_lines.append(f"pullSecret: '{pull_secret_json}'")
    if ssh_pub_key:
        ic_lines.append(f"sshKey: '{ssh_pub_key}'")

    return "\n".join(ic_lines)


def _build_install_script(ocp_version, auto_install):
    return (
        "  - |\n"
        "    cat > /home/cloud-user/install-ocp.sh << 'SCRIPTEOF'\n"
        "    #!/bin/bash\n"
        "    set -e\n"
        "    cd /home/cloud-user\n"
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
        "      echo \"Downloading openshift-install $OCP_VERSION...\"\n"
        "      curl -L -o /tmp/openshift-install.tar.gz https://mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp/stable-$OCP_VERSION/openshift-install-linux.tar.gz\n"
        "      tar xzf /tmp/openshift-install.tar.gz && rm -f /tmp/openshift-install.tar.gz\n"
        "      echo \"Downloading oc client...\"\n"
        "      curl -L -o /tmp/openshift-client.tar.gz https://mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp/stable-$OCP_VERSION/openshift-client-linux.tar.gz\n"
        "      tar xzf /tmp/openshift-client.tar.gz && rm -f /tmp/openshift-client.tar.gz\n"
        "      sudo mv oc kubectl /usr/local/bin/\n"
        "      echo \"Downloaded openshift-install and oc\"\n"
        "    fi\n"
        "    \n"
        "    # Write install-config if not already present\n"
        "    if [ ! -d ocp-install ]; then\n"
        "      mkdir -p ocp-install\n"
        "    fi\n"
        "    \n"
        "    echo ''\n"
        "    echo '================================================'\n"
        "    echo 'OCP Bare Metal IPI Installer Ready'\n"
        "    echo '================================================'\n"
        "    echo ''\n"
        "    echo 'install-config.yaml: ~/ocp-install/install-config.yaml'\n"
        "    echo 'Pull secret:         ~/pull-secret.json'\n"
        "    echo 'openshift-install:   ~/openshift-install'\n"
        "    echo ''\n"
        "    echo 'To install OCP:'\n"
        "    echo '  cd ~/ocp-install'\n"
        "    echo '  ~/openshift-install create cluster --dir . --log-level debug'\n"
        "    echo ''\n"
        "    echo 'To watch progress:'\n"
        "    echo '  tmux attach -t setup'\n"
        "    echo ''\n"
        + ("    # Auto-run OCP installer\n"
           "    echo 'Starting OCP installation...'\n"
           "    cd /home/cloud-user/ocp-install\n"
           "    cp install-config.yaml install-config.yaml.bak\n"
           "    /home/cloud-user/openshift-install create cluster --dir . --log-level debug 2>&1 | tee /home/cloud-user/install.log\n"
           if auto_install else "") +
        "    SCRIPTEOF\n"
        "    chown cloud-user:cloud-user /home/cloud-user/install-ocp.sh\n"
        "    chmod 755 /home/cloud-user/install-ocp.sh\n"
    )
