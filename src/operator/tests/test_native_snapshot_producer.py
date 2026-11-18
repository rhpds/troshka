"""Capture creation is fenced before POST and never resumes a name-only 409."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from helpers import managed_power as power
from helpers import native_origin_checkpoint as checkpoint

from tests.test_managed_power import FakeAPI, resource


@pytest.fixture
def held_capture(origin_capture, monkeypatch):
    import base64
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from handlers import project

    api, root = origin_capture
    key = Ed25519PrivateKey.generate()
    root['spec']['originCheckpoint']['publicKey'] = base64.b64encode(
        key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    root['metadata']['annotations'][project.CAPTURE_ANNOTATION] = json.dumps({'disks': [
        {'vmName': 'vm', 'vmId': 'vm', 'diskId': 'disk', 'pvcName': 'disk', 'sizeGb': 1, 's3Key': 'disk'}]})
    api.values['TroshkaProject', 'project'] = deepcopy(root)
    custom, core, batch = Mock(), Mock(), Mock()
    batch.list_namespaced_job.return_value = SimpleNamespace(items=[])
    monkeypatch.setattr(project.client, 'CustomObjectsApi', lambda: custom)
    monkeypatch.setattr(project.client, 'CoreV1Api', lambda: core)
    monkeypatch.setattr(project.client, 'BatchV1Api', lambda: batch)
    monkeypatch.setattr(project, '_setup_export_sa', lambda *args: None)
    monkeypatch.setattr(project, '_freeze_vmi', lambda *args: False)
    monkeypatch.setattr(project, '_wait_volume_snapshot_ready', AsyncMock(
        side_effect=checkpoint.CheckpointHold('test_snapshot_readiness')))

    def patch_status(custom_api, namespace, name, fields):
        api.values['TroshkaProject', 'project']['status'].update(deepcopy(fields))

    monkeypatch.setattr(project, '_patch_cr_status', patch_status)
    posts = []

    def post(**kwargs):
        value = deepcopy(kwargs['body'])
        posts.append(value)
        value['metadata'].update(uid='original-snapshot', resourceVersion='1')
        api.values['VolumeSnapshot', value['metadata']['name']] = value
        return value

    custom.create_namespaced_custom_object.side_effect = post

    def acknowledge():
        current = api.values['TroshkaProject', 'project']
        request = json.loads(current['metadata']['annotations'][checkpoint.REQUEST])
        ack = {'version': 1, 'intentId': request['intentId'], 'requestDigest': power.digest(request),
               'receiptId': 'receipt', 'receiptDigest': 'digest', 'predecessorId': None}
        ack['signature'] = base64.b64encode(key.sign(json.dumps(
            ack, sort_keys=True, separators=(',', ':')).encode())).decode()
        checkpoint.publish_ack(api, current, request, ack)

    def dispatch(handler='update', wrapped=False):
        import asyncio

        import kopf

        current = api.get('TroshkaProject', 'ns', 'project')
        entry = 'project_update' if handler == 'update' else 'project_managed_power_resume'
        decorator = kopf.on.update.return_value if handler == 'update' else kopf.on.resume.return_value
        target = next(c.args[0] for c in decorator.call_args_list
                      if c.args and getattr(c.args[0], '__name__', None) == entry)
        target = target if wrapped else target.__wrapped__
        if wrapped:
            monkeypatch.setattr(power, 'PowerClient', lambda: api)
        if handler == 'update':
            return asyncio.run(target(
                status=current['status'], meta=current['metadata'], namespace='ns', name='project', patch=Mock()))
        return asyncio.run(target(
            body=current, namespace='ns', patch=Mock()))

    return api, posts, acknowledge, dispatch, custom


@pytest.mark.parametrize('handler', ['update', 'resume'])
def test_held_capture_matching_ack_resumes_once_through_original_uid_fence(held_capture, handler):
    api, posts, acknowledge, dispatch, _ = held_capture
    with pytest.raises(checkpoint.CheckpointHold, match='intent_pending'):
        dispatch()
    assert api.values['TroshkaProject', 'project']['status']['phase'] == 'Capturing'
    assert posts == []
    acknowledge()
    for _ in range(2):
        with pytest.raises(checkpoint.CheckpointHold, match='test_snapshot_readiness'):
            dispatch(handler)
    assert len(posts) == 1


def test_resume_then_update_managed_wrappers_share_original_post_fence(held_capture, monkeypatch):
    from helpers import native_origin_drain as drain

    from tests.test_native_origin_drain import OPERATOR

    monkeypatch.setattr(drain, 'operator_identity', lambda api: deepcopy(OPERATOR))
    _, posts, acknowledge, dispatch, _ = held_capture
    with pytest.raises(checkpoint.CheckpointHold, match='intent_pending'):
        dispatch(wrapped=True)
    acknowledge()
    for handler in ['resume', 'update']:
        with pytest.raises(checkpoint.CheckpointHold, match='test_snapshot_readiness'):
            dispatch(handler, wrapped=True)
    assert len(posts) == 1


@pytest.mark.parametrize('handler', ['update', 'resume'])
def test_held_capture_without_matching_ack_does_not_post(held_capture, handler):
    _, posts, _, dispatch, _ = held_capture
    with pytest.raises(checkpoint.CheckpointHold, match='intent_pending'):
        dispatch()
    with pytest.raises(checkpoint.CheckpointHold, match='intent_pending'):
        dispatch(handler)
    assert posts == []


@pytest.mark.parametrize('handler', ['update', 'resume'])
def test_capture_resume_keeps_ambiguous_post_held(held_capture, handler):
    api, posts, acknowledge, dispatch, custom = held_capture
    with pytest.raises(checkpoint.CheckpointHold):
        dispatch()
    acknowledge()

    def lost(**kwargs):
        posts.append(deepcopy(kwargs['body']))
        raise TimeoutError('unknown POST outcome')

    custom.create_namespaced_custom_object.side_effect = lost
    with pytest.raises(checkpoint.CheckpointHold, match='post_outcome_unresolved'):
        dispatch(handler)
    saved = deepcopy(api.values['TroshkaProject', 'project']['status'])
    with pytest.raises(checkpoint.CheckpointHold, match='original_producer_disappeared'):
        dispatch(handler)
    assert len(posts) == 1
    assert api.values['TroshkaProject', 'project']['status']['originCreateEpochs'] == saved['originCreateEpochs']


@pytest.mark.parametrize('handler', ['update', 'resume'])
@pytest.mark.parametrize('phase,outcome', [
    ('Capturing', 'complete'), ('Capturing', 'failed'),
    ('CaptureComplete', 'complete'), ('CaptureError', 'failed')])
def test_completed_capture_resume_only_clears_retained_request(held_capture, monkeypatch, handler, phase, outcome):
    from handlers import project
    from helpers import native_snapshot_workflow as workflow

    api, posts, _, dispatch, _ = held_capture
    api.values['TroshkaProject', 'project']['status'].update(phase=phase, captureError='retained failure')
    workflow.begin('capture', [{'snapName': 'original-snapshot'}], outcome=outcome)
    removed = []
    monkeypatch.setattr(checkpoint, 'remove', lambda *args: removed.append(args))
    cleared = []
    monkeypatch.setattr(project, '_clear_capture_annotation', lambda *args: cleared.append(True))
    dispatch(handler)
    dispatch(handler)
    assert cleared == [True, True]
    assert removed == [('VolumeSnapshot', 'ns', 'original-snapshot', 'delete-VolumeSnapshot')] * 2
    assert posts == []
    assert api.values['TroshkaProject', 'project']['status']['phase'] == phase
    assert api.values['TroshkaProject', 'project']['status']['captureError'] == 'retained failure'


@pytest.mark.parametrize('handler', ['update', 'resume'])
@pytest.mark.parametrize('phase', ['CaptureComplete', 'CaptureError'])
@pytest.mark.parametrize('cleanup', ['missing', 'different-operation'])
def test_terminal_capture_checkpoint_alone_cannot_restart_producers(held_capture, handler, phase, cleanup):
    api, posts, acknowledge, dispatch, _ = held_capture
    with pytest.raises(checkpoint.CheckpointHold, match='intent_pending'):
        dispatch()
    acknowledge()
    current = api.values['TroshkaProject', 'project']
    current['status'].update(phase=phase, captureError='retained failure')
    if cleanup == 'different-operation':
        current['status']['originSnapshotCleanup'] = {
            'capture': {'operation': 'old-operation', 'resources': [], 'outcome': 'failed'}}
    dispatch(handler)
    dispatch(handler)
    assert posts == []
    assert current['status']['phase'] == phase
    assert current['status']['captureError'] == 'retained failure'


@pytest.mark.parametrize('handler', ['update', 'resume'])
@pytest.mark.parametrize('invalid', ['missing', 'changed-operation', 'legacy'])
def test_unrelated_capturing_root_is_not_restarted(held_capture, handler, invalid):
    api, posts, _, dispatch, _ = held_capture
    with pytest.raises(checkpoint.CheckpointHold):
        dispatch()
    current = api.values['TroshkaProject', 'project']
    if invalid == 'missing':
        current['metadata']['annotations'].pop(checkpoint.REQUEST)
    elif invalid == 'changed-operation':
        current['metadata']['annotations']['troshka.redhat.com/capture-request'] = '{"disks": []}'
    else:
        # participating() reads the serialized root context as well as the API.
        power._context.get()[0]['spec'].pop('originCheckpoint')
        current['spec'].pop('originCheckpoint')
    dispatch(handler)
    assert posts == []


@pytest.fixture
def origin_capture():
    root = resource("TroshkaProject", "project", spec={"originCheckpoint": {
        "version": 1, "intentId": "00000000-0000-4000-8000-000000000001", "publicKey": "key"}})
    root["metadata"].setdefault("annotations", {})["troshka.redhat.com/capture-request"] = "capture-1"
    api = FakeAPI([root, resource("Namespace", "ns"),
                   resource("PersistentVolumeClaim", "disk", root, spec={"volumeName": "pv"})])
    api.find = lambda kind, ns, name: deepcopy(api.values.get((kind, name)))
    tokens = power._context.set((root, "ns", api)), checkpoint._held.set(None)
    try:
        yield api, root
    finally:
        power._context.reset(tokens[0])
        checkpoint._held.reset(tokens[1])


def test_snapshot_creation_retains_intent_before_any_post(origin_capture):
    from helpers.patterns import build_volume_snapshot

    api, root = origin_capture
    raw = Mock()
    body = build_volume_snapshot("snap", "ns", "disk")
    with pytest.raises(checkpoint.CheckpointHold, match="pending"):
        checkpoint.guard_client(raw).create_namespaced_custom_object(group="snapshot.storage.k8s.io", version="v1",
            plural="volumesnapshots", namespace="ns", body=body)
    assert raw.method_calls == []
    import json

    request = json.loads(api.get("TroshkaProject", "ns", "project")["metadata"]["annotations"][checkpoint.REQUEST])
    assert request["action"] == "create-producer"
    assert request["producerIntent"]["role"] == "snapshot"
    assert request["producerIntent"]["source_pvc_name"] == "disk"
    assert body["metadata"]["ownerReferences"][0]["uid"] == root["metadata"]["uid"]


def test_origin_snapshot_timeout_is_not_readiness(origin_capture, monkeypatch):
    from handlers import project

    monkeypatch.setattr(project.asyncio, "sleep", __import__("unittest.mock").mock.AsyncMock())
    api = Mock()
    api.get_namespaced_custom_object.return_value = {"status": {"readyToUse": False}}
    with pytest.raises(checkpoint.CheckpointHold, match="readiness"):
        __import__("asyncio").run(project._wait_volume_snapshot_ready(api, "ns", "snapshot", 20))


def test_new_helper_cannot_request_a_prebound_pv(origin_capture):
    raw = Mock()
    with pytest.raises(checkpoint.CheckpointHold, match="prebound"):
        checkpoint.guard_client(raw).create_namespaced_persistent_volume_claim(namespace="ns", body={
            "kind": "PersistentVolumeClaim", "metadata": {"name": "helper"}, "spec": {"volumeName": "foreign-pv"}})
    assert not raw.method_calls


@pytest.mark.parametrize("lost", ["post-response", "uid-record"])
def test_submitted_snapshot_with_empty_readback_never_reposts_or_changes_epoch(origin_capture, monkeypatch, lost):
    from helpers import native_snapshot_producer as producer
    from helpers.patterns import build_volume_snapshot

    api, root = origin_capture
    body = build_volume_snapshot("snap", "ns", "disk")
    monkeypatch.setattr(checkpoint, "signature_valid", lambda *args: True)
    posts = []
    def post(**kwargs):
        submitted = api.get("TroshkaProject", "ns", "project")["status"]["originProducerObjects"]
        assert next(iter(submitted.values()))["outcome"] == "submitted"
        posts.append(deepcopy(kwargs["body"]))
        if lost == "post-response":
            raise TimeoutError("POST outcome unknown")
        value = deepcopy(kwargs["body"])
        value["metadata"].update(uid="original-uid", resourceVersion="1")
        api.values["VolumeSnapshot", "snap"] = value
        return value

    if lost == "uid-record":
        monkeypatch.setattr(producer, "_record", Mock(side_effect=TimeoutError("UID recording outcome unknown")))
    with pytest.raises(checkpoint.CheckpointHold):
        producer.create(post, {"namespace": "ns", "body": body})
    first = api.get("TroshkaProject", "ns", "project")["status"]
    api.values.pop(("VolumeSnapshot", "snap"), None)
    with pytest.raises(checkpoint.CheckpointHold):
        producer.create(post, {"namespace": "ns", "body": body})
    assert len(posts) == 1
    assert api.get("TroshkaProject", "ns", "project")["status"]["originCreateEpochs"] == first["originCreateEpochs"]


def test_original_creation_epoch_without_uid_or_outcome_record_still_blocks_post(origin_capture, monkeypatch):
    from helpers import native_snapshot_producer as producer
    from helpers.patterns import build_volume_snapshot

    api, root = origin_capture
    api.values["TroshkaProject", "project"]["status"] = {
        "originCreateEpochs": {power.digest(["VolumeSnapshot", "snap"]): "original-epoch"}}
    monkeypatch.setattr(checkpoint, "signature_valid", lambda *args: True)
    post = Mock()
    with pytest.raises(checkpoint.CheckpointHold, match="submitted_outcome_unresolved"):
        producer.create(post, {"namespace": "ns", "body": build_volume_snapshot("snap", "ns", "disk")})
    assert not post.called
    assert checkpoint.creation_epoch(api.get("TroshkaProject", "ns", "project"), "VolumeSnapshot", "snap") == "original-epoch"


@pytest.mark.parametrize("interrupted", ["Job", "PersistentVolumeClaim", "VolumeSnapshot"])
def test_cleanup_retry_stays_in_cleanup_phase_without_creating(origin_capture, monkeypatch, interrupted):
    from helpers import native_snapshot_workflow as workflow

    api, root = origin_capture
    resources = [{"jobName": "job", "tempPvcName": "restore", "scratchPvcName": "scratch", "snapName": "snap"}]
    workflow.begin("capture", resources)
    removed, stopped = [], False

    def remove(kind, namespace, name, reason):
        nonlocal stopped
        if kind == interrupted and not stopped:
            stopped = True
            raise checkpoint.CheckpointHold("interrupted")
        removed.append((kind, name))
        return True

    monkeypatch.setattr(checkpoint, "remove", remove)
    with pytest.raises(checkpoint.CheckpointHold):
        workflow.cleanup(resources)
    assert workflow.pending("capture") == resources
    workflow.cleanup(workflow.pending("capture"))
    assert removed[-1] == ("VolumeSnapshot", "snap")
    assert all(call[0] != "POST" for call in api.calls)


def test_cleanup_does_not_remove_storage_while_helper_pod_remains(origin_capture, monkeypatch):
    from helpers import native_snapshot_workflow as workflow

    api, _ = origin_capture
    pod = resource("Pod", "still-running", spec={"volumes": [{"persistentVolumeClaim": {"claimName": "restore"}}]})
    api.values["Pod", "still-running"] = pod
    removed = []
    monkeypatch.setattr(checkpoint, "remove", lambda *args: removed.append(args))
    with pytest.raises(checkpoint.CheckpointHold, match="writer"):
        workflow.cleanup([{"jobName": "job", "tempPvcName": "restore", "snapName": "snap"}])
    assert [r[0] for r in removed] == ["Job"]


@pytest.mark.parametrize("operation", ["capture", "filepull"])
@pytest.mark.parametrize("partial_failure", [False, True])
def test_real_handler_retry_resumes_persisted_cleanup_before_clearing_request(origin_capture, monkeypatch, operation, partial_failure):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from handlers import project
    from helpers import filepull
    from helpers import native_snapshot_workflow as workflow

    api, root = origin_capture
    annotation = "troshka.redhat.com/" + ("capture-request" if operation == "capture" else "file-pull-request")
    root["metadata"]["annotations"] = {annotation: "operation-1"}
    api.values["TroshkaProject", "project"] = deepcopy(root)
    custom, core, batch = Mock(), Mock(), Mock()
    batch.list_namespaced_job.return_value = SimpleNamespace(items=[])
    monkeypatch.setattr(project.client, "CustomObjectsApi", lambda: custom)
    monkeypatch.setattr(project.client, "CoreV1Api", lambda: core)
    monkeypatch.setattr(project.client, "BatchV1Api", lambda: batch)
    # Signed GuardedClient composition is covered by the backend lifecycle test;
    # this regression focuses the actual handler's durable retry/clear ordering.
    monkeypatch.setattr(project, "guard_client", lambda value: value)
    monkeypatch.setattr(project, "_setup_export_sa", Mock())
    monkeypatch.setattr(project, "_read_export_sizes", Mock())
    monkeypatch.setattr(project, "_freeze_vmi", lambda *args: False)
    monkeypatch.setattr(project, "_wait_volume_snapshot_ready", AsyncMock(return_value=1))
    monkeypatch.setattr(project, "_wait_filepull_pod", AsyncMock(return_value="fixture"))
    monkeypatch.setattr(filepull, "parse_filepull_output", lambda value: b"fixture")
    pipeline = AsyncMock(return_value=dict(jobName="job", tempPvcName="restore", snapName="snapshot",
        diskId="disk", vmId="vm", s3Key="key", format="qcow2", virtualSizeBytes=1))
    monkeypatch.setattr(project, "_snapshot_and_export_disk", pipeline)
    monkeypatch.setattr(project, "_poll_export_jobs", AsyncMock(return_value=None))
    if partial_failure:
        producer = dict(id="original", kind="VolumeSnapshot", role="snapshot", name="original-snapshot",
                        operation_digest=checkpoint.operation(root))
        api.values["TroshkaProject", "project"]["status"] = {
            "originProducerObjects": {"original": {"uid": "original-uid"}},
            "originCheckpointReceipts": {"request": {"request": {"producerIntent": producer}}}}
        pipeline.side_effect = RuntimeError("resolved snapshot; export input invalid")
        project._wait_volume_snapshot_ready.side_effect = RuntimeError("resolved snapshot; export input invalid")

    def status(**kwargs):
        api.values["TroshkaProject", "project"].setdefault("status", {}).update(kwargs["body"]["status"])

    def patch(**kwargs):
        assert workflow.pending(operation) is not None
        assert kwargs["body"]["metadata"]["annotations"] == {annotation: None}
        api.values["TroshkaProject", "project"]["metadata"]["annotations"].pop(annotation)

    custom.patch_namespaced_custom_object_status.side_effect = status
    custom.patch_namespaced_custom_object.side_effect = patch
    stopped = False

    def remove(*args):
        nonlocal stopped
        if partial_failure:
            assert args[:3] == ("VolumeSnapshot", "ns", "original-snapshot")
        assert workflow.pending(operation) is not None
        assert annotation in api.values["TroshkaProject", "project"]["metadata"]["annotations"]
        if not stopped:
            stopped = True
            raise checkpoint.CheckpointHold("delete acknowledgement pending")

    monkeypatch.setattr(checkpoint, "remove", remove)
    async def run():
        if operation == "capture":
            await project._handle_capture({"disks": [{"vmName": "vm", "diskId": "disk"}]}, "ns", "project", SimpleNamespace(status={}))
        else:
            await project._handle_file_pull({"requestId": "request", "pvcName": "disk"}, "ns", "project")

    with pytest.raises(checkpoint.CheckpointHold):
        asyncio.run(run())
    assert custom.patch_namespaced_custom_object.call_count == 0
    creates_before = len(core.method_calls)
    asyncio.run(run())
    assert annotation not in api.values["TroshkaProject", "project"]["metadata"]["annotations"]
    assert pipeline.await_count == (1 if operation == "capture" else 0)
    assert len(core.method_calls) == creates_before


def test_actual_capture_retry_preserves_failed_ceph_stage_and_never_enters_vm_stage(origin_capture, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from handlers import project
    from helpers import native_snapshot_workflow as workflow

    api, _ = origin_capture
    custom, core, batch = Mock(), Mock(), Mock()
    batch.list_namespaced_job.return_value = SimpleNamespace(items=[])
    monkeypatch.setattr(project.client, "CustomObjectsApi", lambda: custom)
    monkeypatch.setattr(project.client, "CoreV1Api", lambda: core)
    monkeypatch.setattr(project.client, "BatchV1Api", lambda: batch)
    monkeypatch.setattr(project, "guard_client", lambda value: value)
    monkeypatch.setattr(project, "_setup_export_sa", Mock())
    monkeypatch.setattr(project, "_patch_ceph_capture_progress", Mock())
    monkeypatch.setattr(project, "_read_export_sizes", Mock())
    ceph = AsyncMock(side_effect=RuntimeError("Ceph export failed"))
    vm = AsyncMock(side_effect=AssertionError("VM stage must not run"))
    monkeypatch.setattr(project, "_run_ceph_device_pipeline", ceph)
    monkeypatch.setattr(project, "_snapshot_and_export_disk", vm)
    monkeypatch.setattr(workflow, "recorded_resources", lambda: [{"snapName": "ceph-original"}])
    def status(**kwargs):
        api.values["TroshkaProject", "project"].setdefault("status", {}).update(kwargs["body"]["status"])
    custom.patch_namespaced_custom_object_status.side_effect = status
    remove = Mock(side_effect=[checkpoint.CheckpointHold("waiting for cleanup ack"), None, None, None])
    monkeypatch.setattr(checkpoint, "remove", remove)
    config = {"cephDevices": [{"kind": "mon", "index": 0}], "disks": [{"vmName": "vm", "diskId": "disk"}]}
    with pytest.raises(checkpoint.CheckpointHold):
        asyncio.run(project._handle_capture(config, "ns", "project", SimpleNamespace(status={})))
    assert not custom.patch_namespaced_custom_object.called
    asyncio.run(project._handle_capture(config, "ns", "project", SimpleNamespace(status={})))
    assert ceph.await_count == 1
    assert vm.await_count == 0
    assert api.values["TroshkaProject", "project"]["status"]["phase"] == "CaptureError"
    assert custom.patch_namespaced_custom_object.call_count == 1
