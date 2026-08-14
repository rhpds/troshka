"""Tests for central pattern/library sync wiring (central_library.py).

Covers two dedicated-Troshka deploy blockers:
  - synced central pattern disks must get a central PatternLocation (so
    pattern_disk_source_for_cluster resolves them), and
  - library refs in a synced pattern topology must remap to the local
    central-synced library items (not only source=="local").
"""

import uuid

from tests.conftest import TestSession


def test_create_pattern_record_creates_central_locations():
    from app.models.pattern_location import PatternLocation
    from app.services.central_library import _create_pattern_record

    db = TestSession()
    try:
        pid = str(uuid.uuid4())
        d1, d2 = str(uuid.uuid4()), str(uuid.uuid4())
        meta = {
            "name": "p1",
            "topology": {"nodes": [], "edges": []},
            "disks": [
                {
                    "id": d1,
                    "s3_key": f"patterns/{pid}/{d1}.qcow2",
                    "format": "qcow2",
                    "size_bytes": 100,
                },
                {
                    "id": d2,
                    "s3_key": f"patterns/{pid}/{d2}.qcow2",
                    "format": "qcow2",
                    "size_bytes": 200,
                },
            ],
        }
        _create_pattern_record(db, pid, meta, "owner", "prov-1")
        db.flush()
        for did in (d1, d2):
            loc = (
                db.query(PatternLocation)
                .filter_by(pattern_disk_id=did, location_type="central", state="synced")
                .first()
            )
            assert loc is not None, f"no central PatternLocation for {did}"
            assert loc.provider_id is None
            assert loc.s3_key == f"patterns/{pid}/{did}.qcow2"
    finally:
        db.rollback()
        db.close()


def test_remap_library_refs_matches_central_items():
    from app.models.library import Library, LibraryItem
    from app.services.central_library import _remap_library_refs

    db = TestSession()
    try:
        lib = Library(id=str(uuid.uuid4()), type="central")
        db.add(lib)
        db.flush()
        item = LibraryItem(
            id=str(uuid.uuid4()),
            library_id=lib.id,
            name="RHEL 10.2 Binary DVD",
            type="iso",
            format="iso",
            s3_key="library/x/RHEL 10.2 Binary DVD.iso",
            source="central",
            state="ready",
            size_bytes=999,
        )
        db.add(item)
        db.flush()
        topo = {
            "nodes": [
                {
                    "type": "storageNode",
                    "data": {
                        "source": "library",
                        "format": "iso",
                        "libraryItemId": "old-id-from-source-instance",
                        "libraryItemName": "RHEL 10.2 Binary DVD",
                        "label": "rhel-dvd",
                    },
                }
            ]
        }
        _remap_library_refs(topo, db)
        assert topo["nodes"][0]["data"]["libraryItemId"] == item.id
    finally:
        db.rollback()
        db.close()
