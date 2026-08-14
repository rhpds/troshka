"""Tests for the legacy patternDiskId topology backfill.

Legacy KubeVirt-captured patterns stored the disk *content UUID* (source_disk_id)
in topology storageNode.data.patternDiskId instead of the PatternDisk.id that
PatternLocation FKs to. This backfill remaps them so deploy placement can find
the disks.
"""

import uuid

import pytest
from sqlalchemy import delete

from app.models.pattern import Pattern, PatternDisk
from app.models.pattern_location import PatternLocation
from app.models.project import Project
from app.models.user import User
from app.scripts.backfill_topology_pattern_disk_ids import (
    backfill_topology_pattern_disk_ids,
)
from tests.conftest import TestSession

_PROJ_NAME = "__backfill_topo_test_proj__"


@pytest.fixture(autouse=True)
def _isolate_pattern_tables():
    """Remove this suite's pattern/project rows before and after each test.

    The backfill scans ALL patterns and projects. Wiping the pattern tables is
    safe (other suites recreate their own), but Projects are shared — other
    suites (e.g. test_gc) keep module-level Project rows that must survive, so
    delete ONLY projects created by this suite (matched by a sentinel name).
    Never touch User — the shared dev-mode admin must survive for auth tests.
    """

    def _clean():
        db = TestSession()
        try:
            db.execute(delete(PatternLocation))
            db.execute(delete(PatternDisk))
            db.execute(delete(Project).where(Project.name == _PROJ_NAME))
            db.execute(delete(Pattern))
            db.commit()
        finally:
            db.close()

    _clean()
    yield
    _clean()


def _user(db):
    u = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4()}@e.com", display_name="t")
    db.add(u)
    db.flush()
    return u


def _pattern_with_disk(db, owner, *, topo_disk_id):
    """Pattern whose topology storageNode references *topo_disk_id*."""
    pat = Pattern(
        id=str(uuid.uuid4()),
        name="p",
        owner_id=owner.id,
        topology={
            "nodes": [
                {
                    "id": "content-uuid",
                    "type": "storageNode",
                    "data": {
                        "source": "pattern",
                        "patternId": None,  # filled below
                        "patternDiskId": topo_disk_id,
                    },
                }
            ]
        },
    )
    db.add(pat)
    db.flush()
    pat.topology["nodes"][0]["data"]["patternId"] = pat.id
    pd = PatternDisk(
        id=str(uuid.uuid4()),
        pattern_id=pat.id,
        source_disk_id="content-uuid",
        source_vm_id="vm",
        s3_key="patterns/p/content-uuid.qcow2",
        format="qcow2",
        size_bytes=100,
        state="available",
    )
    db.add(pd)
    db.flush()
    return pat, pd


def test_remaps_legacy_content_uuid_to_pattern_disk_id():
    db = TestSession()
    try:
        user = _user(db)
        # topology references the content UUID (source_disk_id), the bug
        pat, pd = _pattern_with_disk(db, user, topo_disk_id="content-uuid")
        db.commit()

        result = backfill_topology_pattern_disk_ids(db)

        db.refresh(pat)
        assert pat.topology["nodes"][0]["data"]["patternDiskId"] == pd.id
        assert result["patterns_fixed"] == 1
        assert result["disks_remapped"] == 1
    finally:
        db.close()


def test_leaves_correct_references_untouched():
    db = TestSession()
    try:
        user = _user(db)
        pat, pd = _pattern_with_disk(db, user, topo_disk_id="placeholder")
        # already correct: reassign topology (in-place JSONB edits aren't tracked)
        # pointing at the real PatternDisk.id
        pat.topology = {
            "nodes": [
                {
                    "id": "content-uuid",
                    "type": "storageNode",
                    "data": {
                        "source": "pattern",
                        "patternId": pat.id,
                        "patternDiskId": pd.id,
                    },
                }
            ]
        }
        db.commit()

        result = backfill_topology_pattern_disk_ids(db)

        db.refresh(pat)
        assert pat.topology["nodes"][0]["data"]["patternDiskId"] == pd.id
        assert result["patterns_fixed"] == 0
        assert result["disks_remapped"] == 0
    finally:
        db.close()


def test_remaps_project_topology_via_pattern_id():
    db = TestSession()
    try:
        user = _user(db)
        pat, pd = _pattern_with_disk(db, user, topo_disk_id="content-uuid")
        # a deployed project cloned from the pattern, carrying the bad id
        proj = Project(
            id=str(uuid.uuid4()),
            name=_PROJ_NAME,
            owner_id=user.id,
            state="error",
            topology={
                "nodes": [
                    {
                        "id": "node-x",
                        "type": "storageNode",
                        "data": {
                            "source": "pattern",
                            "patternId": pat.id,
                            "patternDiskId": "content-uuid",
                        },
                    }
                ]
            },
        )
        db.add(proj)
        db.commit()

        result = backfill_topology_pattern_disk_ids(db)

        db.refresh(proj)
        assert proj.topology is not None
        assert proj.topology["nodes"][0]["data"]["patternDiskId"] == pd.id
        assert result["projects_fixed"] == 1
    finally:
        db.close()


def test_dry_run_does_not_persist():
    db = TestSession()
    try:
        user = _user(db)
        pat, pd = _pattern_with_disk(db, user, topo_disk_id="content-uuid")
        db.commit()

        result = backfill_topology_pattern_disk_ids(db, dry_run=True)

        db.refresh(pat)
        # unchanged on disk, but the count reflects what WOULD change
        assert pat.topology["nodes"][0]["data"]["patternDiskId"] == "content-uuid"
        assert result["disks_remapped"] == 1
    finally:
        db.close()
