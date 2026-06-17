"""Cloud-init generator for the 5G RAN lab bastion.

Produces runcmd blocks that set up:
- Container registry (port 8443)
- Gitea git server (port 3000)
- MinIO S3 storage (port 9002)
- dnsmasq DNS/DHCP
- Showroom lab guide UI (port 80/443 via Traefik)
"""

LAB_REPO = "https://github.com/RHsyseng/5g-ran-deployments-on-ocp-lab.git"
LAB_VERSION = "lab-4.20"
REGISTRY_HOST = "infra.5g-deployment.lab:8443"


def generate_bastion_cloud_init(
    bastion_password: str,
    student_name: str = "lab-user",
    lab_version: str = LAB_VERSION,
    bastion_hostname: str = "",
) -> str:
    """Return cloud-init runcmd blocks for RAN lab bastion services."""
    lines = []
    lines.append(_registry_block())
    lines.append(_gitea_block(lab_version))
    lines.append(_minio_block())
    lines.append(_dnsmasq_block())
    lines.append(
        _showroom_block(student_name, bastion_password, lab_version, bastion_hostname)
    )
    return "\n".join(lines)


def _registry_block():
    return (
        "  - |\n"
        "    # Container registry setup\n"
        "    mkdir -p /opt/registry/{auth,certs,data,conf}\n"
        "    # Generate self-signed cert\n"
        "    openssl req -newkey rsa:4096 -nodes -sha256 -keyout /opt/registry/certs/registry-key.pem "
        "-x509 -days 365 -out /opt/registry/certs/registry-cert.pem "
        "-subj '/CN=infra.5g-deployment.lab' -addext 'subjectAltName=DNS:infra.5g-deployment.lab'\n"
        "    cp /opt/registry/certs/registry-cert.pem /etc/pki/ca-trust/source/anchors/\n"
        "    update-ca-trust\n"
        "    dnf install -y httpd-tools\n"
        "    htpasswd -bBc /opt/registry/auth/htpasswd admin 'r3dh4t1!'\n"
        "    cat > /opt/registry/conf/config.yml << 'REGEOF'\n"
        "    version: 0.1\n"
        "    log:\n"
        "      fields:\n"
        "        service: registry\n"
        "    storage:\n"
        "      filesystem:\n"
        "        rootdirectory: /var/lib/registry\n"
        "    http:\n"
        "      addr: :8443\n"
        "      tls:\n"
        "        certificate: /certs/registry-cert.pem\n"
        "        key: /certs/registry-key.pem\n"
        "    auth:\n"
        "      htpasswd:\n"
        "        realm: Registry\n"
        "        path: /auth/htpasswd\n"
        "    REGEOF\n"
        "    podman run -d --name registry --restart=always "
        "-p 8443:8443 "
        "-v /opt/registry/data:/var/lib/registry:z "
        "-v /opt/registry/auth:/auth:z "
        "-v /opt/registry/certs:/certs:z "
        "-v /opt/registry/conf/config.yml:/etc/docker/registry/config.yml:z "
        "docker.io/library/registry:2\n"
    )


def _gitea_block(lab_version):
    return (
        "  - |\n"
        "    # Gitea git server setup\n"
        "    mkdir -p /opt/gitea\n"
        "    chown 1000:1000 /opt/gitea\n"
        "    podman run -d --name gitea --restart=always "
        "-p 3000:3000 -p 2222:22 "
        "-v /opt/gitea:/data:z "
        "docker.io/gitea/gitea:1.21\n"
        "    sleep 10\n"
        "    # Create admin user and mirror lab repo\n"
        "    podman exec --user 1000 gitea /bin/sh -c "
        "'gitea admin user create --username student --password student "
        "--email student@5g-deployment.lab --must-change-password=false --admin' || true\n"
        "    sleep 5\n"
        "    curl -s -u student:student -X POST http://localhost:3000/api/v1/repos/migrate "
        "-H 'Content-Type: application/json' "
        '-d \'{"service":"2","clone_addr":"https://github.com/RHsyseng/5g-ran-deployments-on-ocp-lab.git","uid":1,"repo_name":"5g-ran-deployments-on-ocp-lab"}\' || true\n'
    )


