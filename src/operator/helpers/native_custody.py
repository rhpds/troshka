"""Cleanup-only compatibility for explicitly attested legacy namespace objects.

No Kubernetes ownership is rewritten. Identity records contain no Secret values.
The normal managed admission profile never opts in to this compatibility path.
"""

from copy import deepcopy


def identity(value):
    from helpers.managed_power import digest

    meta = value["metadata"]
    body = {
        k: deepcopy(v)
        for k, v in value.items()
        if k not in {"metadata", "status", "apiVersion", "kind"}
    }
    if meta.get("generateName"):
        body["generateName"] = meta["generateName"]
    body["labels"] = meta.get("labels", {})
    authority_annotations = {
        "openshift.io/internal-registry-pull-secret-ref",
        "openshift.io/internal-registry-auth-token.service-account",
        "openshift.io/internal-registry-auth-token.binding",
        "haproxy.router.openshift.io/timeout",
    }
    body["authorityAnnotations"] = {k: v for k, v in meta.get("annotations", {}).items() if k in authority_annotations}
    if value["kind"] == "Secret":
        body.pop("data", None)
        body.pop("stringData", None)
        body["dataKeys"] = sorted(value.get("data", {}))
    if value["kind"] == "Pod":
        body["labels"] = meta.get("labels", {})
        body["networks"] = meta.get("annotations", {}).get("k8s.v1.cni.cncf.io/networks")
    if value["kind"] == "Deployment":
        body["spec"].pop("replicas", None)
    if value["kind"] == "VirtualMachine":
        body["spec"].pop("running", None)
        body["spec"].pop("runStrategy", None)
    return {
        "kind": value["kind"],
        "name": meta["name"],
        "uid": meta["uid"],
        "namespace": meta["namespace"],
        "digest": digest(body),
        "owners": deepcopy(meta.get("ownerReferences", [])),
    }


def custody_identity(value):
    """Create a fresh receipt independent of Deployment rollout restarts.

    Keep identity() unchanged for sealed cleanup and concurrency checks. Only
    the restart timestamp and its empty annotation map are normalized here.
    """
    candidate = deepcopy(value)
    if candidate.get("kind") == "Deployment":
        metadata = candidate.get("spec", {}).get("template", {}).get("metadata", {})
        annotations = metadata.get("annotations")
        if isinstance(annotations, dict):
            annotations.pop("kubectl.kubernetes.io/restartedAt", None)
            if not annotations:
                metadata.pop("annotations")
    return identity(candidate)


def identity_matches(value, pin):
    """Match legacy receipts across a restart without weakening sealed identity.

    Stored receipts remain unchanged. Fresh normalized receipts and legacy
    receipts without a timestamp may match across restarts. A legacy receipt
    containing a timestamp can only match exactly: its original timestamp
    cannot be recovered from a hash.
    """
    if identity(value) == pin or custody_identity(value) == pin:
        return True
    if value.get("kind") != "Deployment":
        return False
    candidate = deepcopy(value)
    metadata = candidate.get("spec", {}).get("template", {}).get("metadata", {})
    annotations = metadata.get("annotations", {})
    restart = "kubectl.kubernetes.io/restartedAt"
    if restart not in annotations:
        return False
    annotations.pop(restart)
    # Legacy receipts may have pinned an explicitly empty annotations map.
    return identity(candidate) == pin


def stable(value):
    bare = value.get("kind") == "Pod" and any(r.get("kind") == "TroshkaProject" for r in value.get("metadata", {}).get("ownerReferences", []))
    return bare or writer_candidate(value) or value["kind"] not in {
        "TroshkaProject",
        "Pod",
        "ReplicaSet",
        "VirtualMachineInstance",
        "Job",
    }


def writer_candidate(value):
    return value.get("kind") == "Pod" and value.get("metadata", {}).get("labels", {}).get("app") == "troshka-ops-pod"


