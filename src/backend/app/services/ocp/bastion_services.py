"""Generate cloud-init blocks for bastion services from template YAML config.

Reads ``disconnected`` and ``bastion_services`` sections from the resolved
template and produces runcmd blocks. All values come from the YAML — nothing
is hardcoded to a specific catalog item.
"""


def generate_cloud_init(resolved: dict, bastion_password: str) -> str:
    """Return cloud-init runcmd blocks for all bastion services."""
    lines = []
    disconnected = resolved.get("disconnected", {})
    services = resolved.get("bastion_services", {})

    if disconnected.get("registry"):
        lines.append(_registry_block(disconnected["registry"], bastion_password))

    if disconnected.get("mirror"):
        lines.append(
            _mirror_block(
                disconnected["mirror"],
                disconnected["registry"],
                bastion_password,
            )
        )

    if services.get("gitea"):
        lines.append(_gitea_block(services["gitea"], bastion_password))

    if services.get("minio"):
        lines.append(_minio_block(services["minio"], bastion_password))

    return "\n".join(lines)


def _registry_block(cfg: dict, password: str) -> str:
    hostname = cfg.get("hostname", "infra.lab.local")
    port = cfg.get("port", 8443)
    user = cfg.get("user", "admin")
    image = cfg.get("image", "docker.io/library/registry:2")
    cert_cn = cfg.get("cert_cn", hostname)

    return (
        "  - |\n"
        "    # Container registry setup\n"
        "    mkdir -p /opt/registry/{auth,certs,data,conf}\n"
        f"    if [ ! -f /opt/registry/certs/registry-cert.pem ]; then\n"
        f"      openssl req -newkey rsa:4096 -nodes -sha256 "
        f"-keyout /opt/registry/certs/registry-key.pem "
        f"-x509 -days 365 -out /opt/registry/certs/registry-cert.pem "
        f"-subj '/CN={cert_cn}' -addext 'subjectAltName=DNS:{cert_cn}'\n"
        f"      cp /opt/registry/certs/registry-cert.pem /etc/pki/ca-trust/source/anchors/\n"
        f"      update-ca-trust\n"
        f"    fi\n"
        f"    dnf install -y httpd-tools 2>/dev/null\n"
        f"    htpasswd -bBc /opt/registry/auth/htpasswd {user} '{password}'\n"
        f"    cat > /opt/registry/conf/config.yml << 'REGEOF'\n"
        f"    version: 0.1\n"
        f"    log:\n"
        f"      fields:\n"
        f"        service: registry\n"
        f"    storage:\n"
        f"      cache:\n"
        f"        blobdescriptor: inmemory\n"
        f"      filesystem:\n"
        f"        rootdirectory: /var/lib/registry\n"
        f"    http:\n"
        f"      addr: :{port}\n"
        f"      headers:\n"
        f"        X-Content-Type-Options: [nosniff]\n"
        f"    health:\n"
        f"      storagedriver:\n"
        f"        enabled: true\n"
        f"    compatibility:\n"
        f"      schema1:\n"
        f"        enabled: true\n"
        f"    REGEOF\n"
        f"    cat > /etc/systemd/system/podman-registry.service << 'SVCEOF'\n"
        f"    [Unit]\n"
        f"    Description=Container Registry\n"
        f"    After=network.target\n"
        f"    [Service]\n"
        f"    Type=simple\n"
        f"    TimeoutStartSec=300\n"
        f"    ExecStartPre=-/usr/bin/podman rm -f registry\n"
        f"    ExecStart=/usr/bin/podman run --name registry --net host --security-opt label=disable "
        f"-e REGISTRY_AUTH=htpasswd -e REGISTRY_AUTH_HTPASSWD_REALM=Registry "
        f"-e REGISTRY_HTTP_SECRET=redhat "
        f"-e REGISTRY_HTTP_TLS_CERTIFICATE=/certs/registry-cert.pem "
        f"-e REGISTRY_HTTP_TLS_KEY=/certs/registry-key.pem "
        f"-e REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY=/registry "
        f"-v /opt/registry/auth:/auth:z -v /opt/registry/certs:/certs:z "
        f"-v /opt/registry/data:/registry:z "
        f"-v /opt/registry/conf/config.yml:/etc/docker/registry/config.yml:z "
        f"{image}\n"
        f"    ExecStop=-/usr/bin/podman rm -f registry\n"
        f"    Restart=always\n"
        f"    RestartSec=30s\n"
        f"    [Install]\n"
        f"    WantedBy=multi-user.target\n"
        f"    SVCEOF\n"
        f"    systemctl daemon-reload\n"
        f"    systemctl enable --now podman-registry\n"
    )


