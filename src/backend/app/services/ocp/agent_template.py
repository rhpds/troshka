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
import uuid


def customize_topology(topology: dict, template_id: str, config: dict) -> dict:
    """Apply OCP Agent-Based configuration to a base topology."""
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
    """Reuse from IPI template."""
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
    """Reuse from IPI template."""
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

        # DVD mount + yum repos + packages (no libvirt/qemu needed for agent-based)
        if bastion_iso:
            node["data"]["ciUserData"] = (
                "runcmd:\n"
                "  - nmcli con up \"cloud-init ens3\" 2>/dev/null || true\n"
                "  - mkdir -p /mnt/rhel-dvd\n"
                "  - |\n"
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
                "  - dnf install -y git ansible-core python3-pip bind-utils tmux nmstate\n"
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

        # Build install-config.yaml and agent-config.yaml
        install_config = _build_install_config(
            topology, template_id, cluster_name, base_domain,
            api_vip, ingress_vip, password, pull_secret_json, ssh_pub_key,
        )
        agent_config = _build_agent_config(
            topology, cluster_name, base_domain,
        )

        # Install script
        # Collect BMC IPs for the install script (validated)
        bmc_ips = []
        for tnode in topology.get("nodes", []):
            td = tnode.get("data", {})
            if tnode.get("type") == "vmNode" and td.get("bmcEnabled") and td.get("bmcIp"):
                ip = str(ipaddress.IPv4Address(td["bmcIp"]))
                bmc_ips.append(ip)
        bmc_ips_str = " ".join(bmc_ips)

        node["data"]["ciUserData"] += _build_install_script(ocp_version, auto_install_ocp, password, bmc_ips_str)

        # Write install-config.yaml
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

        # Write agent-config.yaml
        if agent_config:
            indented_ac = "\n".join("    " + line for line in agent_config.split("\n"))
            node["data"]["ciUserData"] += (
                "  - |\n"
                "    cat > /home/cloud-user/ocp-install/agent-config.yaml << 'ACEOF'\n"
                f"{indented_ac}\n"
                "    ACEOF\n"
                "    chown -R cloud-user:cloud-user /home/cloud-user/ocp-install\n"
            )

        # Launch installer in tmux
        node["data"]["ciUserData"] += (
            "  - su - cloud-user -c 'setsid tmux new-session -d -s setup /home/cloud-user/install-ocp.sh'\n"
        )

        # Static IP on BMC NIC
        bmc_ip = str(ipaddress.IPv4Address(bastion_bmc_ip))
        node["data"]["ciNetworkConfig"] = (
            "version: 2\n"
            "ethernets:\n"
            "  ens3:\n"
            "    dhcp4: true\n"
            "  ens4:\n"
            "    addresses:\n"
            f"      - {bmc_ip}/24\n"
        )
        break


def _build_install_config(topology, template_id, cluster_name, base_domain,
                          api_vip, ingress_vip, bmc_password, pull_secret_json, ssh_pub_key):
    num_workers = 0 if template_id in ("ocp-compact", "ocp-sno") else 2
    num_masters = 1 if template_id == "ocp-sno" else 3

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
        "    - cidr: 10.0.0.0/24",
        "platform:",
        "  baremetal:",
        "    apiVIPs:",
        f"      - {api_vip}",
        "    ingressVIPs:",
        f"      - {ingress_vip}",
    ]
    if pull_secret_json:
        ic_lines.append(f"pullSecret: '{pull_secret_json}'")
    if ssh_pub_key:
        ic_lines.append(f"sshKey: '{ssh_pub_key}'")

    return "\n".join(ic_lines)


def _build_agent_config(topology, cluster_name, base_domain):
    """Build agent-config.yaml with BMC host details for Redfish virtual media boot."""
    hosts_yaml = ""
    for node in topology.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        td = node.get("data", {})
        if not td.get("bmcEnabled") or not td.get("bmcIp"):
            continue
        vm_name = td.get("name", "")
        bmc_ip = td["bmcIp"]
        cluster_ip = td.get("nics", [{}])[0].get("ip", "")
        boot_mac = td.get("nics", [{}])[0].get("mac", "")
        role = "master" if "cp-" in vm_name or "sno" in vm_name else "worker"

        hosts_yaml += (
            f"    - hostname: {vm_name}\n"
            f"      role: {role}\n"
            f"      interfaces:\n"
            f"        - name: ens3\n"
            f"          macAddress: {boot_mac}\n"
            f"      networkConfig:\n"
            f"        interfaces:\n"
            f"          - name: ens3\n"
            f"            type: ethernet\n"
            f"            state: up\n"
            f"            ipv4:\n"
            f"              enabled: true\n"
            f"              address:\n"
            f"                - ip: {cluster_ip}\n"
            f"                  prefix-length: 24\n"
            f"              dhcp: false\n"
            f"        dns-resolver:\n"
            f"          config:\n"
            f"            server:\n"
            f"              - 10.0.0.1\n"
            f"        routes:\n"
            f"          config:\n"
            f"            - destination: 0.0.0.0/0\n"
            f"              next-hop-address: 10.0.0.1\n"
            f"              next-hop-interface: ens3\n"
        )

    ac_lines = [
        "apiVersion: v1beta1",
        "kind: AgentConfig",
        "metadata:",
        f"  name: {cluster_name}",
        "rendezvousIP: 10.0.0.10",
        "additionalNTPSources:",
        "  - clock.redhat.com",
        "  - pool.ntp.org",
        "hosts:",
    ]
    ac_lines.append(hosts_yaml.rstrip())

    return "\n".join(ac_lines)