def writer_shape(value, root, by_uid):
    """The existing backend scaffold; all dynamic inputs are durably pinned."""
    if not writer_candidate(value) or value["metadata"].get("ownerReferences"):
        return False
    meta, spec = value["metadata"], value.get("spec", {})
    pid = root.get("spec", {}).get("projectId", "")
    runner = meta["name"] == "workload-runner"
    if not pid or meta["name"] not in {"workload-runner", f"troshka-{pid[:8]}-ops"}:
        return False
    if meta.get("labels") != {"app": "troshka-ops-pod", "troshka-project": pid[:8]}:
        return False
    if spec.get("serviceAccountName") != "troshka-network" or spec.get("restartPolicy") != ("Never" if runner else "Always"):
        return False
    if any(spec.get(k) for k in ("initContainers", "ephemeralContainers", "hostNetwork", "hostPID", "hostIPC")):
        return False
    containers = spec.get("containers", [])
    if len(containers) != 1 or not writer_container(containers[0], runner, pid):
        return False
    return writer_mounts(value, by_uid, root) and writer_networks(value, root)


def writer_container(container, runner, pid):
    from helpers.managed_power import manifest_matches

    expected = {"name": "ops", "imagePullPolicy": "Always", "securityContext": {"privileged": True, "capabilities": {"add": ["NET_ADMIN", "NET_RAW"]}}, "resources": {"requests": {"cpu": "500m", "memory": "2Gi"}, "limits": {"cpu": "2", "memory": "4Gi"}}}
    if not all(manifest_matches(v, container.get(k)) for k, v in expected.items()):
        return False
    if any(container.get(k) for k in ("args", "envFrom", "volumeDevices", "lifecycle", "ports")):
        return False
    command = container.get("command", [])
    if len(command) != 3 or command[:2] not in (["bash", "-lc"], ["bash", "-c"]):
        return False
    image = container.get("image", "")
    if runner:
        return image.startswith("quay.io/agnosticd/ee-multicloud:") and not container.get("env") and "ansible-playbook main.yml" in command[2]
    env = {e.get("name"): e.get("value") for e in container.get("env", [])}
    return image.startswith("quay.io/redhat-gpte/troshka-ops-pod:") and env.get("TROSHKA_PROJECT_ID") == pid and set(env) <= {"TROSHKA_API_URL", "TROSHKA_API_KEY", "TROSHKA_PROJECT_ID", "OCP_VERSION", "TROSHKA_FRESH_INSTALL_LOG"} and "openshift-install" in command[2]


def writer_networks(value, root):
    cluster, bmc = [], None
    for node in root.get("spec", {}).get("topology", {}).get("nodes", []):
        data = node.get("data", {})
        if node.get("type") != "networkNode" or data.get("subtype") != "network":
            continue
        nad = "net-" + str(data.get("id", node.get("id", "")))[:8] + "-nad"
        if data.get("networkType") == "bmc":
            bmc = nad
        else:
            cluster.append(nad)
    if value["metadata"]["name"] != "workload-runner" and bmc:
        cluster.append(bmc)
    return value["metadata"].get("annotations", {}).get("k8s.v1.cni.cncf.io/networks") == ",".join(cluster)


def writer_mounts(value, by_uid, root=None):
    """No arbitrary PVC, hostPath or extra Secret mounts enter custody."""
    spec, meta = value["spec"], value["metadata"]
    secret_name = meta["name"] + "-config"
    secrets = [v for v in by_uid.values() if v.get("kind") == "Secret" and v["metadata"]["name"] == secret_name]
    if len(secrets) != 1 or secrets[0]["metadata"].get("ownerReferences") or secrets[0]["metadata"].get("labels") != meta["labels"]:
        return False
    if root is not None and not matches(secrets[0], root):
        return False
    mounts = spec["containers"][0].get("volumeMounts", [])
    keys = set(secrets[0].get("data", {}))
    seen = set()
    for mount in mounts:
        name = mount.get("name", "")
        if name == "ops-config":
            path = mount.get("mountPath", "")
            if not path.startswith("/workdir/") or mount.get("subPath") != path.lstrip("/").replace("/", "_") or mount.get("readOnly") is not True:
                return False
            seen.add(mount["subPath"])
        elif mount != {"name": "ops-workdir", "mountPath": "/workdir"} and mount != {"name": "project-ceph", "mountPath": "/workdir/project-ceph", "readOnly": True} and not (name.startswith("kube-api-access-") and mount.get("mountPath") == "/var/run/secrets/kubernetes.io/serviceaccount" and mount.get("readOnly") is True):
            return False
    if seen != keys:
        return False
    volumes = spec.get("volumes", [])
    for volume in volumes:
        name = volume.get("name", "")
        if name == "ops-workdir" and volume.get("emptyDir") == {}:
            continue
        if name in {"ops-config", "project-ceph"} and volume.get("secret", {}).get("secretName") == (secret_name if name == "ops-config" else "troshka-ceph-external") and set(volume) == {"name", "secret"}:
            continue
        if api_token_volume(volume):
            continue
        return False
    return {"ops-workdir", "ops-config"} <= {v.get("name") for v in volumes}


