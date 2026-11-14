"""Legacy custody never confers normal admission or name-only deletion rights."""

import json
from copy import deepcopy

import pytest
from helpers import managed_power as power
from helpers import native_custody as custody
from helpers.bmc import build_bmc_deployment
from helpers.vnc import build_vnc_rbac

from tests.test_managed_power import FakeAPI, resource


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
