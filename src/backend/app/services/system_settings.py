"""Global admin-tunable settings persisted in ``system_config``."""

from __future__ import annotations

INGRESS_CONTROLLER_KEY = "openshift.ingress_controller"
DEFAULT_INGRESS_CONTROLLER = "default"


def get_ingress_controller_name(db=None) -> str:
    """Return the configured OpenShift IngressController name (default: ``default``)."""
    own_session = db is None
    if own_session:
        from app.core.database import SessionLocal

        db = SessionLocal()
    try:
        from app.models.system_config import SystemConfig

        row = db.query(SystemConfig).filter_by(key=INGRESS_CONTROLLER_KEY).first()
        name = (row.value if row else "") or DEFAULT_INGRESS_CONTROLLER
        return name.strip() or DEFAULT_INGRESS_CONTROLLER
    finally:
        if own_session:
            db.close()


def set_ingress_controller_name(db, name: str) -> str:
    """Persist the IngressController name. Empty/whitespace → ``default``."""
    from app.models.system_config import SystemConfig

    cleaned = (name or "").strip() or DEFAULT_INGRESS_CONTROLLER
    row = db.query(SystemConfig).filter_by(key=INGRESS_CONTROLLER_KEY).first()
    if row:
        row.value = cleaned
    else:
        db.add(SystemConfig(key=INGRESS_CONTROLLER_KEY, value=cleaned))
    db.commit()
    return cleaned