def api_token_volume(volume):
    if not volume.get("name", "").startswith("kube-api-access-") or set(volume) != {"name", "projected"}:
        return False
    sources = volume["projected"].get("sources", [])
    if len(sources) == 4 and sources[-1] == {"configMap": {"name": "openshift-service-ca.crt", "items": [{"key": "service-ca.crt", "path": "service-ca.crt"}]}}:
        sources = sources[:3]
    if len(sources) != 3 or [set(s) for s in sources] != [{"serviceAccountToken"}, {"configMap"}, {"downwardAPI"}]:
        return False
    token = sources[0]["serviceAccountToken"]
    return (
        token.get("path") == "token"
        and set(token) <= {"path", "expirationSeconds"}
        and sources[1]["configMap"] == {"name": "kube-root-ca.crt", "items": [{"key": "ca.crt", "path": "ca.crt"}]}
        and sources[2]["downwardAPI"] == {"items": [{"path": "namespace", "fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.namespace"}}]}
    )


def writer_secret(value, root, by_uid):
    if value.get("kind") != "Secret" or value["metadata"].get("ownerReferences"):
        return False
    name = value["metadata"]["name"]
    return orphan_ops_config(value, root) or any(writer_shape(p, root, by_uid) and name == p["metadata"]["name"] + "-config" for p in by_uid.values()) or any(p["kind"] == "Pod" and name == p["name"] + "-config" for p in evidence(root) or [])


def orphan_ops_config(value, root):
    """The installer intentionally leaves its project config after Pod removal."""
    pid = root.get("spec", {}).get("projectId", "")[:8]
    meta = value["metadata"]
    if meta["name"] != f"troshka-{pid}-ops-config" or meta.get("labels") != {"app": "troshka-ops-pod", "troshka-project": pid} or value.get("type", "Opaque") != "Opaque" or value.get("stringData"):
        return False
    clusters = {str(n.get("data", {}).get("clusterId") or n.get("data", {}).get("name") or "") for n in root.get("spec", {}).get("topology", {}).get("nodes", []) if n.get("type") == "clusterNode"}
    clusters.discard("")
    prefixes = {f"workdir_{c}_.src_" for c in clusters}
    required = {p + suffix for p in prefixes for suffix in ("agent-config.yaml", "install-config.yaml", "openshift_imagepruner-suspend.yaml")}
    allowed = required | {p + "openshift_itms-pull-through.yaml" for p in prefixes} | {"workdir_pull-secret.json"}
    keys = set(value.get("data", {}))
    return bool(required) and required <= keys <= allowed


def validate_intent(root, request):
    from helpers.managed_power import require

    pins = request["cleanupCustody"]
    require(
        request["mode"] == "stopped" and isinstance(pins, list) and len(pins) <= 1000,
        "custody_intent_invalid",
    )
    keys = {"kind", "name", "uid", "namespace", "digest", "owners"}
    identities = []
    for pin in pins:
        require(isinstance(pin, dict) and set(pin) == keys, "custody_intent_invalid")
        require(
            all(isinstance(pin[k], str) and bool(pin[k]) for k in keys - {"owners"}),
            "custody_intent_invalid",
        )
        require(
            pin["namespace"] == root["metadata"]["namespace"]
            and isinstance(pin["owners"], list),
            "custody_intent_invalid",
        )
        require(
            len(pin["digest"]) == 64
            and all(c in "0123456789abcdef" for c in pin["digest"]),
            "custody_intent_invalid",
        )
        identities.append((pin["kind"], pin["name"]))
    require(len(identities) == len(set(identities)), "custody_identity_duplicate")


def evidence(root):
    from helpers.managed_power import intent

    return (intent(root) or {}).get("cleanupCustody")


def matches(value, root):
    pins = evidence(root)
    return pins is not None and any(identity_matches(value, pin) for pin in pins)


