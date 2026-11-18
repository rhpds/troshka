"""Origin-aware storage removal must escape best-effort cleanup handlers."""

from copy import deepcopy

import pytest


def test_exception_boundary_propagates_only_checkpoint_holds():
    from helpers import native_origin_checkpoint as checkpoint

    checkpoint.propagate_hold(RuntimeError("ordinary best-effort failure"))
    held = checkpoint.CheckpointHold("pending")
    with pytest.raises(checkpoint.CheckpointHold) as raised:
        checkpoint.propagate_hold(held)
    assert raised.value is held


@pytest.mark.parametrize("method,kwargs", [
    ("delete_namespaced_persistent_volume_claim", {"name": "disk", "namespace": "ns"}),
    ("delete_namespaced_custom_object", {"group": "cdi.kubevirt.io", "version": "v1beta1", "plural": "datavolumes", "name": "disk", "namespace": "ns"}),
    ("patch_namespaced_persistent_volume_claim", {"name": "disk", "namespace": "ns", "body": {"metadata": {"finalizers": None}}}),
])
def test_guarded_client_holds_deletes_and_finalizer_stripping(monkeypatch, method, kwargs):
    from unittest.mock import MagicMock

    from helpers import managed_power as power
    from helpers import native_origin_checkpoint as checkpoint

    from tests.test_managed_power import FakeAPI, resource

    root = {"spec": {"originCheckpoint": {"intentId": "intent"}}, "metadata": {"namespace": "ns"}}
    token = power._context.set((root, "ns", FakeAPI([resource("Namespace", "ns")])))
    raw = MagicMock()

    def hold(*args):
        raise checkpoint.CheckpointHold("held")

    monkeypatch.setattr(checkpoint, "remove", hold)
    try:
        with pytest.raises(checkpoint.CheckpointHold):
            getattr(checkpoint.guard_client(raw), method)(**kwargs)
        assert not raw.method_calls
    finally:
        power._context.reset(token)


def test_clone_retry_holds_without_signed_checkpoint(monkeypatch):
    from helpers import managed_power as power
    from helpers import native_origin_checkpoint as checkpoint

    from tests.test_managed_power import FakeAPI, resource

    root = resource("TroshkaProject", "project", spec={"originCheckpoint": {"version": 1, "intentId": "intent", "publicKey": "key"}})
    pvc = resource("PersistentVolumeClaim", "disk", root, spec={"volumeName": "pv"})
    dv = resource("DataVolume", "disk", root)
    namespace = resource("Namespace", "ns")
    api = FakeAPI([root, pvc, dv, namespace])
    api.find = api.get
    token = power._context.set((root, "ns", api))
    try:
        with pytest.raises(checkpoint.CheckpointHold):
            checkpoint.remove("DataVolume", "ns", "disk", "clone-retry")
        assert not any(call[0] == "DELETE" for call in api.calls)
        assert api.values["TroshkaProject", "project"]["metadata"]["annotations"][checkpoint.REQUEST]
    finally:
        power._context.reset(token)


def test_swallowed_hold_still_blocks_following_replacement(monkeypatch):
    from unittest.mock import MagicMock

    from helpers import managed_power as power
    from helpers import native_origin_checkpoint as checkpoint

    from tests.test_managed_power import FakeAPI, resource

    root = {"spec": {"originCheckpoint": {}}, "metadata": {"namespace": "ns"}}
    context = power._context.set((root, "ns", FakeAPI([resource("Namespace", "ns")])))
    held = checkpoint._held.set(None)
    raw = MagicMock()
    api = checkpoint.guard_client(raw)
    monkeypatch.setattr(checkpoint, "remove", lambda *args: (_ for _ in ()).throw(checkpoint.CheckpointHold("held")))
    try:
        try:
            api.delete_namespaced_persistent_volume_claim(namespace="ns", name="disk")
        except Exception:  # noqa: BLE001, S110 -- deliberately model a legacy best-effort caller
            pass
        with pytest.raises(checkpoint.CheckpointHold):
            api.create_namespaced_persistent_volume_claim(namespace="ns", body={})
        assert not raw.method_calls
    finally:
        checkpoint._held.reset(held)
        power._context.reset(context)


