"""
Pattern service — captures VM disk snapshots to S3 for pattern storage.
"""

_capture_progress: dict[str, dict] = {}


def capture_pattern_disks(pattern_id: str, project_id: str) -> None:
    """Capture all disks from a project into a pattern (stub)."""
    pass


def get_capture_progress(pattern_id: str) -> dict | None:
    """Return capture progress for a pattern, or None if not tracking."""
    return _capture_progress.get(pattern_id)