def support_shape(value, root):
    """Recognize the exact non-workload shapes emitted by existing producers."""
    from helpers.vnc import build_vnc_rbac

    kind, meta = value["kind"], value["metadata"]
    name, ns = meta["name"], meta["namespace"]
    if (
        meta.get("ownerReferences")
        or meta.get("deletionTimestamp")
        or ns != root["metadata"]["namespace"]
    ):
        return False
    if kind == "ServiceAccount":
        export_complete = name == "troshka-export" and root.get("status", {}).get("phase") == "CaptureComplete" and root.get("status", {}).get("captureProgress") == "Done"
        return (
            (export_complete or name in {"troshka-vnc", "troshka-recert", "troshka-bmc", "troshka-ceph", "troshka-network"})
            and value.get("automountServiceAccountToken") is not False
        )
    if kind == "Secret":
        keys = {
            "accessKeyId",
            "secretKey",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_DEFAULT_REGION",
        }
        return (
            name in {"s3-credentials", "s3-central-credentials", "s3-obc-credentials"}
            and value.get("type", "Opaque") == "Opaque"
            and set(value.get("data", {})) in (keys, keys | {"AWS_ENDPOINT_URL"})
            and not value.get("stringData")
        )
    for expected in build_vnc_rbac(ns):
        if kind == expected["kind"] and name == expected["metadata"]["name"]:
            return all(
                value.get(k) == v for k, v in expected.items() if k != "metadata"
            )
    if name == "troshka-bmc":
        return bmc_grant(value, ns)
    if kind == "RoleBinding" and name == "troshka-provider-scc-exec":
        return value.get("roleRef") == {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": "system:openshift:scc:kubevirt-controller",
        } and value.get("subjects") == [
            {"kind": "ServiceAccount", "name": "troshka", "namespace": "troshka"}
        ]
    return False


def bmc_grant(value, ns):
    kind, name = value["kind"], value["metadata"]["name"]
    if kind == "Role":
        rules = [
            {
                "apiGroups": ["kubevirt.io"],
                "resources": ["virtualmachines"],
                "verbs": ["get", "list", "patch"],
            },
            {
                "apiGroups": ["kubevirt.io"],
                "resources": ["virtualmachineinstances"],
                "verbs": ["get", "list", "patch", "delete"],
            },
            {
                "apiGroups": ["cdi.kubevirt.io"],
                "resources": ["datavolumes"],
                "verbs": ["create", "get", "list", "delete"],
            },
            {
                "apiGroups": [""],
                "resources": ["persistentvolumeclaims"],
                "verbs": ["get", "list"],
            },
        ]
        return value.get("rules") == rules
    if kind == "RoleBinding":
        return value.get("roleRef") == {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": name,
        } and value.get("subjects") == [
            {"kind": "ServiceAccount", "name": name, "namespace": ns}
        ]
    return False


def support(value, by_uid):
    roots = [v for v in by_uid.values() if v.get("kind") == "TroshkaProject"]
    if len(roots) != 1:
        return None
    root = roots[0]
    if (support_shape(value, root) or writer_secret(value, root, by_uid) or network_config(value, by_uid) or route_access(value, root, by_uid) or eip_service(value, root, by_uid) or showroom_service(value, root)) and matches(value, root):
        return {"custody": identity(value)}
    return None


def eip_service(value, root, by_uid):
    if value.get("kind") != "Service" or value["metadata"].get("ownerReferences"):
        return False
    meta, spec = value["metadata"], value.get("spec", {})
    eid = meta.get("labels", {}).get("troshka-eip-id")
    pid = root.get("spec", {}).get("projectId", "")[:8]
    return (
        isinstance(eid, str) and len(eid) == 8 and all(c in "0123456789abcdef" for c in eid) and meta["name"] == "troshka-eip-" + eid
        and bool(pid) and meta.get("namespace") == root["metadata"].get("namespace")
        and meta.get("labels") == {"app": "troshka-eip", "troshka-eip-id": eid}
        and spec.get("type") == "LoadBalancer"
        and spec.get("selector") == {"app": "troshka-gateway-" + pid}
        and not spec.get("externalName") and not spec.get("externalIPs")
        and bool(spec.get("ports"))
        and (eip_placeholder_ports(spec["ports"]) or (
            all(p.get("protocol") == "TCP" and isinstance(p.get("port"), int) and p.get("targetPort") == {80: 1080, 443: 1443, 8080: 18080}.get(p["port"], p["port"]) for p in spec["ports"])
            and all(p["port"] in {int(parts[0]) for parts in route_gateway_forwards(root, by_uid, {"app": "troshka-gateway-" + pid}) if parts[3] == "tcp"} for p in spec["ports"])
        ))
    )


