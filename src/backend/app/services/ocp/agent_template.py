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

_MAC_RE = re.compile(r'^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$')
_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$')


def _firefox_cfg_cmd(oauth_url: str) -> str:
    """Generate a shell command that writes firefox.cfg via base64 decode."""
    import base64
    cfg = (
        "// AutoConfig\n"
        "try {\n"
        "  var dominated = false;\n"
        "  try { Components.classes['@mozilla.org/login-manager;1'].getService(Components.interfaces.nsILoginManager)"
        ".getAllLogins({}).forEach(function(l) { if (l.hostname.indexOf('oauth-openshift') >= 0) dominated = true; }); } catch(e) {}\n"
        "  if (!dominated) {\n"
        "    var file = Components.classes['@mozilla.org/file/local;1'].createInstance(Components.interfaces.nsIFile);\n"
        "    file.initWithPath('/home/cloud-user/ocp-install/auth/kubeadmin-password');\n"
        "    if (file.exists()) {\n"
        "      var fis = Components.classes['@mozilla.org/network/file-input-stream;1'].createInstance(Components.interfaces.nsIFileInputStream);\n"
        "      fis.init(file, 1, 0, 0);\n"
        "      var sis = Components.classes['@mozilla.org/scriptableinputstream;1'].createInstance(Components.interfaces.nsIScriptableInputStream);\n"
        "      sis.init(fis);\n"
        "      var pw = sis.read(sis.available()).trim();\n"
        "      sis.close();\n"
        "      var loginInfo = Components.classes['@mozilla.org/login-manager/loginInfo;1'].createInstance(Components.interfaces.nsILoginInfo);\n"
        f"      loginInfo.init('{oauth_url}', '{oauth_url}/login', null, 'kubeadmin', pw, 'inputUsername', 'inputPassword');\n"
        "      Components.classes['@mozilla.org/login-manager;1'].getService(Components.interfaces.nsILoginManager).addLogin(loginInfo);\n"
        "    }\n"
        "  }\n"
        "} catch(e) {}\n"
    )
    b64 = base64.b64encode(cfg.encode()).decode()
    return f"      echo '{b64}' | base64 -d > $FIREFOX_DIR/firefox.cfg\n"


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
        node["data"]["ciPackages"] = [
            "git", "ansible-core", "python3-pip", "bind-utils", "nmstate",
            "@Server with GUI", "firefox", "gnome-shell-extension-dash-to-dock",
        ]
        if password:
            node["data"]["ciCloudUserPassword"] = password
        if ssh_key_ids:
            node["data"]["ciSshKeyIds"] = ssh_key_ids
        if ssh_keys:
            node["data"]["ciSshKeys"] = ssh_keys

        # DVD mount + yum repos (mounts: runs before packages:)
        if bastion_iso:
            node["data"]["ciUserData"] = (
                "mounts:\n"
                "  - [/dev/sr0, /mnt/rhel-dvd, iso9660, \"ro,nofail\", \"0\", \"0\"]\n"
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
                "  - nmcli con up \"cloud-init ens3\" 2>/dev/null || true\n"
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
            topology, cluster_name, base_domain, api_vip, ingress_vip,
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

        # Quiesce script — gracefully shuts down all OCP nodes for pattern save
        node["data"]["ciUserData"] += (
            "  - |\n"
            "    cat > /home/cloud-user/quiesce-ocp.sh << 'QEOF'\n"
            "    #!/bin/bash\n"
            "    export KUBECONFIG=/home/cloud-user/ocp-install/auth/kubeconfig\n"
            "    echo 'Ejecting virtual media and shutting down nodes...'\n"
            "    for node in $(oc get nodes -o name); do\n"
            "      echo \"  Shutting down $node...\"\n"
            "      oc debug $node -- chroot /host bash -c 'eject /dev/sr0 2>/dev/null; systemctl poweroff' 2>/dev/null &\n"
            "    done\n"
            "    wait\n"
            "    echo 'Waiting for nodes to shut down...'\n"
            "    for i in $(seq 1 60); do\n"
            "      READY=$(timeout 5 oc get nodes --no-headers 2>/dev/null | grep -c Ready || echo 0)\n"
            "      if [ \"$READY\" = \"0\" ]; then\n"
            "        echo 'All nodes are down.'\n"
            "        break\n"
            "      fi\n"
            "      echo \"  $READY node(s) still up...\"\n"
            "      sleep 5\n"
            "    done\n"
            "    echo 'Safe to stop project and save as pattern.'\n"
            "    QEOF\n"
            "    chown cloud-user:cloud-user /home/cloud-user/quiesce-ocp.sh\n"
            "    chmod 755 /home/cloud-user/quiesce-ocp.sh\n"
        )

        # Launch OCP installer in background
        node["data"]["ciUserData"] += (
            "  - sudo -u cloud-user nohup /home/cloud-user/install-ocp.sh > /home/cloud-user/install.log 2>&1 &\n"
        )

        # Desktop setup script — written as a file to avoid nested quoting issues
        node["data"]["ciUserData"] += (
            "  - |\n"
            "    cat > /root/setup-desktop.sh << 'DESKTOPEOF'\n"
            "    #!/bin/bash\n"
            "    set -x\n"
            "    dnf remove -y gnome-initial-setup gnome-software 2>/dev/null\n"
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
            "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/desktop/session/idle-delay \"uint32 0\"\n"
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
            "    sed -i '/^\\[daemon\\]/a AutomaticLoginEnable=True\\nAutomaticLogin=cloud-user' /etc/gdm/custom.conf\n"
            "    echo 'export KUBECONFIG=/home/cloud-user/ocp-install/auth/kubeconfig' >> /home/cloud-user/.bashrc\n"
            "    systemctl set-default graphical.target\n"
            "    systemctl isolate graphical.target\n"
            "    DESKTOPEOF\n"
            "    chmod 755 /root/setup-desktop.sh\n"
            "    nohup /root/setup-desktop.sh > /var/log/desktop-install.log 2>&1 &\n"
        )

        # Firefox enterprise policies + auto-login for OCP console
        console_url = f"https://console-openshift-console.apps.{cluster_name}.{base_domain}"
        oauth_url = f"https://oauth-openshift.apps.{cluster_name}.{base_domain}"
        node["data"]["ciUserData"] += (
            "  - |\n"
            "    mkdir -p /etc/firefox/policies\n"
            "    cat > /etc/firefox/policies/policies.json << 'FPEOF'\n"
            "    {\n"
            "      \"policies\": {\n"
            f"        \"Homepage\": {{\"URL\": \"{console_url}\", \"Locked\": false, \"StartPage\": \"homepage\"}},\n"
            "        \"OverrideFirstRunPage\": \"\",\n"
            "        \"OverridePostUpdatePage\": \"\",\n"
            "        \"UserMessaging\": {\"WhatsNew\": false, \"ExtensionRecommendations\": false, \"FeatureRecommendations\": false, \"UrlbarInterventions\": false, \"SkipOnboarding\": true, \"MoreFromMozilla\": false},\n"
            "        \"DisableTelemetry\": true,\n"
            "        \"Certificates\": {\"ImportEnterpriseRoots\": true},\n"
            "        \"NoDefaultBookmarks\": true,\n"
            "        \"DontCheckDefaultBrowser\": true,\n"
            "        \"DisableAppUpdate\": true\n"
            "      }\n"
            "    }\n"
            "    FPEOF\n"
        )
        # AutoConfig: inject kubeadmin saved login into Firefox on first launch
        node["data"]["ciUserData"] += (
            "  - |\n"
            "    FIREFOX_DIR=$(find /usr/lib64/firefox /usr/lib/firefox -maxdepth 0 2>/dev/null | head -1)\n"
            "    if [ -n \"$FIREFOX_DIR\" ]; then\n"
            "      echo 'pref(\"general.config.filename\", \"firefox.cfg\");' > $FIREFOX_DIR/defaults/pref/autoconfig.js\n"
            "      echo 'pref(\"general.config.obscure_value\", 0);' >> $FIREFOX_DIR/defaults/pref/autoconfig.js\n"
            "      echo 'pref(\"browser.sessionstore.resume_from_crash\", false);' >> $FIREFOX_DIR/defaults/pref/autoconfig.js\n"
            "      echo 'pref(\"browser.shell.checkDefaultBrowser\", false);' >> $FIREFOX_DIR/defaults/pref/autoconfig.js\n"
            "      echo 'pref(\"browser.startup.homepage_override.mstone\", \"ignore\");' >> $FIREFOX_DIR/defaults/pref/autoconfig.js\n"
            "      echo 'pref(\"browser.disableResetPrompt\", true);' >> $FIREFOX_DIR/defaults/pref/autoconfig.js\n"
            "      echo 'pref(\"browser.slowStartup.notificationDisabled\", true);' >> $FIREFOX_DIR/defaults/pref/autoconfig.js\n"
            "      echo 'pref(\"browser.laterrun.enabled\", false);' >> $FIREFOX_DIR/defaults/pref/autoconfig.js\n"
            + _firefox_cfg_cmd(oauth_url) +
            "    fi\n"
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
                          api_vip, ingress_vip, password, pull_secret_json, ssh_pub_key):
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
        "    hosts:",
    ]
    for node in topology.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        td = node.get("data", {})
        if not td.get("bmcEnabled") or not td.get("bmcIp"):
            continue
        vm_name = td.get("name", "")
        boot_mac = td.get("nics", [{}])[0].get("mac", "")
        if not _NAME_RE.match(vm_name) or not _MAC_RE.match(boot_mac):
            continue
        role = "master" if "cp-" in vm_name or "sno" in vm_name else "worker"
        ic_lines.extend([
            f"      - name: {vm_name}",
            f"        role: {role}",
            f"        bootMACAddress: {boot_mac}",
        ])
    if pull_secret_json:
        ic_lines.append(f"pullSecret: '{pull_secret_json}'")
    if ssh_pub_key:
        ic_lines.append(f"sshKey: '{ssh_pub_key}'")

    return "\n".join(ic_lines)