def test_replacement_creation_invalidates_old_absence_authority():
    from helpers import native_origin_checkpoint as checkpoint

    from tests.test_managed_power import FakeAPI, resource

    root = resource("TroshkaProject", "project", spec={"originCheckpoint": {"intentId": "intent"}})
    api = FakeAPI([root, resource("Namespace", "ns")])
    api.find = lambda *args: None
    before = deepcopy(root)
    checkpoint.begin_create(api, root, "PersistentVolumeClaim", "ns", "disk")
    after = api.get("TroshkaProject", "ns", "project")
    assert checkpoint.creation_epoch(before, "PersistentVolumeClaim", "disk") != checkpoint.creation_epoch(after, "PersistentVolumeClaim", "disk")


@pytest.fixture
def signed_case():
    import base64
    import json

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from helpers import managed_power as power
    from helpers import native_origin_checkpoint as checkpoint

    from tests.test_managed_power import FakeAPI, resource

    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    root = resource("TroshkaProject", "project", spec={"originCheckpoint": {"version": 1, "intentId": "intent", "publicKey": public}})
    pvc = resource("PersistentVolumeClaim", "disk", root, spec={"volumeName": "pv"})
    api = FakeAPI([root, pvc, resource("Namespace", "ns")])
    api.find = lambda kind, ns, name: deepcopy(api.values.get((kind, name)))
    request = checkpoint.build_request(api, root, "ns", pvc, "delete-PersistentVolumeClaim")
    ack = {"version": 1, "intentId": "intent", "requestDigest": power.digest(request),
           "receiptId": "committed", "receiptDigest": "digest", "predecessorId": None}
    ack["signature"] = base64.b64encode(key.sign(json.dumps(ack, sort_keys=True, separators=(",", ":")).encode())).decode()
    root["metadata"]["annotations"] = {checkpoint.REQUEST: json.dumps(request), checkpoint.ACK: json.dumps(ack)}
    root["status"]["originCheckpointReceipts"] = {request["requestId"]: {"request": request, "ack": ack}}
    api.values["TroshkaProject", "project"] = deepcopy(root)
    context = power._context.set((root, "ns", api))
    held = checkpoint._held.set(None)
    try:
        yield api, root, request
    finally:
        checkpoint._held.reset(held)
        power._context.reset(context)


def test_valid_ack_uses_exact_uid_and_resource_version(signed_case):
    from helpers import native_origin_checkpoint as checkpoint

    api, _, _ = signed_case
    deleted = []

    def delete(method, kind, namespace, name, body):
        assert body["preconditions"] == {"uid": "disk-uid", "resourceVersion": "1"}
        deleted.append(api.values.pop((kind, name)))

    api.request = delete
    assert checkpoint.remove("PersistentVolumeClaim", "ns", "disk", "delete-PersistentVolumeClaim")
    assert len(deleted) == 1
    # A restart/retry uses persisted signed history, not the in-memory ack.
    assert checkpoint.remove("PersistentVolumeClaim", "ns", "disk", "delete-PersistentVolumeClaim")


@pytest.mark.parametrize("field,value", [("uid", "replacement"), ("resourceVersion", "2")])
def test_same_name_replacement_or_binding_race_holds(signed_case, field, value):
    from helpers import native_origin_checkpoint as checkpoint

    api, _, _ = signed_case
    api.values["PersistentVolumeClaim", "disk"]["metadata"][field] = value
    with pytest.raises(checkpoint.CheckpointHold):
        checkpoint.remove("PersistentVolumeClaim", "ns", "disk", "delete-PersistentVolumeClaim")
    assert not any(c[0] == "DELETE" for c in api.calls)


def test_old_absence_ack_cannot_cover_new_creation(signed_case):
    from helpers import native_origin_checkpoint as checkpoint

    api, root, _ = signed_case
    api.values.pop(("PersistentVolumeClaim", "disk"))
    checkpoint.begin_create(api, root, "PersistentVolumeClaim", "ns", "disk")
    with pytest.raises(checkpoint.CheckpointHold, match="unresolved"):
        checkpoint.remove("PersistentVolumeClaim", "ns", "disk", "delete-PersistentVolumeClaim")


