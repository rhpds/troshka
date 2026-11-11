"""Constrained read-only Ceph transport. Never expose raw command failures."""

import json
import os
import re
import selectors
import signal
import subprocess
import time
from datetime import UTC, datetime
from uuid import UUID

from pydantic import Field

from app.schemas.kubevirt_storage import Bytes, Evidence, ImageKey, ReadResult


def utcnow():
    return datetime.now(UTC)


def _token(value, *, empty=False):
    if (
        not isinstance(value, str)
        or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,256}" if not empty else r"[A-Za-z0-9_.-]{0,256}", value
        )
        or value.startswith("-")
    ):
        raise ValueError("unsupported identity syntax")
    return value


def command_argv(
    operation,
    *,
    pool=None,
    namespace="",
    image=None,
    snapshot=None,
    object_uuid=None,
    journal_key=None,
):
    if operation == "fsid":
        return ["ceph", "fsid"]
    if operation == "pools":
        return ["ceph", "osd", "lspools", "--format", "json"]
    if operation == "version":
        return ["rbd", "--version"]
    pool, namespace = _token(pool), _token(namespace, empty=True)
    opts = ["--pool", pool, "--namespace", namespace]
    if operation == "journal":
        object_uuid = str(UUID(object_uuid))
        if journal_key not in {"csi.imageid", "csi.imagename", "csi.volname"}:
            raise ValueError("unsupported journal key")
        return [
            "rados",
            "--pool",
            pool,
            "--namespace",
            namespace,
            "getomapval",
            "csi.volume." + object_uuid,
            journal_key,
            "-",
        ]
    if operation == "trash":
        return ["rbd", "trash", "ls", *opts, "--format", "json"]
    if operation not in {"info", "info_id", "du", "snapshots", "children"}:
        raise ValueError("unsupported read operation")
    image = _token(image)
    base = {
        "info": ["info"],
        "info_id": ["info"],
        "du": ["du", "--merge-snapshots"],
        "snapshots": ["snap", "ls", "--all"],
        "children": ["children", "--all"],
    }[operation]
    argv = [
        "rbd",
        *base,
        *opts,
        "--image-id" if operation == "info_id" else "--image",
        image,
    ]
    if snapshot is not None:
        if operation != "children":
            raise ValueError("snapshot only supported for children")
        argv += ["--snap", _token(snapshot)]
    return [*argv, "--format", "json"]


def _read_events(selector, proc, output, count, cap, wait):
    for event, _ in selector.select(wait):
        chunk = os.read(event.fileobj.fileno(), 65536)
        if not chunk:
            selector.unregister(event.fileobj)
            continue
        count += len(chunk)
        if count > cap:
            return count, "output_limit"
        if event.fileobj is proc.stdout:
            output.extend(chunk)
    return count, "complete"


def _drain_process(proc, deadline, cap):
    selector = selectors.DefaultSelector()
    output, count = bytearray(), 0
    for pipe in (proc.stdout, proc.stderr):
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe, selectors.EVENT_READ)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", output
            count, status = _read_events(
                selector, proc, output, count, cap, min(0.1, remaining)
            )
            if status != "complete":
                return status, output
        return "complete", output
    finally:
        selector.close()


def _reap_process(proc, status):
    if status == "complete":
        try:
            proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            status = "timeout"
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()
    proc.stdout.close()
    proc.stderr.close()
    if status == "complete" and proc.returncode != 0:
        return "timeout" if proc.returncode in (124, 137) else "unavailable"
    return status