def _mirror_block(mirror_cfg: dict, registry_cfg: dict, password: str) -> str:
    hostname = registry_cfg.get("hostname", "infra.lab.local")
    port = registry_cfg.get("port", 8443)
    user = registry_cfg.get("user", "admin")
    registry_url = f"{hostname}:{port}"
    catalog = mirror_cfg.get("catalog", "")
    packages = mirror_cfg.get("packages", [])
    additional = mirror_cfg.get("additional_images", [])
    catalog_alias = mirror_cfg.get("catalog_alias", "")

    # Build mirror-config.yaml
    pkg_lines = ""
    for pkg in packages:
        pkg_lines += f"    - name: {pkg['name']}\n"
        channels = pkg.get("channels", [])
        if channels:
            pkg_lines += "      channels:\n"
            for ch in channels:
                pkg_lines += f"      - name: {ch}\n"

    additional_lines = ""
    for img in additional:
        additional_lines += f"  - name: {img}\n"

    return (
        "  - |\n"
        "    # Mirror operators to local registry\n"
        f"    # Merge registry creds into pull secret\n"
        f'    python3 -c "\n'
        f"    import json, base64\n"
        f"    ps = json.load(open('/home/cloud-user/pull-secret.json'))\n"
        f"    key = base64.b64encode(b'{user}:{password}').decode()\n"
        f"    ps['auths']['{registry_url}'] = {{'auth': key}}\n"
        f"    json.dump(ps, open('/home/cloud-user/pull-secret.json', 'w'))\n"
        f"    import shutil; shutil.copy('/home/cloud-user/pull-secret.json', '/root/.docker/config.json')\n"
        f'    "\n'
        f"    mkdir -p /root/.docker\n"
        f"    # Download oc-mirror\n"
        f"    if [ ! -f /usr/bin/oc-mirror ]; then\n"
        f"      OCP_VER=$(echo '{catalog}' | grep -oP 'v\\K[0-9]+\\.[0-9]+' || echo '4.20')\n"
        f'      curl -sL "https://mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp/stable-${{OCP_VER}}/oc-mirror.tar.gz" | tar xz -C /usr/bin/\n'
        f"      chmod +x /usr/bin/oc-mirror\n"
        f"    fi\n"
        f"    cat > /root/mirror-config.yaml << 'MIRROREOF'\n"
        f"    apiVersion: mirror.openshift.io/v2alpha1\n"
        f"    kind: ImageSetConfiguration\n"
        f"    mirror:\n"
        f"      additionalImages:\n"
        f"{additional_lines}"
        f"      operators:\n"
        f"      - catalog: {catalog}\n"
        f"        packages:\n"
        f"{pkg_lines}"
        f"    MIRROREOF\n"
        f"    cd /root && oc-mirror --v2 --workspace file:// "
        f"--config=mirror-config.yaml docker://{registry_url} 2>&1 | tee /root/mirror.log\n"
        f"    # Apply mirror manifests\n"
        f"    if [ -d /root/working-dir/cluster-resources ]; then\n"
        f"      export KUBECONFIG=/home/cloud-user/ocp-install/auth/kubeconfig\n"
        f"      oc apply -f /root/working-dir/cluster-resources/ 2>/dev/null || true\n"
        f"    fi\n"
        + (
            f"    # Create catalog source alias for compatibility\n"
            f"    cat << EOF | oc apply -f -\n"
            f"    apiVersion: operators.coreos.com/v1alpha1\n"
            f"    kind: CatalogSource\n"
            f"    metadata:\n"
            f"      name: {catalog_alias}\n"
            f"      namespace: openshift-marketplace\n"
            f"    spec:\n"
            f"      displayName: Red Hat Operators\n"
            f"      image: {registry_url}/redhat/redhat-operator-index:$(echo '{catalog}' | grep -oP ':.*' || echo ':v4.20')\n"
            f"      publisher: Red Hat\n"
            f"      sourceType: grpc\n"
            f"      updateStrategy:\n"
            f"        registryPoll:\n"
            f"          interval: 1h\n"
            f"    EOF\n"
            if catalog_alias
            else ""
        )
    )


