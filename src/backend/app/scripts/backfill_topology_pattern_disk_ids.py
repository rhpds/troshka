"""One-time backfill: remap legacy topology patternDiskId values.

Legacy KubeVirt-native pattern captures wrote the disk *content UUID*
(``PatternDisk.source_disk_id``, the s3_key filename stem) into
``storageNode.data.patternDiskId`` instead of the ``PatternDisk.id`` that
``PatternLocation`` FKs to. Deploy placement looks disks up by ``PatternDisk.id``
via ``pattern_locations``, so those patterns — and every project cloned from
them — fail placement with a misleading "not enough capacity" error.

This script remaps every affected storageNode in all patterns AND projects from
the content UUID to the real ``PatternDisk.id``. It is idempotent: nodes already
pointing at a valid ``PatternDisk.id`` are left untouched.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.models.pattern import Pattern, PatternDisk
from app.models.project import Project

log = logging.getLogger(__name__)


def _remap_topology(topo: dict, resolve) -> int:
    """Remap pattern storageNode ids in *topo* in place. Returns count changed.

    ``resolve(pattern_id, current_id)`` returns the correct ``PatternDisk.id``
    or ``None`` when the reference is already correct or unresolvable.
    """
    changed = 0
    for node in (topo or {}).get("nodes", []):
        if node.get("type") != "storageNode":
            continue
        data = node.get("data", {})
        if data.get("source") != "pattern":
            continue
        current = data.get("patternDiskId")
        if not current:
            continue
        new_id = resolve(data.get("patternId"), current)
        if new_id and new_id != current:
            data["patternDiskId"] = new_id
            changed += 1
    return changed


def _build_resolver(disks: list[PatternDisk]):
    """Build resolve(pattern_id, current_id) -> corrected PatternDisk.id | None."""
    valid_ids = {d.id for d in disks}
    by_pattern_source = {(d.pattern_id, d.source_disk_id): d.id for d in disks}

    source_to_ids: dict[str, set[str]] = {}
    for d in disks:
        source_to_ids.setdefault(d.source_disk_id, set()).add(d.id)
    by_source_unique = {
        src: next(iter(ids)) for src, ids in source_to_ids.items() if len(ids) == 1
    }

    def resolve(pattern_id, current_id):
        if current_id in valid_ids:
            return None  # already a real PatternDisk.id
        if pattern_id and (pattern_id, current_id) in by_pattern_source:
            return by_pattern_source[(pattern_id, current_id)]
        return by_source_unique.get(current_id)

    return resolve


def backfill_topology_pattern_disk_ids(db, dry_run: bool = False) -> dict:
    """Remap legacy content-UUID patternDiskIds to PatternDisk.id.

    Returns counts: ``patterns_fixed``, ``projects_fixed``, ``disks_remapped``.
    With ``dry_run=True`` nothing is persisted, but counts reflect what would
    change.
    """
    disks = db.scalars(select(PatternDisk)).all()
    resolve = _build_resolver(disks)

    patterns_fixed = 0
    projects_fixed = 0
    disks_remapped = 0

    for pat in db.scalars(select(Pattern)).all():
        n = _remap_topology(pat.topology or {}, resolve)
        if n:
            flag_modified(pat, "topology")
            patterns_fixed += 1
            disks_remapped += n

    for proj in db.scalars(select(Project)).all():
        n = _remap_topology(proj.topology or {}, resolve)
        if n:
            flag_modified(proj, "topology")
            projects_fixed += 1
            disks_remapped += n

    if dry_run:
        db.rollback()
    else:
        db.commit()

    return {
        "patterns_fixed": patterns_fixed,
        "projects_fixed": projects_fixed,
        "disks_remapped": disks_remapped,
    }


def run_backfill(dry_run: bool = False) -> None:
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        result = backfill_topology_pattern_disk_ids(db, dry_run=dry_run)
        log.info(
            "Topology patternDiskId backfill (%s): %s",
            "dry-run" if dry_run else "applied",
            result,
        )
    finally:
        db.close()


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    run_backfill(dry_run="--dry-run" in sys.argv)
