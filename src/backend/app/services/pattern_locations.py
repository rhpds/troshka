"""Pattern disk location predicates.

Single source of truth for deciding where each pattern disk can be sourced
from on a given cluster: the cluster's local RGW/OBC, central S4
(`troshka-images`), or neither. Reused by placement and deploy so both apply
identical "all disks or not ready" logic.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.pattern_location import PatternLocation


def pattern_disk_ids_from_topology(topology: dict) -> list[str]:
    """Return patternDiskId for every pattern-sourced storageNode in a topology."""
    ids: list[str] = []
    for node in (topology or {}).get("nodes", []):
        if node.get("type") != "storageNode":
            continue
        data = node.get("data", {})
        if data.get("source") == "pattern" and data.get("patternDiskId"):
            ids.append(data["patternDiskId"])
    return ids


def pattern_disk_source_for_cluster(
    db: Session, pattern_disk_id: str, target_provider_id: str | None
) -> str | None:
    """Where can this disk be sourced from on target_provider_id?

    Returns "obc" (local RGW on that provider), "central" (S4 troshka-images),
    or None if the disk is not synced anywhere reachable from that cluster.
    OBC on the target provider is preferred over central.
    """
    if target_provider_id:
        obc = (
            db.query(PatternLocation)
            .filter_by(
                pattern_disk_id=pattern_disk_id,
                provider_id=target_provider_id,
                location_type="obc",
                state="synced",
            )
            .first()
        )
        if obc:
            return "obc"
    central = (
        db.query(PatternLocation)
        .filter_by(
            pattern_disk_id=pattern_disk_id,
            location_type="central",
            state="synced",
        )
        .first()
    )
    if central:
        return "central"
    return None


def pattern_disks_ready_on_provider(
    db: Session, pattern_disk_ids: list[str], target_provider_id: str | None
) -> bool:
    """True iff every disk resolves to a non-None source on target_provider_id."""
    if not pattern_disk_ids:
        return True
    return all(
        pattern_disk_source_for_cluster(db, did, target_provider_id) is not None
        for did in pattern_disk_ids
    )
