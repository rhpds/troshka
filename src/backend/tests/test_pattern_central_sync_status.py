import uuid

from app.api.patterns import _pattern_to_detail_dict, _pattern_to_list_dict
from app.models.pattern_location import PatternLocation
from tests.test_pattern_sync import _pattern_with_disks, _sync_session


def test_central_failure_is_visible_despite_successful_local_capture():
    db = _sync_session()
    try:
        pattern, disk = _pattern_with_disks(db, str(uuid.uuid4()))
        pattern.state = "available"
        db.add(
            PatternLocation(
                pattern_disk_id=disk.id,
                location_type="central",
                state="error",
                s3_key=disk.s3_key,
                error_message="Multipart completion failed",
            )
        )
        db.flush()
        for serialize in (_pattern_to_list_dict, _pattern_to_detail_dict):
            result = serialize(pattern, db=db)
            assert result["state"] == "available"
            assert result["central_sync"]["state"] == "error"
            assert "Multipart completion failed" in result["central_sync"]["error"]
            assert result["central_sync"]["synced_disks"] == 0
    finally:
        from sqlalchemy.orm import Session

        Session.close(db)


def test_partial_central_copy_never_reports_synced():
    db = _sync_session()
    try:
        pattern, _ = _pattern_with_disks(db, str(uuid.uuid4()))
        result = _pattern_to_detail_dict(pattern, db=db)
        assert result["central_sync"]["state"] == "pending"
        assert result["central_sync"]["total_disks"] == 1
    finally:
        from sqlalchemy.orm import Session

        Session.close(db)


def test_gold_pattern_does_not_claim_a_central_copy_is_pending():
    db = _sync_session()
    try:
        pattern, disk = _pattern_with_disks(db, str(uuid.uuid4()))
        db.add(
            PatternLocation(
                pattern_disk_id=disk.id,
                location_type="gold",
                state="synced",
                s3_key=disk.s3_key,
            )
        )
        db.flush()
        assert _pattern_to_detail_dict(pattern, db=db)["central_sync"] is None
    finally:
        from sqlalchemy.orm import Session

        Session.close(db)