def eip_placeholder_ports(ports):
    """allocate_eip emits 443 -> 443 before gateway forwarding is configured."""
    if len(ports) != 1:
        return False
    port = ports[0]
    return (
        set(port) <= {"port", "targetPort", "protocol", "nodePort"}
        and {k: port.get(k) for k in ("port", "targetPort", "protocol")}
        == {"port": 443, "targetPort": 443, "protocol": "TCP"}
        and ("nodePort" not in port or type(port["nodePort"]) is int and 0 < port["nodePort"] <= 65535)
    )


def gateway_ports(by_uid, selector):
    return {int(parts[0]) for parts in gateway_forwards(by_uid, selector)}


def gateway_forwards(by_uid, selector):
    result = []
    for value in by_uid.values():
        if value.get("kind") != "Deployment" or not all(value.get("spec", {}).get("selector", {}).get("matchLabels", {}).get(k) == v for k, v in selector.items()):
            continue
        containers = value["spec"].get("template", {}).get("spec", {}).get("containers", [])
        forwards = [e.get("value", "") for c in containers for e in c.get("env", []) if e.get("name") == "PORT_FORWARDS"]
        for forward in ",".join(forwards).split(","):
            parts = forward.split(":")
            if len(parts) == 4 and parts[0].isdigit():
                result.append(parts)
    return result


def network_config(value, by_uid):
    from helpers.dnsmasq import generate_dnsmasq_config

    if value.get("kind") != "ConfigMap" or value["metadata"].get("ownerReferences") or value.get("binaryData"):
        return False
    return any(
        parent.get("kind") == "TroshkaNetwork"
        and value["metadata"]["name"] == "dnsmasq-" + parent["metadata"]["name"]
        and value.get("data") == {"dnsmasq.conf": generate_dnsmasq_config(parent["spec"])}
        for parent in by_uid.values()
    )


def route_access(value, root, by_uid):
    """Validate the backend's paired gateway Service/Route scaffold."""
    if value.get("kind") not in {"Service", "Route"}:
        return False
    meta = value["metadata"]
    pid = root.get("spec", {}).get("projectId", "")[:8]
    labels = {"app": "troshka-route-access", "troshka-project": pid}
    if not pid or meta.get("ownerReferences") or meta.get("labels") != labels:
        return False
    name = meta["name"]
    if value["kind"] == "Route" and name.startswith("tpf-"):
        return app_proxy_route(value, root, by_uid)
    pairs = {v["kind"]: v for v in by_uid.values() if v.get("kind") in {"Service", "Route"} and v["metadata"]["name"] == name}
    if set(pairs) != {"Service", "Route"} or any(v["metadata"].get("labels") != labels or v["metadata"].get("ownerReferences") for v in pairs.values()):
        return False
    service, route = pairs["Service"].get("spec", {}), pairs["Route"].get("spec", {})
    selector = {"app": "troshka-gateway-" + pid}
    if service.get("selector") != selector or service.get("type") != "ClusterIP" or any(service.get(k) for k in ("externalName", "externalIPs", "loadBalancerIP")):
        return False
    ports = service.get("ports", [])
    if len(ports) != 1 or ports[0].get("protocol") != "TCP" or ports[0].get("port") != ports[0].get("targetPort"):
        return False
    port = ports[0]["port"]
    if route.get("to", {}).get("kind") != "Service" or route.get("to", {}).get("name") != name or route.get("port") != {"targetPort": port} or route.get("alternateBackends"):
        return False
    if route.get("tls") not in ({"termination": "passthrough"}, {"termination": "edge", "insecureEdgeTerminationPolicy": "Redirect"}):
        return False
    return route_gateway(name, port, selector, by_uid, root)