class BoundedProcess:
    def __init__(self, limits):
        self.limits = limits
        self.scan_deadline = None

    def run(self, argv, *, timeout=None):
        """Drain both pipes concurrently without retaining stderr or unbounded data."""
        deadline = time.monotonic() + (timeout or self.limits.command_timeout_seconds)
        if self.scan_deadline is not None:
            deadline = min(deadline, self.scan_deadline)
        if time.monotonic() >= deadline:
            return ReadResult(status="scan_limit", collected_at=utcnow())
        status, output = "unavailable", bytearray()
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError:
            return ReadResult(status="unavailable", collected_at=utcnow())
        try:
            status, output = _drain_process(
                proc, deadline, self.limits.max_output_bytes
            )
        finally:
            status = _reap_process(proc, status)
        if status != "complete":
            return ReadResult(status=status, collected_at=utcnow())
        try:
            value = output.decode("utf-8").strip()
        except UnicodeError:
            return ReadResult(status="invalid_output", collected_at=utcnow())
        return ReadResult(status="complete", value=value, collected_at=utcnow())


class CapabilityProfile(Evidence):
    command_version: str = Field(min_length=1, max_length=256)
    merge_snapshots_validated: bool = False
    validation_reference: str | None = Field(default=None, max_length=256)
    semantics_version: str = "kubevirt-rbd-allocated-v1"


class PoolConfig(Evidence):
    pool_id: int = Field(ge=0)
    name: str
    csi_cluster_id: str
    journal_pool: str | None = None
    rbd_namespace: str = ""


class ToolboxConfig(Evidence):
    cluster_key: str
    namespace: str = "openshift-storage"
    pod_name: str
    pod_uid: str
    container: str = "rook-ceph-tools"
    ceph_fsid: str
    allowed_pools: list[str]


class DirectTransport:
    def __init__(self, limits, *, ceph_conf=None, ceph_keyring=None):
        self.process = BoundedProcess(limits)
        self.credentials = []
        for flag, path in (("--conf", ceph_conf), ("--keyring", ceph_keyring)):
            if path:
                self.credentials += [flag, os.path.abspath(path)]

    def run(self, operation, **identities):
        argv = command_argv(operation, **identities)
        return self.process.run([argv[0], *self.credentials, *argv[1:]])


class ToolboxTransport:
    def __init__(self, limits, config, kube_reader, kubeconfig, *, scan_deadline=None):
        self.process, self.limits, self.config = BoundedProcess(limits), limits, config
        self.process.scan_deadline = scan_deadline
        self.kube_reader, self.kubeconfig = kube_reader, os.path.abspath(kubeconfig)
        if (
            config.namespace != "openshift-storage"
            or config.container != "rook-ceph-tools"
            or not config.pod_name.startswith("rook-ceph-tools-")
        ):
            raise ValueError("unsupported toolbox identity")
        for value in (config.pod_name, config.container, *config.allowed_pools):
            _token(value)
        self.ready = False
        self._verify()
        # A fixed, harmless timeout probe verifies kill-after syntax before any Ceph read.
        result = self.process.run(
            [*self._prefix(), "timeout", "--kill-after=2s", "1s", "true"], timeout=5
        )
        if result.status != "complete":
            raise ValueError("remote timeout unavailable")
        self.ready = True
        identity = self._dispatch(command_argv("fsid"))
        if identity.status != "complete" or identity.value != config.ceph_fsid:
            raise ValueError("toolbox Ceph identity mismatch")

    def _verify(self):
        c = self.config
        pod = self.kube_reader.pod(c.namespace, c.pod_name)
        if (
            self.kube_reader.cluster_identity() != c.cluster_key
            or pod.get("metadata", {}).get("uid") != c.pod_uid
            or c.container
            not in {v["name"] for v in pod.get("spec", {}).get("containers", [])}
        ):
            raise ValueError("toolbox identity changed")

    def _prefix(self):
        c = self.config
        return [
            "oc",
            "--kubeconfig=" + self.kubeconfig,
            "exec",
            "-n",
            c.namespace,
            c.pod_name,
            "-c",
            c.container,
            "--",
        ]

    def run(self, operation, **identities):
        argv = command_argv(operation, **identities)
        if "pool" in identities and identities["pool"] not in self.config.allowed_pools:
            raise ValueError("pool outside toolbox allowlist")
        try:
            self._verify()
        except Exception:
            return ReadResult(status="identity_changed", collected_at=utcnow())
        return self._dispatch(argv)

    def _dispatch(self, argv):
        remaining = (
            self.process.scan_deadline - time.monotonic()
            if self.process.scan_deadline
            else None
        )
        if remaining is not None and remaining < 6:
            return ReadResult(status="scan_limit", collected_at=utcnow())
        remote = (
            min(self.limits.command_timeout_seconds, int(remaining - 5))
            if remaining
            else self.limits.command_timeout_seconds
        )
        prefix = [*self._prefix(), "timeout", "--kill-after=2s", f"{remote}s"]
        return self.process.run([*prefix, *argv], timeout=remote + 5)