def _build_install_script(ocp_version, auto_install, bmc_password="", bmc_ips_str=""):
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
        + ("    # Auto-run agent-based installer\n"
           f"    BMC_PASS='{bmc_password}'\n"
           "    echo 'Creating agent ISO...'\n"
           "    cd /home/cloud-user/ocp-install\n"
           "    cp install-config.yaml install-config.yaml.bak\n"
           "    cp agent-config.yaml agent-config.yaml.bak\n"
           "    /home/cloud-user/openshift-install agent create image --dir . --log-level debug 2>&1 | tee /home/cloud-user/create-image.log\n"
           "    \n"
           "    echo 'Agent ISO created. Serving via HTTP and booting nodes...'\n"
           "    # Serve the ISO on port 8080\n"
           "    cd /home/cloud-user/ocp-install\n"
           "    nohup python3 -m http.server 8080 > /tmp/http-server.log 2>&1 &\n"
           "    HTTP_PID=$!\n"
           "    echo \"HTTP server PID: $HTTP_PID\"\n"
           "    \n"
           "    # Boot each CP node via Redfish virtual media\n"
           "    BASTION_IP=$(ip -4 addr show ens3 | grep -oP '(?<=inet\\s)\\d+(\\.\\d+){3}')\n"
           "    ISO_URL=\"http://${BASTION_IP}:8080/agent.x86_64.iso\"\n"
           "    echo \"ISO URL: $ISO_URL\"\n"
           "    \n"
           f"    for BMC_IP in {bmc_ips_str}; do\n"
           "      echo \"Mounting ISO on BMC $BMC_IP...\"\n"
           "      # Get system UUID from sushy\n"
           "      SYS_ID=$(curl -s -u admin:$BMC_PASS http://${BMC_IP}:8000/redfish/v1/Systems | python3 -c \"import json,sys; print(json.load(sys.stdin)['Members'][0]['@odata.id'].split('/')[-1])\")\n"
           "      echo \"  System: $SYS_ID\"\n"
           "      # Insert virtual media (Systems path, HTTP, with auth)\n"
           "      curl -s -u admin:$BMC_PASS -X POST \"http://${BMC_IP}:8000/redfish/v1/Systems/${SYS_ID}/VirtualMedia/Cd/Actions/VirtualMedia.InsertMedia\" \\\n"
           "        -H 'Content-Type: application/json' \\\n"
           "        -d \"{\\\"Image\\\": \\\"${ISO_URL}\\\", \\\"Inserted\\\": true, \\\"WriteProtected\\\": true}\" || true\n"
           "      # Reboot — UEFI boot order is hd,cdrom so empty disk falls through to ISO\n"
           "      # After agent writes CoreOS to disk, next reboot boots from disk first\n"
           "      curl -s -u admin:$BMC_PASS -X POST \"http://${BMC_IP}:8000/redfish/v1/Systems/${SYS_ID}/Actions/ComputerSystem.Reset\" \\\n"
           "        -H 'Content-Type: application/json' \\\n"
           "        -d '{\"ResetType\": \"ForceRestart\"}' || true\n"
           "      echo \"Booted $BMC_IP from ISO\"\n"
           "    done\n"
           "    \n"
           "    \n"
           "    echo 'Waiting for cluster installation to complete...'\n"
           "    /home/cloud-user/openshift-install agent wait-for install-complete --dir /home/cloud-user/ocp-install --log-level debug 2>&1 | tee /home/cloud-user/install.log\n"
           if auto_install else "") +
        "    SCRIPTEOF\n"
        "    chown cloud-user:cloud-user /home/cloud-user/install-ocp.sh\n"
        "    chmod 755 /home/cloud-user/install-ocp.sh\n"
    )