def app_proxy_route(value, root, by_uid):
    pid = root["spec"]["projectId"][:8]
    hosts = [h for n in root["spec"].get("topology", {}).get("nodes", []) if n.get("data", {}).get("isShowroom") for tab in n["data"].get("showroomTabs", []) for h in tab.get("proxyHosts", [])]
    names = {("tpf-" + pid + "-" + app_proxy_code(h))[:63] for h in hosts}
    if value["metadata"]["name"] not in names:
        return False
    sources = [v for v in by_uid.values() if v.get("kind") == "Route" and v["metadata"]["name"] == "showroom"]
    return len(sources) == 1 and route_access(sources[0], root, by_uid) and all(value.get("spec", {}).get(k) == sources[0].get("spec", {}).get(k) for k in ("to", "port", "tls")) and not value.get("spec", {}).get("alternateBackends")


def app_proxy_code(host):
    """Same deterministic identifier as backend showroom_scaffold."""
    import hashlib
    import re
    host = (host or "").strip().lower()
    parts = host.split(".")
    label = parts[0]
    cluster = re.sub(r"[^a-z0-9-]+", "-", parts[2]).strip("-")[:16] if len(parts) >= 3 and parts[1] == "apps" else ""
    code = {"console-openshift-console": "con", "oauth-openshift": "oauth"}.get(label)
    if code is None:
        slug = re.sub(r"[^a-z0-9]+", "-", label).strip("-")[:8].strip("-")
        suffix = hashlib.sha1(host.encode(), usedforsecurity=False).hexdigest()[:6]
        code = f"{slug}-{suffix}" if slug else suffix
    return f"{code}-{cluster}" if cluster else code


def route_gateway(name, port, selector, by_uid, root):
    """Bind the published port to a retained project gateway forward."""
    for parts in route_gateway_forwards(root, by_uid, selector):
        external = int(parts[0])
        listen = {80: 1080, 443: 1443, 8080: 18080}.get(external, external)
        if parts[3] == "tcp" and port == listen and name in route_names(root, parts[1], parts[2]):
            return True
    return False


def route_gateway_forwards(root, by_uid, selector):
    """Use retained topology only when its corresponding gateway no longer exists."""
    if any(value.get("kind") == "Deployment" and all(value.get("spec", {}).get("selector", {}).get("matchLabels", {}).get(k) == v for k, v in selector.items()) for value in by_uid.values()):
        return gateway_forwards(by_uid, selector)
    from helpers.k8s import _build_port_forwards_str

    gateways = [node.get("data", {}) for node in root.get("spec", {}).get("topology", {}).get("nodes", []) if node.get("data", {}).get("subtype") == "gateway"]
    if len(gateways) != 1:
        return []
    forwards = _build_port_forwards_str(gateways[0].get("portForwards", []))
    parts = [forward.split(":") for forward in forwards.split(",")]
    return [part for part in parts if len(part) == 4 and part[0].isdigit()]


def route_names(root, ip, port):
    from helpers.topology import (
        enrich_container_nics,
        enrich_showroom_infra_networks,
        extract_containers,
    )
    topology = deepcopy(root.get("spec", {}).get("topology", {}))
    containers = extract_containers(topology)
    enrich_container_nics(topology, containers)
    enrich_showroom_infra_networks(topology, containers)
    names = {f"rt-{ip.replace('.', '-')}-{port}"[:63]}
    pid = root.get("spec", {}).get("projectId", "")[:8]
    if ip == "showroom-" + pid and str(port) == "80" and any(c.get("isShowroom") for c in containers):
        names.add("showroom")
    data = [n.get("data", {}) for n in topology.get("nodes", [])]
    data.extend({**c, "label": c["name"]} for c in containers)
    for node in data:
        if any(nic.get("ip") == ip for nic in node.get("nics", [])):
            label = node.get("label", "")
            names.add("showroom" if label.lower() == "showroom" else f"rt-{label}-{port}"[:63])
    return names


