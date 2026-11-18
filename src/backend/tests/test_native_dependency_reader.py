"""The bundled reader must distinguish successful zero overlap from failure."""

import pytest

from app.services.kubevirt_storage_transport import command_argv
from app.services.native_dependency_reader import observe


class Image:
    selected = None

    def id(self):
        return "image"

    def features(self):
        return 0

    def list_snaps(self):
        return [{"id": 17, "namespace": 2, "trash": {"original_namespace_type": 0}}]

    def set_snap_by_id(self, sid):
        assert sid == 17
        self.selected = sid

    def set_snap(self, name):
        assert name is None
        self.selected = None

    def overlap(self):
        return 100 if self.selected else 0

    def get_parent_image_spec(self):
        assert self.selected == 17
        return dict(
            pool_id=14,
            pool_namespace="",
            image_id="parent",
            snap_id=12,
            snap_namespace_type=2,
            trash=False,
        )


def test_reader_uses_successful_overlap_and_immutable_trash_snapshot():
    result = observe(Image(), "fsid", "image", 10, 128)
    assert result["head"] == {"overlap_bytes": 0, "parent": None}
    assert result["snapshots"][0]["dependency"]["parent"]["snapshot_id"] == "12"


@pytest.mark.parametrize(
    "failure",
    ["overlap", "parent", "missing_identity", "snapshot_changed", "migration", "limit"],
)
def test_reader_errors_never_become_zero_overlap(failure):
    image = Image()

    def unavailable():
        raise FileNotFoundError("sanitized by transport")

    if failure == "overlap":
        image.overlap = unavailable
    elif failure == "parent":
        image.get_parent_image_spec = unavailable
    elif failure == "missing_identity":
        image.get_parent_image_spec = lambda: {
            "snap_namespace_type": 0,
            "trash": False,
            "snap_id": 12,
        }
    elif failure == "snapshot_changed":
        reads = iter([[{"id": 17, "namespace": 0}], []])
        image.list_snaps = lambda: next(reads)
    elif failure == "migration":
        image.features = lambda: 128
    with pytest.raises((ValueError, KeyError, FileNotFoundError)):
        observe(image, "fsid", "image", 0 if failure == "limit" else 10, 128)


def test_dependency_transport_is_fixed_bounded_python3_program():
    args = dict(
        pool="pool",
        namespace="",
        image="image",
        ceph_fsid="d29a1eb6-17b1-4b3a-a2e4-6365b9ea75a6",
        pool_id=14,
        max_objects=10,
    )
    argv = command_argv("dependencies", **args)
    assert argv[:2] == ["python3", "-c"]
    assert "read_only=True" in argv[2]
    assert argv[-5:] == ["image", "10", "null", "", ""]
    for changed in (
        {"image": "x;id"},
        {"max_objects": 0},
        {"pool_id": True},
        {"ceph_fsid": "invalid"},
        {"dependency_snapshots": ["17;id"]},
        {"dependency_snapshots": ["17", "17"]},
    ):
        with pytest.raises(ValueError):
            command_argv("dependencies", **(args | changed))
    with pytest.raises(TypeError):
        command_argv("dependencies", **args, script="anything")


@pytest.mark.parametrize(
    "mismatch",
    [
        None,
        "version",
        "fsid",
        "pool",
        "cli",
        "python_package",
        "cli_package",
        "library_package",
    ],
)
def test_bundled_entrypoint_opens_only_pinned_read_only_image(
    monkeypatch, capsys, mismatch
):
    import json
    import sys
    from contextlib import nullcontext
    from types import SimpleNamespace

    from app.services import native_dependency_reader as bundled
    from app.services.native_dependency_reader import main

    outputs = [
        bundled.QUALIFIED_CLI,
        "python3-rbd-" + bundled.QUALIFIED_RELEASE,
        "ceph-common-" + bundled.QUALIFIED_RELEASE,
        "librbd1-" + bundled.QUALIFIED_RELEASE,
    ]
    changes = {"cli": 0, "python_package": 1, "cli_package": 2, "library_package": 3}
    if mismatch in changes:
        outputs[changes[mismatch]] = "unqualified"
    commands = []

    def run(command, **kwargs):
        assert kwargs == dict(check=True, capture_output=True, text=True, timeout=5)
        commands.append(command)
        return SimpleNamespace(stdout=outputs[len(commands) - 1])

    monkeypatch.setattr(bundled.subprocess, "run", run)
    monkeypatch.setattr(bundled.shutil, "which", lambda name: "/usr/bin/rbd")

    opened = []
    ioctx = SimpleNamespace(
        get_pool_id=lambda: 99 if mismatch == "pool" else 14,
        set_namespace=lambda value: opened.append(("namespace", value)),
    )
    cluster = SimpleNamespace(
        get_fsid=lambda: "wrong" if mismatch == "fsid" else "fsid",
        open_ioctx=lambda pool: nullcontext(ioctx),
    )

    def open_image(context, **kwargs):
        assert context is ioctx
        assert kwargs == {"image_id": "image", "read_only": True}
        opened.append(("image", kwargs))
        return nullcontext(Image())

    monkeypatch.setitem(
        sys.modules,
        "rados",
        SimpleNamespace(Rados=lambda **kwargs: nullcontext(cluster)),
    )
    monkeypatch.setitem(
        sys.modules,
        "rbd",
        SimpleNamespace(
            __file__="/usr/lib/python3/rbd.so",
            RBD=lambda: SimpleNamespace(
                version=lambda: (1, 18 if mismatch == "version" else 19, 0)
            ),
            Image=open_image,
            RBD_FEATURE_MIGRATING=128,
        ),
    )
    monkeypatch.setattr(
        sys, "argv", ["reader", "fsid", "pool", "14", "", "image", "10", "null", "", ""]
    )
    if mismatch:
        with pytest.raises(ValueError):
            main()
        assert not any(kind == "image" for kind, _ in opened)
    else:
        main()
        assert json.loads(capsys.readouterr().out)["image_id"] == "image"


def test_cache_reader_selects_only_requested_snapshot_and_requires_its_presence():
    image = Image()
    image.list_snaps = lambda: [{"id": 17, "namespace": 0}, {"id": 18, "namespace": 99}]
    result = observe(image, "fsid", "image", 1, 128, ["17"])
    assert [s["snapshot_id"] for s in result["snapshots"]] == ["17"]
    with pytest.raises(ValueError, match="missing boundary"):
        observe(image, "fsid", "image", 1, 128, ["19"])
