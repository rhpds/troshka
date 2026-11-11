import os
import sys

import pytest

from app.schemas.kubevirt_storage import CollectionLimits
from app.services.kubevirt_storage_transport import (
    BoundedProcess,
    ToolboxConfig,
    ToolboxTransport,
    command_argv,
    decode_csi_handle,
    parse_usage,
)


def test_fixed_commands_reject_injection_and_mutations():
    assert command_argv("fsid") == ["ceph", "fsid"]
    with pytest.raises(ValueError):
        command_argv("delete", pool="pool", image="a")
    with pytest.raises(ValueError):
        command_argv("info", pool="pool;id", image="abc")
    assert (
        command_argv(
            "journal",
            pool="pool",
            object_uuid="00000000-0000-4000-8000-000000000001",
            journal_key="csi.imageid",
        )[-1]
        == "-"
    )


def test_decoder_uses_length_and_pool_id():
    assert decode_csi_handle(
        "0001-0004-rook-0000000000000001-00000000-0000-4000-8000-000000000001"
    ) == ("rook", 1, "00000000-0000-4000-8000-000000000001")
    with pytest.raises(ValueError):
        decode_csi_handle(
            "0001-0005-rook-0000000000000001-00000000-0000-4000-8000-000000000001"
        )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"images": []},
        {"images": [{"id": "abc", "used_size": -1, "provisioned_size": 10}]},
        {"images": [{"id": "other", "used_size": 0, "provisioned_size": 10}]},
        {
            "images": [
                {"id": "abc", "used_size": 0, "provisioned_size": 10},
                {"id": "abc", "used_size": 5, "provisioned_size": 10},
            ]
        },
    ],
)
def test_du_rejects_incomplete_or_ambiguous_output(payload):
    with pytest.raises(ValueError):
        parse_usage(payload, "abc")


def test_du_zero_is_success():
    assert parse_usage(
        {"images": [{"id": "abc", "used_size": 0, "provisioned_size": 10}]}, "abc"
    ) == (0, 10)


def test_bounded_process_sanitizes_failure_and_caps_both_streams():
    process = BoundedProcess(CollectionLimits(max_output_bytes=1024))
    result = process.run(
        [
            sys.executable,
            "-c",
            "import sys;sys.stderr.write('SECRET'*1000);sys.stdout.write('x'*100000)",
        ]
    )
    assert result.status == "output_limit"
    assert "SECRET" not in result.model_dump_json()


def test_process_timeout_kills_descendants(tmp_path):
    pid_file = tmp_path / "pid"
    code = (
        "import subprocess,time,pathlib; p=subprocess.Popen(['"
        + sys.executable
        + "','-c','import time;time.sleep(30)']);pathlib.Path("
        + repr(str(pid_file))
        + ").write_text(str(p.pid));time.sleep(30)"
    )
    result = BoundedProcess(CollectionLimits(command_timeout_seconds=1)).run(
        [sys.executable, "-c", code]
    )
    assert result.status == "timeout"
    pid = int(pid_file.read_text())
    # macOS reaps killed orphans; Linux may briefly expose a harmless zombie.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    from pathlib import Path

    status = Path(f"/proc/{pid}/stat")
    assert status.exists() and status.read_text().split()[2] == "Z"


def test_toolbox_pins_identity_and_exact_remote_argv(monkeypatch):
    from app.schemas.kubevirt_storage import ReadResult
    from tests.test_kubevirt_storage_graph import NOW

    class Kube:
        def cluster_identity(self):
            return "cluster"

        def pod(self, ns, name):
            return {
                "metadata": {"uid": "pod-uid"},
                "spec": {"containers": [{"name": "rook-ceph-tools"}]},
            }

    calls = []

    def run(self, argv, **kw):
        calls.append((argv, kw))
        return ReadResult(
            status="complete",
            value="fsid" if argv[-2:] == ["ceph", "fsid"] else "{}",
            collected_at=NOW,
        )

    monkeypatch.setattr(BoundedProcess, "run", run)
    config = ToolboxConfig(
        cluster_key="cluster",
        namespace="openshift-storage",
        pod_name="rook-ceph-tools-test",
        pod_uid="pod-uid",
        container="rook-ceph-tools",
        ceph_fsid="fsid",
        allowed_pools=["pool"],
    )
    kube = Kube()
    transport = ToolboxTransport(
        CollectionLimits(), config, kube, "/private/tmp/test-kubeconfig"
    )
    transport.run("info", pool="pool", image="abc")
    assert sum(argv[-2:] == ["ceph", "fsid"] for argv, _ in calls) == 1
    assert calls[-1][0] == [
        "oc",
        "--kubeconfig=/private/tmp/test-kubeconfig",
        "exec",
        "-n",
        "openshift-storage",
        "rook-ceph-tools-test",
        "-c",
        "rook-ceph-tools",
        "--",
        "timeout",
        "--kill-after=2s",
        "15s",
        "rbd",
        "info",
        "--pool",
        "pool",
        "--namespace",
        "",
        "--image",
        "abc",
        "--format",
        "json",
    ]
    assert calls[-1][1]["timeout"] == 20
    kube.cluster_identity = lambda: "replacement"
    assert transport.run("info", pool="pool", image="abc").status == "identity_changed"
    with pytest.raises(ValueError):
        ToolboxTransport(
            CollectionLimits(),
            config.model_copy(update={"namespace": "tenant"}),
            kube,
            "/private/tmp/config",
        )