def showroom_service(value, root):
    from helpers.topology import extract_containers
    if value.get("kind") != "Service" or value["metadata"].get("ownerReferences"):
        return False
    pid = root.get("spec", {}).get("projectId", "")[:8]
    meta, spec = value["metadata"], value.get("spec", {})
    showrooms = [c for c in extract_containers(root.get("spec", {}).get("topology", {})) if c.get("isShowroom")]
    return (
        len(showrooms) == 1 and meta["name"] == "showroom-" + pid
        and meta.get("labels") == {"app": "troshka-showroom", "troshka-project": pid}
        and spec.get("type") == "ClusterIP"
        and spec.get("selector") == {"troshka-pod": showrooms[0]["id"][:8]}
        and spec.get("ports") == [{"name": "http", "port": 80, "targetPort": 80, "protocol": "TCP"}]
        and not spec.get("externalName") and not spec.get("externalIPs")
    )


def persistent_state(value, parent):
    if value["kind"] != "PersistentVolumeClaim" or parent["kind"] != "VirtualMachine":
        return False
    domain = (
        parent.get("spec", {}).get("template", {}).get("spec", {}).get("domain", {})
    )
    enabled = (
        domain.get("devices", {}).get("tpm", {}).get("persistent") is True
        or domain.get("firmware", {})
        .get("bootloader", {})
        .get("efi", {})
        .get("persistent")
        is True
    )
    prefix = "persistent-state-for-" + parent["metadata"]["name"]
    meta = value["metadata"]
    suffix = meta["name"].removeprefix(prefix + "-")
    generated = meta.get("generateName") == prefix + "-" and len(suffix) == 5 and all(c in "bcdfghjklmnpqrstvwxz2456789" for c in suffix)
    return enabled and (meta["name"] == prefix or generated)


def bmc_manifest(root, by_uid):
    from helpers.bmc import build_bmc_deployment

    ns = root["metadata"]["namespace"]
    topology = root["spec"].get("topology", {})
    nodes = topology.get("nodes", [])
    networks = [
        v
        for v in by_uid.values()
        if v.get("kind") == "TroshkaNetwork"
        and v.get("spec", {}).get("networkType") == "bmc"
    ]
    if len(networks) != 1:
        return None
    network = networks[0]
    nad = network.get("status", {}).get("nadName", network["metadata"]["name"] + "-nad")
    vms = bmc_vms(nodes, by_uid)
    if not vms:
        return None
    credentials = next(
        (
            {
                "username": n.get("data", {}).get("bmcUsername", ""),
                "password": n.get("data", {}).get("bmcPassword", ""),
            }
            for n in nodes
            if n.get("data", {}).get("networkType") == "bmc"
        ),
        {},
    )
    return build_bmc_deployment(ns.replace("troshka-", ""), ns, vms, nad, credentials)


def bmc_vms(nodes, by_uid):
    ips = {
        n.get("data", {}).get("id", n.get("id")): n.get("data", {}).get("bmcIp", "")
        for n in nodes
        if n.get("type") == "vmNode"
    }
    result = []
    for vm in by_uid.values():
        spec = vm.get("spec", {})
        if vm.get("kind") == "TroshkaVM" and spec.get("bmcEnabled"):
            result.append(
                {
                    "vmId": spec["vmId"],
                    "bmcIp": spec.get("bmcIp") or ips.get(spec["vmId"], ""),
                    "domainUuid": vm.get("status", {}).get("domainUuid", ""),
                }
            )
    return result


def bmc_shape(value, root, by_uid):
    from helpers.managed_power import manifest_matches

    if value.get("kind") != "Deployment" or value["metadata"].get("ownerReferences"):
        return False
    manifest = bmc_manifest(root, by_uid)
    if manifest is None:
        return False
    ns = root["metadata"]["namespace"]
    actual = value.get("spec", {}).get("template", {}).get("spec", {})
    containers = actual.get("containers", [])
    if len(containers) != 1 or not containers[0].get("image", "").startswith(
        "quay.io/redhat-gpte/troshka-bmc:"
    ):
        return False
    manifest["spec"]["template"]["spec"]["containers"][0]["image"] = containers[0][
        "image"
    ]
    return (
        value["metadata"]["name"] == manifest["metadata"]["name"]
        and value["metadata"].get("namespace") == ns
        and value["spec"].get("selector") == manifest["spec"]["selector"]
        and manifest_matches(
            manifest["spec"]["template"]["metadata"],
            value["spec"]["template"].get("metadata", {}),
        )
        and manifest_matches(manifest["spec"]["template"]["spec"], actual)
    )