def _gitea_block(cfg: dict, password: str) -> str:
    image = cfg.get("image", "docker.io/gitea/gitea:1.21")
    port = cfg.get("port", 3000)
    repos = cfg.get("repos", [])

    repo_cmds = ""
    for repo in repos:
        url = repo.get("url", "")
        name = repo.get("name", "")
        repo_cmds += (
            f"    curl -s -u student:{password} -X POST http://localhost:{port}/api/v1/repos/migrate "
            f"-H 'Content-Type: application/json' "
            f'-d \'{{"service":"2","clone_addr":"{url}","uid":1,"repo_name":"{name}"}}\' '
            f">/dev/null 2>&1 || true\n"
        )

    return (
        "  - |\n"
        "    # Gitea git server\n"
        f"    mkdir -p /opt/gitea && chown 1000:1000 /opt/gitea\n"
        f"    rm -rf /opt/gitea/ssh 2>/dev/null\n"
        f"    cat > /etc/systemd/system/podman-gitea.service << 'SVCEOF'\n"
        f"    [Unit]\n"
        f"    Description=Gitea Server\n"
        f"    After=network.target\n"
        f"    [Service]\n"
        f"    Type=simple\n"
        f"    TimeoutStartSec=300\n"
        f"    ExecStartPre=-/usr/bin/podman rm -f gitea\n"
        f"    ExecStart=podman run --name gitea --net host --security-opt label=disable "
        f"-e USER_UID=1000 -e USER_GID=1000 "
        f"-e GITEA__server__HTTP_PORT={port} "
        f"-e GITEA__service__DISABLE_REGISTRATION=true "
        f"-e GITEA__security__INSTALL_LOCK=true "
        f"-v /opt/gitea/:/data {image}\n"
        f"    ExecStop=-/usr/bin/podman rm -f gitea\n"
        f"    Restart=always\n"
        f"    RestartSec=30s\n"
        f"    [Install]\n"
        f"    WantedBy=multi-user.target\n"
        f"    SVCEOF\n"
        f"    systemctl daemon-reload\n"
        f"    systemctl enable --now podman-gitea\n"
        f"    for i in $(seq 1 30); do curl -s http://localhost:{port}/api/v1/version >/dev/null 2>&1 && break; sleep 2; done\n"
        f"    podman exec --user 1000 gitea /bin/sh -c "
        f"'gitea admin user create --username student --password {password} "
        f"--email student@lab.local --must-change-password=false --admin' 2>/dev/null || true\n"
        f"    sleep 3\n"
        f"{repo_cmds}"
    )


def _minio_block(cfg: dict, password: str) -> str:
    image = cfg.get("image", "quay.io/minio/minio:latest")
    port = cfg.get("port", 9000)
    console_port = cfg.get("console_port", 9001)
    user = cfg.get("user", "admin")
    buckets = cfg.get("buckets", [])

    bucket_cmds = ""
    for b in buckets:
        bucket_cmds += f"    mc mb minio/{b} 2>/dev/null || true\n"

    return (
        "  - |\n"
        "    # MinIO S3 storage\n"
        f"    mkdir -p /opt/minio/s3-volume\n"
        f"    cat > /etc/systemd/system/podman-minio.service << SVCEOF\n"
        f"    [Unit]\n"
        f"    Description=MinIO S3 Storage\n"
        f"    After=network.target\n"
        f"    [Service]\n"
        f"    Type=simple\n"
        f"    TimeoutStartSec=300\n"
        f"    ExecStartPre=-/usr/bin/podman rm -f minio-server\n"
        f"    ExecStart=/usr/bin/podman run --name minio-server --net host --security-opt label=disable "
        f"-v /opt/minio/s3-volume:/data:rw,z "
        f"-e MINIO_ROOT_USER={user} -e MINIO_ROOT_PASSWORD={password} "
        f"{image} server /data --console-address :{console_port}\n"
        f"    ExecStop=-/usr/bin/podman rm -f minio-server\n"
        f"    Restart=always\n"
        f"    RestartSec=30s\n"
        f"    [Install]\n"
        f"    WantedBy=multi-user.target\n"
        f"    SVCEOF\n"
        f"    systemctl daemon-reload\n"
        f"    systemctl enable --now podman-minio\n"
        f"    sleep 5\n"
        f"    if [ ! -f /usr/bin/mc ]; then\n"
        f"      curl -sL https://dl.min.io/client/mc/release/linux-amd64/mc -o /usr/bin/mc\n"
        f"      chmod 755 /usr/bin/mc\n"
        f"    fi\n"
        f"    mc alias set minio http://localhost:{port} {user} {password} 2>/dev/null || true\n"
        f"{bucket_cmds}"
    )
