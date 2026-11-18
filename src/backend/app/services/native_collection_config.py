"""Protected collector configuration shared by operator CLI and RQ workers."""

import json
import os
import stat
from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from pydantic import (
    Field,
    TypeAdapter,
    field_validator,
    model_serializer,
    model_validator,
)

from app.schemas.kubevirt_storage import Evidence, ProjectRoot
from app.schemas.kubevirt_storage_enrollment import VerificationPins
from app.schemas.native_snapshot_custody import SnapshotJournalProfile
from app.services.kubevirt_storage_inventory import KubernetesReader
from app.services.kubevirt_storage_transport import (
    CapabilityProfile,
    DirectTransport,
    PoolConfig,
    RbdReader,
    ToolboxConfig,
    ToolboxTransport,
)


class CacheRoot(Evidence):
    cluster_key: str = Field(min_length=1, max_length=256)
    namespace: str = Field(min_length=1, max_length=256)
    namespace_uid: str = Field(min_length=1, max_length=256)


class Manifest(Evidence):
    collector_id: str = Field(min_length=1, max_length=256)
    ceph_fsid: UUID
    project_roots: list[ProjectRoot] = Field(min_length=1, max_length=10000)
    cache_roots: list[CacheRoot] = Field(default_factory=list, max_length=100)
    pools: list[PoolConfig] = Field(min_length=1, max_length=100)
    capability: CapabilityProfile
    toolbox: ToolboxConfig | None = None
    csi_requests: dict[str, str] = Field(default_factory=dict, max_length=10000)


def read_operator_file(path, limit=64 * 1024 * 1024):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
        ):
            raise ValueError(
                "operator-owned regular file without group/other writes required"
            )
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("operator input exceeds size bound")
    return json.loads(raw)


class BackendConfig(Evidence):
    ceph_fsid: UUID
    pools: list[PoolConfig] = Field(min_length=1, max_length=100)
    snapshot_profiles: list[SnapshotJournalProfile] = Field(
        default_factory=list, max_length=100
    )

    @model_serializer(mode="wrap")
    def legacy(self, handler):
        value = handler(self)
        if not self.snapshot_profiles:
            value.pop("snapshot_profiles", None)
        return value


class DirectBackendConfig(BackendConfig):
    # Legacy direct documents may omit mode; both credential paths stay mandatory.
    mode: Literal["direct"] = "direct"
    ceph_conf: str = Field(min_length=1)
    ceph_keyring: str = Field(min_length=1)


class PinnedToolboxConfig(ToolboxConfig):
    cluster_key: str = Field(min_length=1)
    pod_name: str = Field(pattern=r"^rook-ceph-tools-[A-Za-z0-9_.-]+$")
    pod_uid: str = Field(min_length=1)
    allowed_pools: list[str] = Field(min_length=1, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def explicit_toolbox_pins(cls, value):
        # The transport base has defaults; protected configuration must supply
        # both identities explicitly before those defaults can be applied.
        if (
            isinstance(value, Mapping)
            and not {"namespace", "container"} <= value.keys()
        ):
            raise ValueError(
                f"{cls.__name__}: toolbox namespace and container must be explicit"
            )
        return value

    @field_validator("namespace")
    @classmethod
    def pinned_namespace(cls, value: str) -> str:
        if value != "openshift-storage":
            raise ValueError(
                f"{cls.__name__}: toolbox namespace must be openshift-storage"
            )
        return value

    @field_validator("container")
    @classmethod
    def pinned_container(cls, value: str) -> str:
        if value != "rook-ceph-tools":
            raise ValueError(
                f"{cls.__name__}: toolbox container must be rook-ceph-tools"
            )
        return value


class ToolboxBackendConfig(BackendConfig):
    mode: Literal["toolbox"]
    kubeconfig: str = Field(min_length=2, pattern=r"^/")
    toolbox: PinnedToolboxConfig

    @model_validator(mode="after")
    def matching_pins(self):
        if self.toolbox.ceph_fsid != str(self.ceph_fsid):
            raise ValueError("toolbox Ceph identity mismatch")
        allowed = self.toolbox.allowed_pools
        if len(set(allowed)) != len(allowed) or set(allowed) != {
            p.name for p in self.pools
        }:
            raise ValueError("toolbox pool allowlist must match configured pools")
        return self


class ToolboxIdentityReader(KubernetesReader):
    """Reuse collector identity GETs with verification's budget and cleanup."""

    def __init__(self, kubeconfig, limits, deadline, kubeconfig_data):
        self.command_timeout_seconds = limits.command_timeout_seconds
        try:
            super().__init__(
                kubeconfig,
                None,
                collector_id="enrollment-identity-only",
                cache_roots=[],
                scan_deadline=deadline,
                kubeconfig_data=kubeconfig_data,
            )
        except Exception:
            if getattr(self, "api", None) is not None:
                self.api.close()
            raise

    def _timeout(self):
        return min(super()._timeout(), self.command_timeout_seconds)


def _check_backend_coverage(backends, pins):
    fsids = [str(config.ceph_fsid) for config in backends]
    configured = {(str(c.ceph_fsid), p.pool_id) for c in backends for p in c.pools}
    pinned = {(p.ceph_fsid, p.pool_id) for p in pins.pool_keys}
    if len(fsids) != len(set(fsids)) or configured != pinned:
        raise ValueError("backend configuration must exactly cover pinned pools")


def _backend_reader(config, limits, deadline, cleanup, kubeconfig_data):
    if isinstance(config, ToolboxBackendConfig):
        kube = ToolboxIdentityReader(
            config.kubeconfig, limits, deadline, kubeconfig_data
        )
        cleanup.callback(kube.api.close)
        transport = ToolboxTransport(
            limits, config.toolbox, kube, config.kubeconfig, scan_deadline=deadline
        )
    else:
        transport = DirectTransport(
            limits, ceph_conf=config.ceph_conf, ceph_keyring=config.ceph_keyring
        )
        transport.process.scan_deadline = deadline
    reader = RbdReader(
        transport,
        str(config.ceph_fsid),
        config.pools,
        CapabilityProfile(command_version="enrollment-identity-only"),
    )
    reader.snapshot_profiles = config.snapshot_profiles
    return reader


def _managed_inventory(value, pins):
    if pins.empty_domain_bootstrap is None:
        return Manifest.model_validate(value)

    class BootstrapInventory(Manifest):
        # Only this managed path accepts zero roots. Protected DB anchoring in
        # snapshot() still authorizes the exact bootstrap pins before collection.
        project_roots: list[ProjectRoot] = Field(max_length=0)

    return BootstrapInventory.model_validate(value)


def _managed_inputs(args):
    pins = VerificationPins.model_validate(read_operator_file(args.pins))
    config = _managed_inventory(read_operator_file(args.inventory), pins)
    backends = TypeAdapter(
        list[DirectBackendConfig | ToolboxBackendConfig]
    ).validate_python(read_operator_file(args.backend_config))
    _check_backend_coverage(backends, pins)
    if (
        len(backends) != 1
        or config.collector_id not in pins.collector_ids
        or config.capability.initialization_evidence != pins.initialization_evidence
    ):
        raise ValueError(
            "managed collection needs one matching protected initialization/collector profile"
        )
    if config.pools != backends[0].pools or str(config.ceph_fsid) != str(
        backends[0].ceph_fsid
    ):
        raise ValueError("collector/backend pool configuration mismatch")
    return pins, config, backends[0]