def verify_ceph(root, rows, by_uid):
    from helpers.ceph_appliance import (
        build_export_job,
    )
    from helpers.managed_power import manifest_matches, parent_kind, require
    from helpers.topology import extract_ceph_cluster

    desired = extract_ceph_cluster(root["spec"].get("topology", {}))
    require(
        len(rows["TroshkaCeph"]) == (1 if desired else 0),
        "custody_ceph_inventory_changed",
    )
    for ceph in rows["TroshkaCeph"]:
        require(
            parent_kind(ceph, by_uid) == "TroshkaProject"
            and ceph.get("status", {}).get("phase") == "Ready",
            "custody_ceph_not_settled",
        )
        require(manifest_matches(desired, ceph["spec"]), "custody_ceph_spec_changed")
        verify_ceph_deployments(ceph, rows, by_uid)
    for job in rows["Job"]:
        require(parent_kind(job, by_uid) == "TroshkaCeph", "custody_job_unsupported")
        ceph = by_uid[job["metadata"]["ownerReferences"][0]["uid"]]
        expected_job = build_export_job(ceph)
        historical_helper_images(expected_job["spec"]["template"]["spec"], job["spec"]["template"]["spec"])
        require(
            job["metadata"]["name"] == expected_job["metadata"]["name"]
            and manifest_matches(
                expected_job["spec"]["template"]["spec"],
                job["spec"]["template"]["spec"],
            ),
            "custody_export_changed",
        )
        require(
            job.get("status", {}).get("succeeded") == 1
            and not job.get("status", {}).get("active"),
            "custody_export_active",
        )


def verify_ceph_deployments(ceph, rows, by_uid):
    from helpers.ceph_appliance import build_mon_deployment, build_osd_deployment
    from helpers.managed_power import manifest_matches, parent_kind, require
    from helpers.rook_ceph import normalize_ceph_counts

    count, _, _ = normalize_ceph_counts(ceph["spec"])
    deployments = [
        v for v in rows["Deployment"] if parent_kind(v, by_uid) == "TroshkaCeph"
    ]
    require(bool(deployments), "custody_ceph_deployment_missing")
    containers = deployments[0]["spec"]["template"]["spec"].get("containers", [])
    require(bool(containers), "custody_ceph_container_changed")
    image = containers[0]["image"]
    manifests = [build_mon_deployment(ceph, image)] + [
        build_osd_deployment(ceph, image, i) for i in range(count)
    ]
    expected = {v["metadata"]["name"]: v for v in manifests}
    require(
        {v["metadata"]["name"] for v in deployments} == set(expected),
        "custody_ceph_deployment_missing",
    )
    for deployment in deployments:
        manifest = expected[deployment["metadata"]["name"]]
        historical_helper_images(manifest["spec"]["template"]["spec"], deployment["spec"]["template"]["spec"])
        require(
            manifest_matches(
                manifest["spec"]["template"]["spec"],
                deployment["spec"]["template"]["spec"],
            ),
            "custody_ceph_manifest_changed",
        )
        require(
            manifest_matches(
                manifest["spec"]["template"]["metadata"],
                deployment["spec"]["template"].get("metadata", {}),
            )
            and deployment["spec"].get("selector") == manifest["spec"]["selector"],
            "custody_ceph_network_changed",
        )


def historical_helper_images(expected, actual):
    """Historical Troshka image tags may differ; every other field stays exact."""
    repositories = {"quay.io/redhat-gpte/troshka-tools", "quay.io/redhat-gpte/troshka-gateway"}
    for field in ("containers", "initContainers"):
        live = {c.get("name"): c for c in actual.get(field, [])}
        for container in expected.get(field, []):
            repository = container.get("image", "").split(":", 1)[0]
            image = live.get(container["name"], {}).get("image", "")
            if repository in repositories and image.startswith(repository + ":"):
                container["image"] = image


def container_spec_matches(expected, actual):
    from helpers.managed_power import manifest_matches

    expected, actual = deepcopy(expected), deepcopy(actual)
    historical_helper_images(expected, actual)
    for spec in (expected, actual):
        for field in ("containers", "initContainers"):
            for container in spec.get(field, []):
                capabilities = container.get("securityContext", {}).get("capabilities", {})
                for key in ("add", "drop"):
                    if key in capabilities:
                        capabilities[key] = sorted(capabilities[key])
    return manifest_matches(expected, actual)