def decode_csi_handle(handle):
    match = re.fullmatch(
        r"0001-([0-9a-f]{4})-(.+)-([0-9a-f]{16})-([0-9a-f-]{36})", handle
    )
    if not match or len(match[2]) != int(match[1], 16):
        raise ValueError("unsupported CSI handle")
    _token(match[2])
    return match[2], int(match[3], 16), str(UUID(match[4]))


def parse_usage(payload, image_id):
    rows = payload.get("images")
    if (
        not isinstance(rows, list)
        or len(rows) != 1
        or rows[0].get("id") != image_id
        or "snapshot" in rows[0]
    ):
        raise ValueError("missing or ambiguous merged image aggregate")
    from pydantic import TypeAdapter

    adapter = TypeAdapter(Bytes)
    return adapter.validate_python(rows[0]["used_size"]), adapter.validate_python(
        rows[0]["provisioned_size"]
    )


class RbdReader:
    """Explicit reader interface with configured pool and CSI journal ownership."""

    def __init__(self, transport, ceph_fsid, pools, capability):
        self.transport, self.ceph_fsid, self.pools, self.capability = (
            transport,
            ceph_fsid,
            pools,
            capability,
        )
        self.names = {}
        self.binding_target_verified = False

    def _read(self, op, json_output=True, **kwargs):
        result = self.transport.run(op, **kwargs)
        if result.status != "complete":
            return result
        try:
            value = json.loads(result.value) if json_output else result.value
            return result.model_copy(update={"value": value})
        except (ValueError, TypeError):
            return ReadResult(status="invalid_output", collected_at=result.collected_at)

    def cluster_identity(self):
        return self._read("fsid", False)

    def command_version(self):
        return self._read("version", False)

    def pool_identity(self):
        result = self._read("pools")
        if result.status != "complete":
            return result
        if not isinstance(result.value, list) or any(
            not any(
                row.get("poolnum") == p.pool_id and row.get("poolname") == p.name
                for row in result.value
            )
            for p in self.pools
        ):
            return ReadResult(
                status="identity_changed", collected_at=result.collected_at
            )
        return result

    def _pool(self, key):
        if key.ceph_fsid != self.ceph_fsid:
            raise ValueError("foreign Ceph identity")
        found = [
            p
            for p in self.pools
            if p.pool_id == key.pool_id and p.rbd_namespace == key.rbd_namespace
        ]
        if len(found) != 1:
            raise ValueError("unconfigured pool identity")
        return found[0]

    def _csi_pool(self, driver, cluster, pool_id, expected_request, attributes):
        pools = [
            p
            for p in self.pools
            if p.pool_id == pool_id and p.csi_cluster_id == cluster
        ]
        if (
            len(pools) != 1
            or driver not in {"rbd.csi.ceph.com", cluster + ".rbd.csi.ceph.com"}
            or expected_request is None
        ):
            raise ValueError("unsupported CSI mapping")
        p = pools[0]
        expected = {
            "clusterID": cluster,
            "pool": p.name,
            "journalPool": p.journal_pool or p.name,
        }
        if any(
            name in attributes and attributes[name] != value
            for name, value in expected.items()
        ):
            raise ValueError("CSI attributes disagree with configured identity")
        return p

    def _verify_binding_target(self):
        identity = self.cluster_identity()
        if identity.status != "complete" or identity.value != self.ceph_fsid:
            return ReadResult(status="identity_changed", collected_at=utcnow())
        pools = self.pool_identity()
        if pools.status == "complete":
            self.binding_target_verified = True
        return pools

    def resolve_csi(
        self, driver, handle, *, expected_request=None, volume_attributes=None
    ):
        try:
            if not self.binding_target_verified:
                verified = self._verify_binding_target()
                if verified.status != "complete":
                    return verified
            cluster, pool_id, object_uuid = decode_csi_handle(handle)
            attributes = volume_attributes or {}
            p = self._csi_pool(driver, cluster, pool_id, expected_request, attributes)
            values = {}
            for field in ("csi.imageid", "csi.imagename", "csi.volname"):
                result = self._read(
                    "journal",
                    False,
                    pool=p.journal_pool or p.name,
                    namespace=p.rbd_namespace,
                    object_uuid=object_uuid,
                    journal_key=field,
                )
                if result.status != "complete":
                    return result
                values[field] = _token(result.value)
            if values["csi.volname"] != expected_request:
                return ReadResult(status="identity_changed", collected_at=utcnow())
            if (
                "imageName" in attributes
                and attributes["imageName"] != values["csi.imagename"]
            ):
                return ReadResult(status="identity_changed", collected_at=utcnow())
            info = self._read(
                "info",
                pool=p.name,
                namespace=p.rbd_namespace,
                image=values["csi.imagename"],
            )
            if info.status != "complete":
                return info
            if (
                not isinstance(info.value, dict)
                or info.value.get("id") != values["csi.imageid"]
            ):
                return ReadResult(
                    status="identity_changed", collected_at=info.collected_at
                )
            key = ImageKey(
                ceph_fsid=self.ceph_fsid,
                pool_id=pool_id,
                rbd_namespace=p.rbd_namespace,
                image_id=values["csi.imageid"],
            )
            self.names[key] = values["csi.imagename"]
            return ReadResult(
                status="complete", collected_at=info.collected_at, value=key
            )
        except (ValueError, KeyError, TypeError):
            return ReadResult(status="unsupported", collected_at=utcnow())

    def _image(self, op, key, **kwargs):
        try:
            p = self._pool(key)
            name = self.names.get(key)
            if name is None:
                # Immutable ID addresses parents and retained images without name guessing.
                return self._read_id(op, p, key, **kwargs)
            return self._read(
                op, pool=p.name, namespace=p.rbd_namespace, image=name, **kwargs
            )
        except (ValueError, KeyError):
            return ReadResult(status="unsupported", collected_at=utcnow())

    def _read_id(self, op, pool, key, **kwargs):
        # rbd supports --image-id for info; info supplies the verified current name.
        result = self.transport.run(
            "info_id", pool=pool.name, namespace=pool.rbd_namespace, image=key.image_id
        )
        if result.status != "complete":
            return result
        try:
            info = json.loads(result.value)
            if info["id"] != key.image_id:
                raise ValueError("changed image")
            self.names[key] = _token(info["name"])
            return self._image(op, key, **kwargs)
        except (ValueError, KeyError, TypeError):
            return ReadResult(
                status="identity_changed", collected_at=result.collected_at
            )

    def image_info(self, key):
        return self._image("info", key)

    def image_usage(self, key):
        return self._image("du", key)

    def image_snapshots(self, key):
        return self._image("snapshots", key)

    def image_children(self, key, snapshot):
        return self._image("children", key, snapshot=snapshot)

    def trash_inventory(self, key):
        p = self._pool(key)
        return self._read("trash", pool=p.name, namespace=p.rbd_namespace)
