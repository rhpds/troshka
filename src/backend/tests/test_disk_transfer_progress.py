"""Actual importer download size, rather than virtual disk capacity."""

import base64
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def transfer(monkeypatch):
    from app.services import disk_transfer_progress as module

    cache = {}
    now = [100.0]
    monkeypatch.setattr(module, "get_progress", lambda key: cache.get(key))
    monkeypatch.setattr(
        module, "set_progress", lambda key, value, **kw: cache.__setitem__(key, value)
    )
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    core = MagicMock()
    core.read_namespaced_persistent_volume_claim.return_value = NS(
        metadata=NS(uid="pvc-uid")
    )
    core.read_namespaced_secret.return_value = NS(
        data={
            "accessKeyId": base64.b64encode(b"access").decode(),
            "secretKey": base64.b64encode(b"private").decode(),
        }
    )
    pod = NS(
        metadata=NS(name="importer-prime", uid="importer-uid"),
        status=NS(phase="Running"),
        spec=NS(
            volumes=[NS(persistent_volume_claim=NS(claim_name="prime-pvc-uid"))],
            containers=[NS(name="importer")],
        ),
    )
    core.list_namespaced_pod.return_value = NS(items=[pod])
    client = MagicMock()
    client.head_object.return_value = {"ContentLength": 100_000_000}
    create_client = MagicMock(return_value=client)
    monkeypatch.setattr(module.boto3, "client", create_client)
    execute = MagicMock(return_value="25000000\n")
    monkeypatch.setattr(module, "stream", execute)
    dv = {
        "metadata": {"name": "disk", "namespace": "project-ns", "uid": "dv-uid"},
        "spec": {
            "source": {
                "s3": {
                    "url": "https://s3.example.test/bucket/disk.qcow2",
                    "secretRef": "s3-secret",
                }
            },
            "storage": {"resources": {"requests": {"storage": "500Gi"}}},
        },
        "status": {"phase": "ImportInProgress", "progress": "N/A"},
    }
    return NS(
        module=module,
        core=core,
        dv=dv,
        cache=cache,
        now=now,
        client=client,
        create_client=create_client,
        execute=execute,
        pod=pod,
    )


def test_first_sample_uses_object_size_and_only_stats_scratch(transfer):
    t = transfer
    result = t.module.get_s3_download_progress(t.core, t.dv, "provider")
    assert result == "downloading 25.0% — 25.0 / 100.0 MB"
    t.client.head_object.assert_called_once_with(Bucket="bucket", Key="disk.qcow2")
    t.core.read_namespaced_secret.assert_called_once_with(
        "s3-secret", "project-ns", _request_timeout=5
    )
    assert t.create_client.call_args.kwargs["endpoint_url"] == "https://s3.example.test"
    config = t.create_client.call_args.kwargs["config"]
    assert config.connect_timeout == 2
    assert config.read_timeout == 3
    assert config.retries == {"total_max_attempts": 1}
    assert t.execute.call_args.kwargs["command"] == [
        "stat",
        "-c",
        "%s",
        "/scratch/tmpimage",
    ]
    assert t.execute.call_args.kwargs["container"] == "importer"
    assert t.execute.call_args.kwargs["_request_timeout"] == 5
    assert "private" not in repr(t.cache)


@pytest.mark.parametrize("region", ["eu-west-1", "ap-southeast-2"])
def test_probe_respects_sdk_region_configuration(transfer, monkeypatch, region):
    from boto3.session import Session

    monkeypatch.setenv("AWS_DEFAULT_REGION", region)
    session = Session()
    created = []

    def create_client(*args, **kwargs):
        client = session.client(*args, **kwargs)
        created.append(client)
        monkeypatch.setattr(client, "head_object", lambda **kw: {"ContentLength": 42})
        return client

    transfer.create_client.side_effect = create_client
    assert (
        transfer.module._object_size(
            transfer.core, transfer.dv["spec"]["source"]["s3"], "project-ns"
        )
        == 42
    )
    assert created[0].meta.region_name == region


def test_second_sample_reports_smoothed_actual_speed_and_reuses_head(transfer):
    t = transfer
    t.module.get_s3_download_progress(t.core, t.dv, "provider")
    t.now[0] += 20
    t.execute.return_value = "65000000"
    assert t.module.get_s3_download_progress(t.core, t.dv, "provider").endswith(
        "· 2.0 MB/s"
    )
    t.now[0] += 20
    t.execute.return_value = "85000000"
    assert t.module.get_s3_download_progress(t.core, t.dv, "provider").endswith(
        "· 1.5 MB/s"
    )
    t.client.head_object.assert_called_once()