def test_ack_publication_preserves_status_and_uses_status_subresource(signed_case):
    from helpers import native_origin_checkpoint as checkpoint

    api, root, request = signed_case
    root["status"]["phase"] = "Running"
    root["status"]["existingHandler"] = {"progress": 42}
    ack = root["status"]["originCheckpointReceipts"][request["requestId"]]["ack"]
    original = api.patch
    patches = []

    def patch(value, operations, *, status=False):
        patches.append(status)
        return original(value, operations, status=status)

    api.patch = patch
    checkpoint.publish_ack(api, root, request, ack)
    current = api.get("TroshkaProject", "ns", "project")
    assert current["status"]["phase"] == "Running"
    assert current["status"]["existingHandler"] == {"progress": 42}
    assert patches == [True, False]


def test_origin_coordination_status_schema_and_scoped_rbac():
    from pathlib import Path

    import yaml

    root = Path(__file__).parents[1]
    schema = yaml.safe_load((root / "crds/troshkaproject.yaml").read_text())
    version = schema["spec"]["versions"][0]
    assert "status" in version["subresources"]
    assert version["schema"]["openAPIV3Schema"]["properties"]["status"]["x-kubernetes-preserve-unknown-fields"]
    for path in [root / "deploy/clusterrole.yaml", root.parents[1] / "infra/ocpvirt-rbac.yaml"]:
        documents = list(yaml.safe_load_all(path.read_text()))
        assert any("troshkaprojects/status" in rule.get("resources", [])
                   and set(rule.get("verbs", [])) & {"patch", "*"}
                   for doc in documents if doc for rule in doc.get("rules", []))


@pytest.fixture
def blank_case(monkeypatch):
    import base64
    import json
    from unittest.mock import MagicMock

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from helpers import managed_power as power
    from helpers import native_origin_checkpoint as checkpoint

    from tests.test_managed_power import FakeAPI, resource

    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    root = resource("TroshkaProject", "project", spec={"originCheckpoint": {"version": 1, "intentId": "intent", "publicKey": public}})
    child = resource("TroshkaVM", "vm", root, spec={"disks": [{"id": "disk", "blank": True}]})
    pvc = resource("PersistentVolumeClaim", "vm-disk-disk", child, spec={"volumeName": "pv"})
    root["status"]["originCreateEpochs"] = {power.digest(["PersistentVolumeClaim", pvc["metadata"]["name"]]): "pvc-created"}
    kv = resource("VirtualMachine", "troshka-vm", child)
    api = FakeAPI([root, child, pvc, kv, resource("Namespace", "ns")])
    api.find = lambda kind, ns, name: deepcopy(api.values.get((kind, name)))
    root["status"]["originCheckpointReceipts"] = {}
    for target in [kv, pvc]:
        request = checkpoint.build_request(api, root, "ns", target, "delete-" + target["kind"], child)
        ack = {"version": 1, "intentId": "intent", "requestDigest": power.digest(request),
               "receiptId": "committed", "receiptDigest": "digest", "predecessorId": None}
        ack["signature"] = base64.b64encode(key.sign(json.dumps(ack, sort_keys=True, separators=(",", ":")).encode())).decode()
        root["status"]["originCheckpointReceipts"][request["requestId"]] = {"request": request, "ack": ack}
        root["metadata"]["annotations"] = {checkpoint.REQUEST: json.dumps(request), checkpoint.ACK: json.dumps(ack)}
    api.values["TroshkaProject", "project"] = deepcopy(root)
    api.values.pop(("VirtualMachine", "troshka-vm"))
    deleted = []

    def delete(method, kind, namespace, name, body):
        value = api.values[kind, name]
        assert body["preconditions"] == {k: value["metadata"][k] for k in ("uid", "resourceVersion")}
        deleted.append((kind, name))
        api.values.pop((kind, name))

    api.request = delete
    monkeypatch.setattr(power, "PowerClient", lambda: api)
    context = power._context.set((root, "ns", api))
    producer = power._producer_body.set(child)
    held = checkpoint._held.set(None)
    try:
        yield api, root, child, pvc, deleted, MagicMock()
    finally:
        checkpoint._held.reset(held)
        power._producer_body.reset(producer)
        power._context.reset(context)


