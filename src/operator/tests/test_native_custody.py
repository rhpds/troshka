"""Legacy custody never confers normal admission or name-only deletion rights."""

import json
from copy import deepcopy

import pytest
from helpers import managed_power as power
from helpers import native_custody as custody
from helpers.bmc import build_bmc_deployment
from helpers.vnc import build_vnc_rbac

from tests.test_managed_power import FakeAPI, resource


@pytest.mark.parametrize("annotations", [None, {}, {"k8s.v1.cni.cncf.io/networks": "net"}])
def test_restart_matches_original_receipt_without_changing_identity(annotations):
    root = resource("TroshkaProject", "project")
    value = resource("Deployment", "helper", root, {"template": {"metadata": {}, "spec": {"containers": [{"name": "helper", "image": "original"}]}}})
    if annotations is not None:
        value["spec"]["template"]["metadata"]["annotations"] = annotations
    pin = custody.identity(value)
    attest(root, [value])
    value["spec"]["template"]["metadata"].setdefault("annotations", {})["kubectl.kubernetes.io/restartedAt"] = "2026-11-17T00:00:00Z"
    before, original_pin = deepcopy(value), deepcopy(pin)
    assert custody.identity_matches(value, pin)
    assert custody.matches(value, root)
    assert custody.identity(value) != pin
    assert value == before and pin == original_pin


@pytest.mark.parametrize("change", ["image", "mount", "owner", "uid", "network", "unknown"])
def test_restart_does_not_hide_other_custody_changes(change):
    value = resource("Deployment", "helper", spec={"template": {"metadata": {"annotations": {}}, "spec": {"containers": [{"name": "helper", "image": "original"}]}}})
    pin = custody.identity(value)
    template = value["spec"]["template"]
    template["metadata"]["annotations"]["kubectl.kubernetes.io/restartedAt"] = "2026-11-17T00:00:00Z"
    if change == "image":
        template["spec"]["containers"][0]["image"] = "foreign"
    elif change == "mount":
        template["spec"]["containers"][0]["volumeMounts"] = [{"name": "foreign", "mountPath": "/foreign"}]
    elif change == "owner":
        value["metadata"]["ownerReferences"] = [{"uid": "foreign"}]
    elif change == "uid":
        value["metadata"]["uid"] = "replacement"
    elif change == "network":
        template["metadata"]["annotations"]["k8s.v1.cni.cncf.io/networks"] = "foreign"
    else:
        template["spec"]["unknownField"] = "foreign"
    assert not custody.identity_matches(value, pin)