def _minio_block():
    return (
        "  - |\n"
        "    # MinIO S3 storage setup\n"
        "    mkdir -p /opt/minio/s3-volume\n"
        "    podman run -d --name minio --restart=always "
        "-p 9002:9000 -p 9001:9001 "
        "-v /opt/minio/s3-volume:/data:z "
        "-e MINIO_ROOT_USER=admin "
        "-e MINIO_ROOT_PASSWORD=admin1234 "
        "quay.io/minio/minio:latest server /data --console-address ':9001'\n"
        "    sleep 5\n"
        "    # Install mc client and create buckets\n"
        "    curl -sL https://dl.min.io/client/mc/release/linux-amd64/mc -o /usr/bin/mc\n"
        "    chmod 755 /usr/bin/mc\n"
        "    mc alias set minio http://localhost:9002 admin admin1234 || true\n"
        "    mc mb minio/sno-abi minio/sno-ibi minio/logs minio/multiclusterobservability 2>/dev/null || true\n"
    )


def _dnsmasq_block():
    return (
        "  - |\n"
        "    # dnsmasq DNS setup for lab domain\n"
        "    dnf install -y dnsmasq\n"
        "    mkdir -p /opt/dnsmasq/include.d\n"
        "    BASTION_IP=$(ip -4 addr show eth0 | grep -oP '(?<=inet )\\S+' | cut -d/ -f1)\n"
        "    cat > /opt/dnsmasq/dnsmasq.conf << 'DNSEOF'\n"
        "    strict-order\n"
        "    bind-dynamic\n"
        "    bogus-priv\n"
        "    dhcp-authoritative\n"
        "    conf-dir=/opt/dnsmasq/include.d\n"
        "    DNSEOF\n"
        "    # Hub cluster DNS entries\n"
        "    cat > /opt/dnsmasq/include.d/hub.ipv4 << HUBEOF\n"
        "    address=/api.hub.5g-deployment.lab/192.168.125.10\n"
        "    address=/api-int.hub.5g-deployment.lab/192.168.125.10\n"
        "    address=/.apps.hub.5g-deployment.lab/192.168.125.11\n"
        "    address=/infra.5g-deployment.lab/${BASTION_IP}\n"
        "    HUBEOF\n"
        "    # Start dnsmasq\n"
        "    dnsmasq -C /opt/dnsmasq/dnsmasq.conf --no-daemon &\n"
    )


def _showroom_block(student_name, password, lab_version, bastion_hostname):
    """Modular Showroom setup — reusable for future lab templates."""
    return (
        "  - |\n"
        "    # Showroom lab guide UI setup\n"
        "    mkdir -p /opt/showroom/lab-content\n"
        f"    git clone --single-branch -b {lab_version} "
        f"https://github.com/RHsyseng/5g-ran-deployments-on-ocp-lab.git "
        "/opt/showroom/lab-repo\n"
        "    # Build lab docs with Antora\n"
        "    podman run --rm -v /opt/showroom/lab-repo:/antora:z "
        "quay.io/rhsysdeseng/showroom:antora-v3.0.0 site.yml\n"
        "    cp -r /opt/showroom/lab-repo/gh-pages/* /opt/showroom/lab-content/\n"
        "    # Serve lab content via Apache\n"
        "    podman run -d --name showroom-apache --restart=always "
        "-p 8888:8080 "
        "-v /opt/showroom/lab-content:/var/www/html:z "
        "quay.io/fedora/httpd-24-micro:2.4\n"
        "    # Wetty web terminal\n"
        "    podman run -d --name showroom-wetty --restart=always "
        "--network host "
        f"-e SSHHOST=127.0.0.1 -e SSHPORT=22 -e SSHUSER={student_name} "
        f"-e SSHPASS={password} "
        "-e BASE=/terminal "
        "quay.io/rhsysdeseng/showroom:wetty\n"
    )
