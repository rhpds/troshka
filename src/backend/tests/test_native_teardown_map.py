"""Map-based teardown seals custody or live-namespace targets without fleet scans."""

from types import SimpleNamespace
from uuid import uuid4

from app.schemas.kubevirt_storage import ImageKey, ProjectRoot, VolumeBinding
from app.schemas.native_project_cleanup import (
    CleanupAuthority,
    ProjectNamespaceCleanup,
    StorageLiabilities,
)
from app.services import native_teardown_map as teardown
from app.services.kubevirt_storage_enrollment import digest


def _authority(**updates):
    base = dict(
        source_kind="admin_custody",
        source_id=uuid4(),
        source_digest="a" * 64,
        project_id=uuid4(),
        owner_id=uuid4(),
        host_id=uuid4(),
        provider_id=uuid4(),
        cluster_uid="cluster",
        namespace="troshka-demo",
        namespace_uid="ns-uid",
        root_uid="root-uid",
        domain_id=uuid4(),
        enrollment_id=uuid4(),
        root=None,
    )
    base.update(updates)
    return CleanupAuthority(**base)


def _work(liabilities, **authority):
    selected = _authority(**authority)
    return ProjectNamespaceCleanup(
        id=uuid4(),
        authority=selected,
        mode="delete",
        liabilities=liabilities,
        liability_digest=digest(liabilities.model_dump(mode="json")),
        prepared_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )


def test_hard_debts_ignore_soft_coverage_only():
    liabilities = StorageLiabilities(
        unresolved=["storage_coverage_unavailable", "pending_pvc:x"]
    )
    assert teardown.hard_debts(liabilities) == ["pending_pvc:x"]
    assert not teardown.map_sealed(
        SimpleNamespace(
            liabilities=StorageLiabilities(unresolved=["storage_coverage_incomplete"])
        )
    )


def test_seal_from_custody_clears_soft_coverage(monkeypatch):
    key = ImageKey(ceph_fsid="fsid", pool_id=1, rbd_namespace="", image_id="csi-vol-1")
    root = ProjectRoot(
        project_id=uuid4(),
        cluster_key="c",
        namespace="ns",
        namespace_uid="u",
        project_cr_uid="r",
    )
    binding = VolumeBinding(
        root=root,
        pvc_uid="pvc",
        pv_uid="pv",
        csi_driver="rbd.csi.ceph.com",
        volume_handle="handle",
        image_key=key,
    )
    work = _work(
        StorageLiabilities(
            bindings=[binding],
            private_images=[key],
            unresolved=["storage_coverage_incomplete"],
        ),
        root=root,
    )
    saved = {}

    def _save(db, current, **updates):
        value = current.model_copy(update=updates)
        saved["work"] = value
        return value

    monkeypatch.setattr("app.services.native_project_namespace_cleanup._save", _save)
    monkeypatch.setattr(
        "app.services.native_project_namespace_cleanup._evidence",
        lambda *a, **k: None,
    )
    sealed = teardown.seal(SimpleNamespace(), SimpleNamespace(), work)
    assert sealed.liabilities.unresolved == []
    assert sealed.liabilities.private_images == [key]
    assert teardown.map_sealed(sealed)


def test_seal_empty_namespace_clears_soft_coverage(monkeypatch):
    work = _work(StorageLiabilities(unresolved=["storage_coverage_unavailable"]))

    class API:
        def rows(self, kind, namespace):
            return []

    monkeypatch.setattr(
        "app.services.native_project_namespace_cleanup._save",
        lambda db, current, **updates: current.model_copy(update=updates),
    )
    monkeypatch.setattr(
        "app.services.native_project_namespace_cleanup._evidence",
        lambda *a, **k: None,
    )
    sealed = teardown.seal(SimpleNamespace(), API(), work)
    assert sealed.liabilities.unresolved == []
    assert sealed.liabilities.private_images == []


def test_shared_images_never_become_private(monkeypatch):
    shared = ImageKey(ceph_fsid="fsid", pool_id=1, rbd_namespace="", image_id="gold")
    work = _work(
        StorageLiabilities(
            shared_images=[shared],
            unresolved=["storage_coverage_unavailable"],
        )
    )

    class API:
        def rows(self, kind, namespace):
            return []

    monkeypatch.setattr(
        "app.services.native_project_namespace_cleanup._save",
        lambda db, current, **updates: current.model_copy(update=updates),
    )
    monkeypatch.setattr(
        "app.services.native_project_namespace_cleanup._evidence",
        lambda *a, **k: None,
    )
    sealed = teardown.seal(SimpleNamespace(), API(), work)
    assert sealed.liabilities.shared_images == [shared]
    assert shared not in sealed.liabilities.private_images