def _build_agent_config(topology, cluster_name, base_domain, api_vip="10.0.0.2", ingress_vip="10.0.0.3"):
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
        if not _NAME_RE.match(vm_name) or not _MAC_RE.match(boot_mac):
            continue
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
        "      sudo mv oc kubectl /usr/bin/\n"
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
        "    echo 'To gracefully shut down the cluster (for pattern save):'\n"
        "    echo '  ~/quiesce-ocp.sh'\n"
        "    echo '  # Then stop the project in Troshka and Save as Pattern'\n"
        "    echo ''\n"
        + ("    # Auto-run agent-based installer\n"
           "    INSTALL_START=$(date +%s)\n"
           "    echo \"Install started at $(date)\"\n"
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
           "    BASTION_IP=$(ip -4 addr show ens4 | grep -oP '(?<=inet\\s)\\d+(\\.\\d+){3}')\n"
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
           "    /home/cloud-user/openshift-install agent wait-for install-complete --dir /home/cloud-user/ocp-install --log-level debug 2>&1\n"
           "    INSTALL_END=$(date +%s)\n"
           "    ELAPSED=$(( INSTALL_END - INSTALL_START ))\n"
           "    echo ''\n"
           "    echo '================================================'\n"
           "    echo \"Install completed at $(date)\"\n"
           "    echo \"Total time: $(( ELAPSED / 60 )) min $(( ELAPSED % 60 )) sec\"\n"
           "    echo '================================================'\n"
           "    # Eject agent ISO via Redfish virtual media\n"
           "    echo 'Ejecting agent ISO from nodes...'\n"
           f"    for BMC_IP in {bmc_ips_str}; do\n"
           "      SYS_ID=$(curl -s -u admin:$BMC_PASS http://${BMC_IP}:8000/redfish/v1/Systems | python3 -c \"import json,sys; print(json.load(sys.stdin)['Members'][0]['@odata.id'].split('/')[-1])\" 2>/dev/null)\n"
           "      curl -s -u admin:$BMC_PASS -X POST \"http://${BMC_IP}:8000/redfish/v1/Systems/${SYS_ID}/VirtualMedia/Cd/Actions/VirtualMedia.EjectMedia\" -H 'Content-Type: application/json' -d '{}' 2>/dev/null\n"
           "    done\n"
           "    # Cleanup: remove cached ISO and temp files\n"
           "    rm -rf /home/cloud-user/.cache/agent/ /tmp/http-server.log /tmp/cookies /tmp/*.zip /var/tmp/dnf-*\n"
           "    dnf clean all 2>/dev/null\n"
           "    # Kill the HTTP server used to serve the agent ISO\n"
           "    kill $HTTP_PID 2>/dev/null\n"
           if auto_install else "") +
        "    SCRIPTEOF\n"
        "    chown cloud-user:cloud-user /home/cloud-user/install-ocp.sh\n"
        "    chmod 755 /home/cloud-user/install-ocp.sh\n"
    )
