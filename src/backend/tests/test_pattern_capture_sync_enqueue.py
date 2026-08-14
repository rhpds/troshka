from unittest.mock import patch

from app.services import pattern_service


def test_enqueue_sync_after_capture_helper():
    with patch.object(pattern_service, "enqueue_job") as enq:
        pattern_service._enqueue_pattern_sync("pat-123", "prov-1")
        enq.assert_called_once()
        args, kwargs = enq.call_args
        assert args[0] is pattern_service.sync_pattern_to_central
        assert args[1] == "pat-123"
        assert kwargs.get("queue_name") == "default"


def test_no_enqueue_without_source_provider():
    with patch.object(pattern_service, "enqueue_job") as enq:
        pattern_service._enqueue_pattern_sync("pat-123", None)
        enq.assert_not_called()