def test_receipt_with_original_restart_timestamp_remains_exact_only():
    value = resource("Deployment", "helper", spec={"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "original"}}}})
    pin = custody.identity(value)
    assert custody.identity_matches(value, pin)
    value["spec"]["template"]["metadata"]["annotations"]["kubectl.kubernetes.io/restartedAt"] = "changed"
    assert not custody.identity_matches(value, pin)


def test_restart_compatibility_does_not_apply_to_other_kinds():
    value = resource("VirtualMachine", "vm", spec={"template": {"metadata": {"annotations": {}}}})
    pin = custody.identity(value)
    value["spec"]["template"]["metadata"]["annotations"]["kubectl.kubernetes.io/restartedAt"] = "changed"
    assert not custody.identity_matches(value, pin)


@pytest.mark.parametrize("change", [None, "image", "owner", "network"])
def test_fresh_custody_pin_survives_repeated_restarts_only(change):
    value = resource("Deployment", "helper", spec={"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "restart1"}}, "spec": {"containers": [{"name": "helper", "image": "original"}]}}})
    before = deepcopy(value)
    raw = custody.identity(value)
    pin = custody.custody_identity(value)
    assert value == before and custody.identity(value) == raw
    assert raw != pin
    value["spec"]["template"]["metadata"]["annotations"]["kubectl.kubernetes.io/restartedAt"] = "restart2"
    if change == "image":
        value["spec"]["template"]["spec"]["containers"][0]["image"] = "foreign"
    elif change == "owner":
        value["metadata"]["ownerReferences"] = [{"uid": "foreign"}]
    elif change == "network":
        value["spec"]["template"]["metadata"]["annotations"]["k8s.v1.cni.cncf.io/networks"] = "foreign"
    assert custody.identity_matches(value, pin) is (change is None)


def test_fresh_custody_identity_only_normalizes_deployment_restart_annotation():
    value = resource("VirtualMachine", "vm", spec={"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "restart1"}}}})
    assert custody.custody_identity(value) == custody.identity(value)


def test_fresh_custody_pin_matches_empty_annotations_before_first_restart():
    value = resource("Deployment", "helper", spec={"template": {"metadata": {"annotations": {}}}})
    pin = custody.custody_identity(value)
    assert custody.identity_matches(value, pin)
    value["spec"]["template"]["metadata"]["annotations"]["kubectl.kubernetes.io/restartedAt"] = "restart1"
    assert custody.identity_matches(value, pin)


@pytest.mark.parametrize("change", [None, "uid", "spec", "labels", "foreign"])
def test_sealed_custody_survives_staged_dependency_deletion(change):
    root = resource("TroshkaProject", "project", spec={"projectId": "project-id"})
    gateway = resource("Deployment", "gateway", root, {"replicas": 0})
    network = resource("TroshkaNetwork", "network", root)
    route = resource("Route", "access-route", spec={"to": {"name": "access-service"}})
    service = resource("Service", "access-service", spec={"selector": {"app": "gateway"}})
    config = resource("ConfigMap", "dnsmasq-config")
    config["data"] = {"dnsmasq.conf": "source-validated"}
    secret = resource("Secret", "credentials")
    secret["data"] = {"access_key": "never-in-receipt"}
    role = resource("Role", "console-role")
    values = [gateway, network, route, service, config, secret, role]
    objects = []
    for value in values:
        meta = value["metadata"]
        sealed = {"kind": value["kind"], "name": meta["name"], "namespace": "ns", "uid": meta["uid"]}
        if value in [route, service, config, secret, role]:
            sealed["namespace_bootstrap"] = {"custody": custody.identity(value)}
        objects.append(sealed)
    objects.sort(key=lambda v: (v["kind"], v["name"]))
    payload = {
        "version": "native-cleanup-v1", "namespace_mode": "managed", "intent_id": "cleanup-1", "generation": "allocation-1",
        "root": {"namespace": "ns", "namespace_uid": "ns-uid", "project_cr_uid": root["metadata"]["uid"], "deployment_generation": 1},
        "stop_proof": {"request_id": "stop-1", "generation": "allocation-1"},
        "objects": objects, "inventory_digest": power.digest(objects),
    }
    root["metadata"]["annotations"] = {power.CLEANUP_ANNOTATION: json.dumps(payload), power.ANNOTATION: json.dumps({"version": 1, "mode": "stopped", "requestId": "stop-1", "allocationGeneration": "allocation-1"})}

    class API(FakeAPI):
        def request(self, method, kind, namespace, name, body):
            assert method == "DELETE"
            meta = self.values[kind, name]["metadata"]
            assert body["preconditions"] == {"uid": meta["uid"], "resourceVersion": meta["resourceVersion"]}
            self.calls.append((kind, name))
            del self.values[kind, name]
            return {"kind": "Status", "status": "Success"}

    api = API([root, resource("Namespace", "ns"), *values])
    assert power.cleanup_delete(api, root) is False
    assert ("Deployment", "gateway") not in api.values
    assert power.cleanup_delete(api, root) is False
    assert ("TroshkaNetwork", "network") not in api.values
    assert ("Route", "access-route") not in api.values
    # Access-service now lacks its gateway; DNS config lacks its network.
    current = api.values["Service", "access-service"]
    if change == "uid":
        current["metadata"]["uid"] = "replacement"
    elif change == "spec":
        current["spec"]["selector"] = {"app": "foreign"}
    elif change == "labels":
        current["metadata"]["labels"] = {"foreign": "true"}
    elif change == "foreign":
        api.values["Service", "foreign"] = resource("Service", "foreign")
    if change:
        before = deepcopy(api.calls)
        with pytest.raises(power.ManagedPowerError):
            power.cleanup_delete(api, root)
        assert api.calls == before
    else:
        assert power.cleanup_delete(api, root) is False
        assert ("Service", "access-service") not in api.values
        assert power.cleanup_delete(api, root) is True
        assert all((v["kind"], v["metadata"]["name"]) in api.values for v in [config, secret, role])
        assert "never-in-receipt" not in json.dumps(payload)


def writer_fixture(runner=False):
    root = resource("TroshkaProject", "project", spec={"projectId": "12345678-project", "topology": {"nodes": []}})
    name = "workload-runner" if runner else "troshka-12345678-ops"
    pod = resource("Pod", name, spec={
        "serviceAccountName": "troshka-network", "restartPolicy": "Never" if runner else "Always",
        "containers": [{"name": "ops", "image": "quay.io/agnosticd/ee-multicloud:chained-latest" if runner else "quay.io/redhat-gpte/troshka-ops-pod:latest", "imagePullPolicy": "Always", "command": ["bash", "-lc", "ansible-playbook main.yml" if runner else "openshift-install"], "env": [] if runner else [{"name": "TROSHKA_PROJECT_ID", "value": "12345678-project"}], "securityContext": {"privileged": True, "capabilities": {"add": ["NET_ADMIN", "NET_RAW"]}}, "resources": {"requests": {"cpu": "500m", "memory": "2Gi"}, "limits": {"cpu": "2", "memory": "4Gi"}}, "volumeMounts": [{"name": "ops-workdir", "mountPath": "/workdir"}, {"name": "ops-config", "mountPath": "/workdir/config", "subPath": "workdir_config", "readOnly": True}]}],
        "volumes": [{"name": "ops-workdir", "emptyDir": {}}, {"name": "ops-config", "secret": {"secretName": name + "-config"}}],
    })
    pod["metadata"].update(labels={"app": "troshka-ops-pod", "troshka-project": "12345678"}, annotations={"k8s.v1.cni.cncf.io/networks": ""})
    secret = resource("Secret", name + "-config")
    secret["metadata"]["labels"] = deepcopy(pod["metadata"]["labels"])
    secret["data"] = {"workdir_config": "private"}
    index = {v["metadata"]["uid"]: v for v in [root, pod, secret]}
    attest(root, [pod, secret])
    return root, pod, secret, index


@pytest.mark.parametrize("runner", [False, True])
def test_writer_scaffold_is_pinned_without_mutation(runner):
    root, pod, secret, index = writer_fixture(runner)
    before = deepcopy([root, pod, secret])
    power.verify_chain(pod, root, index)
    assert power.parent_kind(pod, index) == "CustodyWriter"
    assert power.cleanup_bootstrap(secret, index)
    assert [root, pod, secret] == before
    assert "private" not in json.dumps(custody.evidence(root))


@pytest.mark.parametrize("mutation", ["uid", "command", "sidecar", "hostPath", "network", "owner"])
def test_writer_replacement_or_configuration_drift_rejected(mutation):
    root, pod, _, index = writer_fixture()
    if mutation == "uid":
        pod["metadata"]["uid"] = "replacement"
    elif mutation == "command":
        pod["spec"]["containers"][0]["command"][2] += "; foreign"
    elif mutation == "sidecar":
        pod["spec"]["containers"].append({"name": "foreign"})
    elif mutation == "hostPath":
        pod["spec"]["volumes"].append({"name": "host", "hostPath": {"path": "/"}})
    elif mutation == "network":
        pod["metadata"]["annotations"]["k8s.v1.cni.cncf.io/networks"] = "foreign"
    else:
        pod["metadata"]["ownerReferences"] = [{"uid": "foreign"}]
    with pytest.raises(power.ManagedPowerError):
        power.verify_chain(pod, root, index)


def test_writer_drain_requires_observed_absence_and_uid_preconditions():
    root, pod, _, _ = writer_fixture()
    state = {"targets": [{"kind": "Pod", "name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"], "custodyWriter": True}]}
    calls = []
    class API:
        def get(self, *args):
            return root
        def request(self, *args):
            calls.append(args)
    index = {("Pod", pod["metadata"]["name"]): pod}
    assert not power.drain_writers(API(), root, index, state)
    assert calls[0][0:2] == ("DELETE", "Pod")
    assert calls[0][-1]["preconditions"] == {"uid": pod["metadata"]["uid"], "resourceVersion": pod["metadata"]["resourceVersion"]}
    assert power.drain_writers(API(), root, {}, state)


def test_stop_does_not_touch_vm_while_writer_deletion_is_pending():
    root, pod, _, _ = writer_fixture()
    vm = resource("VirtualMachine", "vm", spec={"runStrategy": "Always"})
    state = {"targets": [
        {"kind": "VirtualMachine", "name": "vm", "uid": vm["metadata"]["uid"]},
        {"kind": "Pod", "name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"], "custodyWriter": True},
    ]}
    api = FakeAPI([root, pod, vm])
    rows = {k: [] for k in power.INVENTORY}
    rows.update(Pod=[pod], VirtualMachine=[vm])
    power.stop_targets(api, root, rows, state)
    assert api.calls and all(call[0] == "DELETE" and call[1][0] == "Pod" for call in api.calls)
    assert vm["spec"]["runStrategy"] == "Always"


def test_projected_token_volume_cannot_smuggle_foreign_config():
    volume = {"name": "kube-api-access-abc", "projected": {"sources": [
        {"serviceAccountToken": {"path": "token", "expirationSeconds": 3607}},
        {"configMap": {"name": "kube-root-ca.crt", "items": [{"key": "ca.crt", "path": "ca.crt"}]}},
        {"downwardAPI": {"items": [{"path": "namespace", "fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.namespace"}}]}},
    ]}}
    assert custody.api_token_volume(volume)
    volume["projected"]["sources"].append({"configMap": {"name": "openshift-service-ca.crt", "items": [{"key": "service-ca.crt", "path": "service-ca.crt"}]}})
    assert custody.api_token_volume(volume)
    volume["projected"]["sources"][1]["configMap"]["name"] = "foreign"
    assert not custody.api_token_volume(volume)


@pytest.mark.parametrize("change", ["none", "progress", "annotation", "status", "ordinary"])
def test_historical_capture_complete_requires_finished_capture_and_no_active_work(change):
    root = resource("TroshkaProject", "project", spec={"topology": {"nodes": []}})
    root["status"] = {"phase": "CaptureComplete", "captureProgress": "Done"}
    attest(root, [])
    if change == "progress":
        root["status"]["captureProgress"] = "Exporting"
    elif change == "annotation":
        root["metadata"]["annotations"]["troshka.redhat.com/capture"] = "pending"
    elif change == "status":
        root["status"]["capture"] = {"active": True}
    elif change == "ordinary":
        root["metadata"]["annotations"] = {}
    rows = {k: [] for k in power.INVENTORY}
    if change == "none":
        power.profile(root, rows, {root["metadata"]["uid"]: root}, FakeAPI([]))
    else:
        with pytest.raises(power.ManagedPowerError):
            power.profile(root, rows, {root["metadata"]["uid"]: root}, FakeAPI([]))


def test_export_account_requires_completed_capture_and_pin():
    root = resource("TroshkaProject", "project")
    root["status"] = {"phase": "CaptureComplete", "captureProgress": "Done"}
    sa = resource("ServiceAccount", "troshka-export")
    attest(root, [sa])
    assert custody.support(sa, {root["metadata"]["uid"]: root})
    root["status"]["captureProgress"] = "Exporting"
    assert not custody.support(sa, {root["metadata"]["uid"]: root})


def test_showroom_flag_is_cleanup_only_and_still_uses_container_profile():
    topology = {"nodes": [{"type": "containerNode", "data": {"isShowroom": True}}]}
    power.validate_profile_nodes(topology, True)
    with pytest.raises(power.ManagedPowerError):
        power.validate_profile_nodes(topology, False)


def test_historical_helper_tags_and_capability_order_do_not_allow_command_drift():
    expected = {"containers": [{"name": "helper", "image": "quay.io/redhat-gpte/troshka-gateway:latest", "command": ["setup"], "securityContext": {"capabilities": {"add": ["SETUID", "SETGID"]}}}]}
    actual = deepcopy(expected)
    actual["containers"][0]["image"] = "quay.io/redhat-gpte/troshka-gateway:production"
    actual["containers"][0]["securityContext"]["capabilities"]["add"].reverse()
    assert custody.container_spec_matches(expected, actual)
    actual["containers"][0]["command"] = ["foreign"]
    assert not custody.container_spec_matches(expected, actual)
    actual = deepcopy(expected)
    actual["containers"][0]["image"] = "quay.io/foreign/gateway:production"
    assert not custody.container_spec_matches(expected, actual)


def test_dnsmasq_config_requires_exact_network_renderer_and_pin():
    from helpers.dnsmasq import generate_dnsmasq_config
    root = resource("TroshkaProject", "project")
    network = resource("TroshkaNetwork", "net", root, spec={"cidr": "10.0.0.0/24"})
    config = resource("ConfigMap", "dnsmasq-net")
    config["data"] = {"dnsmasq.conf": generate_dnsmasq_config(network["spec"])}
    attest(root, [network, config])
    index = {v["metadata"]["uid"]: v for v in [root, network, config]}
    assert custody.support(config, index)
    config["data"]["dnsmasq.conf"] += "\nserver=foreign"
    assert not custody.support(config, index)


def test_showroom_service_binds_original_topology_container():
    root = resource("TroshkaProject", "project", spec={"projectId": "12345678-project", "topology": {"nodes": [{"id": "showroom-node", "type": "containerNode", "data": {"label": "showroom", "isShowroom": True}}]}})
    service = resource("Service", "showroom-12345678", spec={"type": "ClusterIP", "selector": {"troshka-pod": "showroom"}, "ports": [{"name": "http", "port": 80, "targetPort": 80, "protocol": "TCP"}]})
    service["metadata"]["labels"] = {"app": "troshka-showroom", "troshka-project": "12345678"}
    assert custody.showroom_service(service, root)
    assert "showroom" in custody.route_names(root, "showroom-12345678", "80")
    service["spec"]["selector"]["troshka-pod"] = "foreign"
    assert not custody.showroom_service(service, root)


def test_app_proxy_names_are_derived_from_explicit_tab_hosts():
    assert custody.app_proxy_code("console-openshift-console.apps.destination.example") == "con-destination"
    assert custody.app_proxy_code("oauth-openshift.apps.source.example") == "oauth-source"
    root = resource("TroshkaProject", "project", spec={"projectId": "12345678-project", "topology": {"nodes": []}})
    route = resource("Route", "tpf-12345678-con-destination")
    assert not custody.app_proxy_route(route, root, {})


def test_orphan_installer_config_requires_topology_bound_keys_and_original_uid():
    root = resource("TroshkaProject", "project", spec={"projectId": "12345678-project", "topology": {"nodes": [{"type": "clusterNode", "data": {"clusterId": "source"}}]}})
    secret = resource("Secret", "troshka-12345678-ops-config")
    secret["metadata"]["labels"] = {"app": "troshka-ops-pod", "troshka-project": "12345678"}
    secret["data"] = {"workdir_source_.src_" + suffix: "private" for suffix in ("agent-config.yaml", "install-config.yaml", "openshift_imagepruner-suspend.yaml")}
    attest(root, [secret])
    index = {root["metadata"]["uid"]: root, secret["metadata"]["uid"]: secret}
    assert custody.support(secret, index)
    secret["metadata"]["uid"] = "replacement"
    assert not custody.support(secret, index)
    secret["data"]["workdir_foreign_.src_install-config.yaml"] = "private"
    assert not custody.orphan_ops_config(secret, root)


def test_route_pair_requires_project_gateway_forward_and_exact_service():
    root = resource("TroshkaProject", "project", spec={"projectId": "12345678-project"})
    gateway = resource("Deployment", "gateway", root, spec={"selector": {"matchLabels": {"app": "troshka-gateway-12345678", "troshka-role": "gateway"}}, "template": {"spec": {"containers": [{"env": [{"name": "PORT_FORWARDS", "value": "6443:10.0.0.4:6443:tcp"}]}]}}})
    service = resource("Service", "rt-10-0-0-4-6443", spec={"type": "ClusterIP", "selector": {"app": "troshka-gateway-12345678"}, "ports": [{"port": 6443, "targetPort": 6443, "protocol": "TCP"}]})
    route = resource("Route", service["metadata"]["name"], spec={"to": {"kind": "Service", "name": service["metadata"]["name"]}, "port": {"targetPort": 6443}, "tls": {"termination": "passthrough"}})
    route["metadata"]["uid"] += "-route"
    for value in (service, route):
        value["metadata"]["labels"] = {"app": "troshka-route-access", "troshka-project": "12345678"}
    index = {v["metadata"]["uid"]: v for v in [root, gateway, service, route]}
    assert custody.route_access(route, root, index)
    eip = resource("Service", "troshka-eip-abcdef12", spec=deepcopy(service["spec"]))
    eip["metadata"]["labels"] = {"app": "troshka-eip", "troshka-eip-id": "abcdef12"}
    eip["spec"]["type"] = "LoadBalancer"
    assert custody.eip_service(eip, root, index)
    eip["spec"]["ports"][0].update(port=9999, targetPort=9999)
    assert not custody.eip_service(eip, root, index)
    service["spec"]["selector"]["app"] = "foreign"
    assert not custody.route_access(route, root, index)


@pytest.mark.parametrize("node_port", [None, 30157])
def test_allocated_eip_placeholder_requires_original_cleanup_receipt_without_gateway(node_port):
    root = resource("TroshkaProject", "project", spec={"projectId": "c7d797e2-project"})
    service = resource("Service", "troshka-eip-32a039a0", spec={"type": "LoadBalancer", "selector": {"app": "troshka-gateway-c7d797e2"}, "ports": [{"port": 443, "targetPort": 443, "protocol": "TCP"}]})
    service["metadata"]["labels"] = {"app": "troshka-eip", "troshka-eip-id": "32a039a0"}
    if node_port is not None:
        service["spec"]["ports"][0]["nodePort"] = node_port
    index = {root["metadata"]["uid"]: root, service["metadata"]["uid"]: service}
    assert not custody.support(service, index)
    attest(root, [service])
    assert custody.support(service, index)
    service["metadata"]["uid"] = "replacement"
    assert not custody.support(service, index)


@pytest.mark.parametrize("change", ["target", "extra_port", "protocol", "selector", "label", "type", "namespace", "owner", "port_name", "node_port", "external_ip"])
def test_allocated_eip_placeholder_rejects_foreign_shapes_even_with_receipt(change):
    root = resource("TroshkaProject", "project", spec={"projectId": "c7d797e2-project"})
    service = resource("Service", "troshka-eip-32a039a0", spec={"type": "LoadBalancer", "selector": {"app": "troshka-gateway-c7d797e2"}, "ports": [{"port": 443, "targetPort": 443, "protocol": "TCP", "nodePort": 30157}]})
    service["metadata"]["labels"] = {"app": "troshka-eip", "troshka-eip-id": "32a039a0"}
    spec, meta = service["spec"], service["metadata"]
    if change == "target":
        spec["ports"][0]["targetPort"] = 9999
    elif change == "extra_port":
        spec["ports"].append({"port": 6443, "targetPort": 6443, "protocol": "TCP"})
    elif change == "protocol":
        spec["ports"][0]["protocol"] = "UDP"
    elif change == "selector":
        spec["selector"]["app"] = "foreign"
    elif change == "label":
        meta["labels"]["app"] = "foreign"
    elif change == "type":
        spec["type"] = "ClusterIP"
    elif change == "namespace":
        meta["namespace"] = "foreign"
    elif change == "owner":
        meta["ownerReferences"] = [{"uid": "foreign"}]
    elif change == "port_name":
        spec["ports"][0]["name"] = "foreign"
    elif change == "node_port":
        spec["ports"][0]["nodePort"] = "30157"
    else:
        spec["externalIPs"] = ["10.0.0.5"]
    attest(root, [service])
    index = {root["metadata"]["uid"]: root, service["metadata"]["uid"]: service}
    assert not custody.support(service, index)


@pytest.mark.parametrize("change", [None, "port", "ip", "selector", "duplicate_gateway", "udp", "live_wrong_forward", "no_receipt", "replacement"])
def test_route_retains_topology_custody_when_gateway_is_absent(change):
    gateway = {"id": "gw", "type": "networkNode", "data": {"subtype": "gateway", "portForwards": [{"extPort": "6443", "intIp": "10.0.0.4", "intPort": "6443", "proto": "tcp"}]}}
    root = resource("TroshkaProject", "project", spec={"projectId": "12345678-project", "topology": {"nodes": [gateway]}})
    service = resource("Service", "rt-10-0-0-4-6443", spec={"type": "ClusterIP", "selector": {"app": "troshka-gateway-12345678"}, "ports": [{"port": 6443, "targetPort": 6443, "protocol": "TCP"}]})
    route = resource("Route", service["metadata"]["name"], spec={"to": {"kind": "Service", "name": service["metadata"]["name"]}, "port": {"targetPort": 6443}, "tls": {"termination": "passthrough"}})
    route["metadata"]["uid"] += "-route"
    for value in (service, route):
        value["metadata"]["labels"] = {"app": "troshka-route-access", "troshka-project": "12345678"}
    if change == "port":
        gateway["data"]["portForwards"][0]["extPort"] = "9999"
    elif change == "ip":
        gateway["data"]["portForwards"][0]["intIp"] = "10.0.0.5"
    elif change == "selector":
        service["spec"]["selector"]["app"] = "foreign"
    elif change == "duplicate_gateway":
        root["spec"]["topology"]["nodes"].append(deepcopy(gateway))
    elif change == "udp":
        gateway["data"]["portForwards"][0]["proto"] = "udp"
    index = {v["metadata"]["uid"]: v for v in [root, service, route]}
    if change == "live_wrong_forward":
        live = resource("Deployment", "gateway", root, spec={"selector": {"matchLabels": {"app": "troshka-gateway-12345678"}}, "template": {"spec": {"containers": [{"env": [{"name": "PORT_FORWARDS", "value": "9999:10.0.0.4:6443:tcp"}]}]}}})
        index[live["metadata"]["uid"]] = live
    if change != "no_receipt":
        attest(root, [service, route])
    if change == "replacement":
        route["metadata"]["uid"] = "replacement"
    assert bool(custody.support(route, index)) == (change is None)


@pytest.mark.parametrize("change", [None, "wrong_target", "udp", "unmapped", "live_wrong_forward"])
def test_configured_eip_uses_retained_forwards_only_when_gateway_absent(change):
    root = resource("TroshkaProject", "project", spec={"projectId": "12345678-project", "topology": {"nodes": [{"type": "networkNode", "data": {"subtype": "gateway", "portForwards": [{"extPort": "2222", "intIp": "10.0.0.4", "intPort": "22", "proto": "tcp"}, {"extPort": "6443", "intIp": "10.0.0.4", "intPort": "6443", "proto": "tcp"}]}}]}})
    service = resource("Service", "troshka-eip-2e6b9ff9", spec={"type": "LoadBalancer", "selector": {"app": "troshka-gateway-12345678"}, "ports": [{"name": "pf-0", "port": 2222, "targetPort": 2222, "protocol": "TCP", "nodePort": 30157}, {"name": "pf-1", "port": 6443, "targetPort": 6443, "protocol": "TCP", "nodePort": 30158}]})
    service["metadata"]["labels"] = {"app": "troshka-eip", "troshka-eip-id": "2e6b9ff9"}
    if change == "wrong_target":
        service["spec"]["ports"][0]["targetPort"] = 22
    elif change == "udp":
        root["spec"]["topology"]["nodes"][0]["data"]["portForwards"][0]["proto"] = "udp"
    elif change == "unmapped":
        service["spec"]["ports"][0].update(port=9999, targetPort=9999)
    index = {root["metadata"]["uid"]: root, service["metadata"]["uid"]: service}
    if change == "live_wrong_forward":
        live = resource("Deployment", "gateway", root, spec={"selector": {"matchLabels": {"app": "troshka-gateway-12345678"}}, "template": {"spec": {"containers": [{"env": [{"name": "PORT_FORWARDS", "value": "9999:10.0.0.4:9999:tcp"}]}]}}})
        index[live["metadata"]["uid"]] = live
    assert not custody.support(service, index)
    attest(root, [service])
    assert bool(custody.support(service, index)) == (change is None)


def attest(root, values):
    root["metadata"]["annotations"] = {
        power.ANNOTATION: json.dumps(
            {
                "version": 1,
                "mode": "stopped",
                "requestId": "stop",
                "allocationGeneration": "custody",
                "cleanupCustody": [
                    custody.identity(v) for v in values if custody.stable(v)
                ],
            }
        )
    }


@pytest.mark.parametrize("mutation", ["uid", "rules", "owner"])
def test_console_support_requires_original_uid_exact_rules_and_no_foreign_owner(
    mutation,
):
    root = resource("TroshkaProject", "project")
    role = build_vnc_rbac("ns")[0]
    role["metadata"]["uid"] = "role-uid"
    attest(root, [role])
    index = {root["metadata"]["uid"]: root}
    assert power.cleanup_bootstrap(role, index) == {"custody": custody.identity(role)}
    changed = deepcopy(role)
    if mutation == "rules":
        changed["rules"][0]["verbs"].append("patch")
    elif mutation == "uid":
        changed["metadata"]["uid"] = "replacement"
    else:
        changed["metadata"]["ownerReferences"] = [{"uid": "foreign"}]
    with pytest.raises(power.ManagedPowerError):
        power.verify_chain(changed, root, index)
    assert power.cleanup_bootstrap(changed, index) is None


def test_s3_receipt_never_contains_secret_values_or_their_hashes():
    root = resource("TroshkaProject", "project")
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "s3-credentials", "namespace": "ns", "uid": "secret"},
        "type": "Opaque",
        "data": {
            k: "sensitive-payload"
            for k in (
                "accessKeyId",
                "secretKey",
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_DEFAULT_REGION",
            )
        },
    }
    assert custody.support_shape(secret, root)
    before = custody.identity(secret)
    secret["data"] = {k: "rotated-payload" for k in secret["data"]}
    assert custody.identity(secret) == before
    assert "payload" not in json.dumps(before)
    secret["data"]["foreign-key"] = "foreign"
    assert not custody.support_shape(secret, root)


def test_generated_pull_secret_of_legacy_account_requires_attested_uid():
    root = resource("TroshkaProject", "project")
    sa = resource("ServiceAccount", "troshka-vnc")
    sa["metadata"]["annotations"] = {"openshift.io/internal-registry-pull-secret-ref": "vnc-dockercfg"}
    secret = resource("Secret", "vnc-dockercfg")
    secret["metadata"]["ownerReferences"] = [{"apiVersion": "v1", "kind": "ServiceAccount", "name": "troshka-vnc", "uid": sa["metadata"]["uid"]}]
    secret["metadata"]["annotations"] = {"openshift.io/internal-registry-auth-token.service-account": "troshka-vnc", "openshift.io/internal-registry-auth-token.binding": "bound"}
    secret["type"] = "kubernetes.io/dockercfg"
    secret["data"] = {".dockercfg": "private"}
    attest(root, [sa, secret])
    by_uid = {v["metadata"]["uid"]: v for v in [root, sa, secret]}
    assert power.cleanup_bootstrap(secret, by_uid) is not None
    secret["metadata"]["uid"] = "replacement"
    assert power.cleanup_bootstrap(secret, by_uid) is None


def test_vm_persistent_state_is_retained_only_for_persistent_firmware():
    vm = resource(
        "VirtualMachine",
        "troshka-vm-12345678",
        spec={
            "template": {"spec": {"domain": {"devices": {"tpm": {"persistent": True}}}}}
        },
    )
    pvc = resource(
        "PersistentVolumeClaim", "persistent-state-for-troshka-vm-12345678", vm
    )
    assert custody.persistent_state(pvc, vm)
    generated = deepcopy(pvc)
    generated["metadata"].update(name=pvc["metadata"]["name"] + "-4ms4l", generateName=pvc["metadata"]["name"] + "-")
    assert custody.persistent_state(generated, vm)
    before = custody.identity(generated)
    generated["metadata"]["generateName"] = "foreign-"
    assert not custody.persistent_state(generated, vm)
    assert custody.identity(generated) != before
    vm["spec"]["template"]["spec"]["domain"]["devices"]["tpm"]["persistent"] = False
    assert not custody.persistent_state(pvc, vm)


def test_bmc_drains_before_vm_power_and_rejects_sidecar():
    root = resource("TroshkaProject", "project", spec={"topology": {"nodes": []}})
    vm = resource(
        "TroshkaVM",
        "vm",
        root,
        {"vmId": "12345678", "bmcEnabled": True, "bmcIp": "10.0.0.5"},
    )
    network = resource(
        "TroshkaNetwork",
        "bmc-net",
        root,
        {"networkType": "bmc"},
        {"nadName": "bmc-nad"},
    )
    deployment = build_bmc_deployment(
        "ns",
        "ns",
        [{"vmId": "12345678", "bmcIp": "10.0.0.5", "domainUuid": ""}],
        "bmc-nad",
        {},
    )
    deployment["metadata"].update(uid="bmc-uid", generation=1, resourceVersion="1")
    deployment["status"] = {"observedGeneration": 1}
    rs = resource("ReplicaSet", "bmc-rs", deployment, {"replicas": 1})
    pod = resource("Pod", "bmc-pod", rs)
    attest(root, [vm, network, deployment])
    by_uid = {v["metadata"]["uid"]: v for v in [root, vm, network, deployment, rs, pod]}
    assert custody.bmc_shape(deployment, root, by_uid)
    power.verify_chain(deployment, root, by_uid)
    changed = deepcopy(deployment)
    changed["spec"]["template"]["spec"]["containers"].append(
        {"name": "foreign", "image": "evil"}
    )
    assert not custody.bmc_shape(changed, root, by_uid)
    api = FakeAPI([root, vm, network, deployment, rs, pod])
    rows = {kind: api.rows(kind, "ns") for kind in power.INVENTORY}
    assert not power.drain_bmc(api, root, rows)
    assert [call[1] for call in api.calls] == [("Deployment", "bmc-ns")]
    rows["Pod"] = []
    rows["ReplicaSet"][0]["spec"]["replicas"] = 0
    stopped = api.get("Deployment", "ns", "bmc-ns")
    stopped["status"]["observedGeneration"] = stopped["metadata"]["generation"]
    rows["Deployment"] = [stopped]
    assert power.drain_bmc(api, root, rows)


def test_no_custody_annotation_keeps_ordinary_ownership_rule():
    root = resource("TroshkaProject", "project")
    role = build_vnc_rbac("ns")[0]
    role["metadata"]["uid"] = "role"
    with pytest.raises(power.ManagedPowerError, match="managed_ownership_unverified"):
        power.verify_chain(role, root, {root["metadata"]["uid"]: root})


@pytest.mark.parametrize("mutation", ["running", "duplicate", "namespace"])
def test_custody_marker_rejects_resume_and_ambiguous_identity(mutation):
    root = resource("TroshkaProject", "project")
    value = resource("ServiceAccount", "troshka-vnc")
    attest(root, [value])
    request = power.intent(root)
    if mutation == "running":
        request["mode"] = "running"
    elif mutation == "duplicate":
        request["cleanupCustody"].append(deepcopy(request["cleanupCustody"][0]))
    else:
        request["cleanupCustody"][0]["namespace"] = "foreign"
    root["metadata"]["annotations"][power.ANNOTATION] = json.dumps(request)
    with pytest.raises(power.ManagedPowerError):
        power.intent(root)


def test_ceph_real_manifests_are_read_only_and_active_export_is_rejected():
    from helpers.ceph_appliance import (
        build_export_job,
        build_mon_deployment,
        build_osd_deployment,
    )
    from helpers.topology import extract_ceph_cluster

    root = resource(
        "TroshkaProject",
        "project",
        spec={
            "topology": {
                "nodes": [
                    {
                        "id": "ceph",
                        "type": "cephClusterNode",
                        "data": {"labIp": "10.0.0.4", "osdCount": 3},
                    }
                ]
            }
        },
    )
    ceph = resource(
        "TroshkaCeph",
        "ceph",
        root,
        extract_ceph_cluster(root["spec"]["topology"]),
        {"phase": "Ready"},
    )
    deployments = [build_mon_deployment(ceph, "quay.io/ceph/ceph:v19")] + [
        build_osd_deployment(ceph, "quay.io/ceph/ceph:v19", i) for i in range(3)
    ]
    job = build_export_job(ceph)
    job["status"] = {"succeeded": 1}
    for value in deployments + [job]:
        value["metadata"].update(
            uid=value["metadata"]["name"] + "-uid", resourceVersion="1", generation=1
        )
    values = [root, ceph, *deployments, job]
    attest(root, values)
    rows = {kind: [v for v in values if v["kind"] == kind] for kind in power.INVENTORY}
    by_uid = {v["metadata"]["uid"]: v for v in values}
    before = deepcopy(values)
    custody.verify_ceph(root, rows, by_uid)
    assert values == before
    job["status"]["active"] = 1
    with pytest.raises(power.ManagedPowerError, match="custody_export_active"):
        custody.verify_ceph(root, rows, by_uid)
    job["status"].pop("active")
    deployments[0]["spec"]["template"]["spec"]["containers"][0]["command"] = ["foreign"]
    with pytest.raises(power.ManagedPowerError, match="custody_ceph_manifest_changed"):
        custody.verify_ceph(root, rows, by_uid)