def test_blank_disk_real_removal_reaches_signed_plain_pvc(blank_case):
    from handlers import vm
    from helpers import native_origin_checkpoint as checkpoint

    _, _, _, pvc, deleted, raw = blank_case
    vm._delete_removed_disks({"disk": {"blank": True}}, {}, "vm", "ns",
                             checkpoint.guard_client(raw), checkpoint.guard_client(raw))
    assert deleted == [("PersistentVolumeClaim", pvc["metadata"]["name"])]
    assert not raw.method_calls
    # Finalizer/reconfigure retry after the PVC disappeared must also finish.
    vm._delete_removed_disks({"disk": {"blank": True}}, {}, "vm", "ns",
                             checkpoint.guard_client(raw), checkpoint.guard_client(raw))


def test_plain_pvc_still_requires_signed_checkpoint(blank_case):
    from handlers import vm
    from helpers import native_origin_checkpoint as checkpoint

    api, _, _, _, deleted, raw = blank_case
    api.values["TroshkaProject", "project"]["metadata"]["annotations"].pop(checkpoint.ACK)
    with pytest.raises(checkpoint.CheckpointHold, match="pending"):
        vm._delete_removed_disks({"disk": {"blank": True}}, {}, "vm", "ns",
                                 checkpoint.guard_client(raw), checkpoint.guard_client(raw))
    assert not deleted and not raw.method_calls


def test_new_child_marker_precedes_operator_post(blank_case):
    from helpers import native_origin_checkpoint as checkpoint

    _, _, child, _, _, raw = blank_case
    body = deepcopy(child)
    body["metadata"]["name"] = "new-child"
    original_spec = deepcopy(body["spec"])
    original_owners = deepcopy(body["metadata"]["ownerReferences"])

    def post(**kwargs):
        assert kwargs["body"]["spec"] == {**original_spec, checkpoint.PARTICIPATION: "intent"}
        assert kwargs["body"]["metadata"]["ownerReferences"] == original_owners

    raw.create_namespaced_custom_object.side_effect = post
    checkpoint.guard_client(raw).create_namespaced_custom_object(
        group="troshka.redhat.com", version="v1alpha1", plural="troshkavms", namespace="ns", body=body
    )
    raw.create_namespaced_custom_object.assert_called_once()


