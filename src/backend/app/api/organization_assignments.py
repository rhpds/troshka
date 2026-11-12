"""One transaction and private error contract for membership entry points."""

from datetime import UTC, datetime

from fastapi import HTTPException

from app.schemas.quota import AccountingUnavailable, QuotaDenied
from app.services.organization_service import transfer_user


def commit_assignment(db, actor, user_id, organization_id, *, expected_source=None):
    try:
        result = transfer_user(
            db,
            actor,
            user_id,
            organization_id,
            datetime.now(UTC),
            expected_source=expected_source,
        )
        db.commit()
        return result
    except QuotaDenied as exc:
        db.rollback()
        raise HTTPException(
            409,
            {
                "reason": exc.reason,
                "scope": exc.scope,
                "dimension": exc.dimension,
                "used": str(exc.used),
                "reserved": str(exc.reserved),
                "requested": str(exc.requested),
                "limit": str(exc.limit),
            },
        ) from exc
    except AccountingUnavailable as exc:
        db.rollback()
        raise HTTPException(
            503, {"reason": "accounting_unavailable", "message": str(exc)}
        ) from exc
    except Exception:
        db.rollback()
        raise