def test_throttles_success_and_failure_probes(transfer):
    t = transfer
    result = t.module.get_s3_download_progress(t.core, t.dv, "provider")
    assert t.module.get_s3_download_progress(t.core, t.dv, "provider") == result
    t.execute.assert_called_once()
    t.now[0] += 20
    t.execute.side_effect = RuntimeError("forbidden")
    assert t.module.get_s3_download_progress(t.core, t.dv, "provider") is None
    assert t.module.get_s3_download_progress(t.core, t.dv, "provider") is None
    assert t.execute.call_count == 2


@pytest.mark.parametrize("restart", ["uid", "shrink"])
def test_restarts_reset_rate(transfer, restart):
    t = transfer
    t.module.get_s3_download_progress(t.core, t.dv, "provider")
    t.now[0] += 20
    t.execute.return_value = "50000000"
    t.module.get_s3_download_progress(t.core, t.dv, "provider")
    t.now[0] += 20
    if restart == "uid":
        t.pod.metadata.uid = "replacement-importer"
        t.execute.return_value = "60000000"
    else:
        t.execute.return_value = "100"
    assert "MB/s" not in t.module.get_s3_download_progress(t.core, t.dv, "provider")


@pytest.mark.parametrize("progress", ["0.0%", "50%", 15, "100.0%"])
def test_skips_cdi_conversion_progress(transfer, progress):
    t = transfer
    t.dv["status"]["progress"] = progress
    assert t.module.get_s3_download_progress(t.core, t.dv, "provider") is None
    t.core.list_namespaced_pod.assert_not_called()
    t.create_client.assert_not_called()


@pytest.mark.parametrize("total", [0, None])
def test_unknown_or_empty_object_returns_no_metrics(transfer, total):
    t = transfer
    t.client.head_object.return_value = {"ContentLength": total}
    assert t.module.get_s3_download_progress(t.core, t.dv, "provider") is None
    t.execute.assert_not_called()


def test_small_object_and_percent_are_bounded(transfer):
    t = transfer
    t.client.head_object.return_value = {"ContentLength": 100}
    t.execute.return_value = "101"
    assert (
        t.module.get_s3_download_progress(t.core, t.dv, "provider")
        == "downloading 100.0% — 101 / 100 B"
    )


def test_cache_is_scoped_by_provider_namespace_and_dv_uid(transfer):
    t = transfer
    t.module.get_s3_download_progress(t.core, t.dv, "provider-one")
    t.module.get_s3_download_progress(t.core, t.dv, "provider-two")
    t.dv["metadata"]["namespace"] = "other-ns"
    t.module.get_s3_download_progress(t.core, t.dv, "provider-one")
    t.dv["metadata"]["uid"] = "other-dv"
    t.module.get_s3_download_progress(t.core, t.dv, "provider-one")
    assert len(t.cache) == 4


def test_ignores_unrelated_importer_pod(transfer):
    t = transfer
    t.pod.spec.volumes[0].persistent_volume_claim.claim_name = "another-disk"
    assert t.module.get_s3_download_progress(t.core, t.dv, "provider") is None
    t.execute.assert_not_called()


def test_permission_failures_preserve_existing_status(transfer):
    t = transfer
    t.core.read_namespaced_secret.side_effect = RuntimeError("forbidden")
    assert t.module.get_s3_download_progress(t.core, t.dv, "provider") is None
    assert t.module.get_s3_download_progress(t.core, t.dv, "provider") is None
    t.core.read_namespaced_secret.assert_called_once()


@pytest.mark.parametrize("change", ["source", "phase", "namespace", "uid"])
def test_only_probes_eligible_s3_dvs(transfer, change):
    t = transfer
    if change == "source":
        t.dv["spec"]["source"] = {"http": {"url": "https://example.test/disk"}}
    elif change == "phase":
        t.dv["status"]["phase"] = "Succeeded"
    else:
        t.dv["metadata"].pop(change)
    assert t.module.get_s3_download_progress(t.core, t.dv, "provider") is None
    t.create_client.assert_not_called()
    t.execute.assert_not_called()


def test_missing_scratch_file_returns_no_override(transfer):
    t = transfer
    t.execute.return_value = ""
    assert t.module.get_s3_download_progress(t.core, t.dv, "provider") is None


def test_does_not_exec_nonrunning_importers(transfer):
    t = transfer
    t.pod.status.phase = "Pending"
    assert t.module.get_s3_download_progress(t.core, t.dv, "provider") is None
    t.execute.assert_not_called()


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_preserves_valid_source_endpoint_scheme(transfer, scheme):
    t = transfer
    t.dv["spec"]["source"]["s3"][
        "url"
    ] = f"{scheme}://s4.internal:9000/bucket/disk.qcow2"
    assert t.module.get_s3_download_progress(t.core, t.dv, "provider") is not None
    assert (
        t.create_client.call_args.kwargs["endpoint_url"]
        == f"{scheme}://s4.internal:9000"
    )