def test_participation_schema_is_immutable_optional_and_creation_only():
    from pathlib import Path

    import yaml
    from helpers import native_origin_checkpoint as checkpoint

    path = Path(__file__).parents[1] / "crds/troshkavm.yaml"
    spec = yaml.safe_load(path.read_text())["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]
    field = checkpoint.PARTICIPATION
    assert field not in spec["required"]
    assert spec["properties"][field]["format"] == "uuid"
    assert {"self == oldSelf"} == {r["rule"] for r in spec["properties"][field]["x-kubernetes-validations"]}
    assert f"has(self.{field}) == has(oldSelf.{field})" in {r["rule"] for r in spec["x-kubernetes-validations"]}
    legacy = {"kind": "TroshkaVM", "metadata": {}, "spec": {"cpus": 2}}
    expected = deepcopy(legacy)
    checkpoint.mark_child({"spec": {}}, legacy)
    assert legacy == expected


@pytest.mark.parametrize("ambiguity", ["dv_owner", "dv_epoch", "cdi", "unrecorded"])
def test_absent_dv_with_ambiguous_original_still_holds(blank_case, ambiguity):
    from handlers import vm
    from helpers import managed_power as power
    from helpers import native_origin_checkpoint as checkpoint

    api, _, _, pvc, deleted, raw = blank_case
    name = pvc["metadata"]["name"]
    if ambiguity == "dv_owner":
        api.values["PersistentVolumeClaim", name]["metadata"]["ownerReferences"][0]["kind"] = "DataVolume"
    elif ambiguity == "dv_epoch":
        api.values["TroshkaProject", "project"]["status"]["originCreateEpochs"][power.digest(["DataVolume", name])] = "original-dv"
    elif ambiguity == "cdi":
        api.values["PersistentVolumeClaim", name]["metadata"]["annotations"] = {"cdi.kubevirt.io/storage.import.endpoint": "source"}
    else:
        api.values["TroshkaProject", "project"]["status"]["originCreateEpochs"] = {}
    with pytest.raises(checkpoint.CheckpointHold):
        vm._delete_removed_disks({"disk": {"blank": True}}, {}, "vm", "ns",
                                 checkpoint.guard_client(raw), checkpoint.guard_client(raw))
    assert not deleted and not raw.method_calls


def test_blank_disk_real_finalizer_uses_live_context(blank_case, monkeypatch):
    import asyncio

    from handlers import vm

    from tests.test_operator_handlers import TestVmDeleteHandler

    _, _, child, pvc, deleted, raw = blank_case
    monkeypatch.setattr(vm.client, "CoreV1Api", lambda: raw)
    monkeypatch.setattr(vm.client, "CustomObjectsApi", lambda: raw)
    asyncio.run(TestVmDeleteHandler._get_vm_delete_fn()(
        spec=child["spec"], status={}, meta=child["metadata"], namespace="ns", name="vm"
    ))
    assert deleted == [("PersistentVolumeClaim", pvc["metadata"]["name"])]
    raw.delete_namespaced_secret.assert_called_once()


@pytest.mark.parametrize("parent_state", ["deleting", "absent"])
def test_legacy_real_finalizer_allows_parent_gc(monkeypatch, parent_state):
    import asyncio
    from unittest.mock import MagicMock

    from handlers import vm
    from helpers import managed_power as power

    from tests.test_managed_power import FakeAPI, resource
    from tests.test_operator_handlers import TestVmDeleteHandler

    root = resource("TroshkaProject", "project")
    root["metadata"]["deletionTimestamp"] = "now"
    child = resource("TroshkaVM", "vm", root)
    values = [child, resource("Namespace", "ns")]
    if parent_state != "absent":
        values.append(root)
    api = FakeAPI(values)
    api.find = lambda kind, ns, name: deepcopy(api.values.get((kind, name)))
    monkeypatch.setattr(power, "PowerClient", lambda: api)
    raw = MagicMock()
    monkeypatch.setattr(vm.client, "CoreV1Api", lambda: raw)
    monkeypatch.setattr(vm.client, "CustomObjectsApi", lambda: raw)
    asyncio.run(TestVmDeleteHandler._get_vm_delete_fn()(
        spec={"disks": [{"id": "disk", "blank": True}]}, status={},
        meta=child["metadata"], namespace="ns", name="vm"
    ))
    raw.delete_namespaced_persistent_volume_claim.assert_called_once()


@pytest.mark.parametrize("evidence", ["child_marker", "namespace_marker", "deleting_root", "read_error", "wrong_root"])
def test_unresolved_origin_finalizer_still_holds(blank_case, evidence):
    from helpers import native_origin_checkpoint as checkpoint

    api, _, child, _, deleted, raw = blank_case
    if evidence == "child_marker":
        child["spec"][checkpoint.PARTICIPATION] = "intent"
        api.values.pop(("TroshkaProject", "project"))
    elif evidence == "namespace_marker":
        api.values["Namespace", "ns"]["metadata"]["annotations"] = {"troshka.redhat.com/origin-intent": "intent"}
        api.values.pop(("TroshkaProject", "project"))
    elif evidence == "deleting_root":
        api.values["TroshkaProject", "project"]["metadata"]["deletionTimestamp"] = "now"
    elif evidence == "wrong_root":
        api.values["TroshkaProject", "project"]["metadata"]["uid"] = "replacement"
    else:
        api.find = lambda *args: (_ for _ in ()).throw(checkpoint.CheckpointHold("unavailable"))
    with pytest.raises((checkpoint.CheckpointHold, ValueError)), checkpoint.finalizer(child["metadata"], "ns", child["spec"]):
        raw.delete_namespaced_persistent_volume_claim(namespace="ns", name="disk")
    assert not raw.method_calls and not deleted