def test_csi_binding_requires_journal_request_name_and_immutable_id():
    import json

    from app.schemas.kubevirt_storage import ReadResult
    from app.services.kubevirt_storage_transport import (
        CapabilityProfile,
        PoolConfig,
        RbdReader,
    )
    from tests.test_kubevirt_storage_graph import NOW

    class Transport:
        journal = {
            "csi.imageid": "abcdef123456",
            "csi.imagename": "csi-vol-00000000-0000-4000-8000-000000000001",
            "csi.volname": "pvc-original",
        }
        image_id = "abcdef123456"

        def run(self, op, **kwargs):
            if op == "fsid":
                return ReadResult(status="complete", value="fsid", collected_at=NOW)
            if op == "pools":
                return ReadResult(
                    status="complete",
                    value=json.dumps([{"poolname": "pool", "poolnum": 1}]),
                    collected_at=NOW,
                )
            value = (
                self.journal[kwargs["journal_key"]]
                if op == "journal"
                else json.dumps({"id": self.image_id})
            )
            return ReadResult(status="complete", value=value, collected_at=NOW)

    transport = Transport()
    reader = RbdReader(
        transport,
        "fsid",
        [PoolConfig(pool_id=1, name="pool", csi_cluster_id="rook")],
        CapabilityProfile(command_version="test"),
    )
    handle = "0001-0004-rook-0000000000000001-00000000-0000-4000-8000-000000000001"
    assert reader.resolve_csi("rook.rbd.csi.ceph.com", handle).status == "unsupported"
    assert (
        reader.resolve_csi(
            "rook.rbd.csi.ceph.com", handle, expected_request="pvc-current"
        ).status
        == "identity_changed"
    )
    assert (
        reader.resolve_csi(
            "rook.rbd.csi.ceph.com", handle, expected_request="pvc-original"
        ).value.image_id
        == "abcdef123456"
    )
    transport.image_id = "replacement"
    assert (
        reader.resolve_csi(
            "rook.rbd.csi.ceph.com", handle, expected_request="pvc-original"
        ).status
        == "identity_changed"
    )


def test_exit_124_is_typed_timeout_without_stdout():
    result = BoundedProcess(CollectionLimits()).run(
        [sys.executable, "-c", "import sys;print('SECRET');sys.exit(124)"]
    )
    assert result.status == "timeout"
    assert result.value is None


def test_wrong_live_pool_id_blocks_csi_before_journal_reads():
    import json

    from app.schemas.kubevirt_storage import ReadResult
    from app.services.kubevirt_storage_transport import (
        CapabilityProfile,
        PoolConfig,
        RbdReader,
    )
    from tests.test_kubevirt_storage_graph import NOW

    class Transport:
        def __init__(self):
            self.calls = []

        def run(self, op, **_kwargs):
            self.calls.append(op)
            value = (
                "fsid"
                if op == "fsid"
                else json.dumps([{"poolname": "pool", "poolnum": 99}])
            )
            return ReadResult(status="complete", value=value, collected_at=NOW)

    transport = Transport()
    reader = RbdReader(
        transport,
        "fsid",
        [PoolConfig(pool_id=1, name="pool", csi_cluster_id="rook")],
        CapabilityProfile(command_version="test"),
    )
    handle = "0001-0004-rook-0000000000000001-00000000-0000-4000-8000-000000000001"
    result = reader.resolve_csi(
        "rook.rbd.csi.ceph.com", handle, expected_request="pvc-original"
    )
    assert result.status == "identity_changed"
    assert transport.calls == ["fsid", "pools"]
