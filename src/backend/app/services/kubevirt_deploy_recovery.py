"""Resume monitoring an existing native KubeVirt deployment without redeploying.

Enqueue ``resume_kubevirt_deploy_monitor`` with an RQ timeout of at least 10800
seconds and retain the project's RQ job reference for the same duration.
"""

import copy

from app.core.database import SessionLocal
from app.core.redis import delete_progress, set_progress
from app.models.host import Host
from app.models.project import Project
from app.models.provider import Provider
from app.services.deploy_service import (
    _deploy_after_special_host,
    _poll_kubevirt_deploy,
)
from app.services.providers import get_provider_driver
from app.services.ws_pubsub import notify_project

_RECOVERABLE_ERRORS = {
    "Deploy timed out waiting for operator (2 hours)",
    "Background job lost while deploying — please retry",
    "Task exceeded maximum timeout value (7200 seconds)",
}


def _load_recovery_context(db, project_id):
    project = db.query(Project).filter_by(id=project_id).first()
    if project is None:
        raise ValueError("Project does not exist")
    if project.state != "deploying" and not (
        project.state == "error" and project.deploy_error in _RECOVERABLE_ERRORS
    ):
        raise ValueError("Project is not an interrupted deployment")
    host = db.query(Host).filter_by(id=project.host_id).first()
    if host is None or host.host_type != "kubevirt-cluster":
        raise ValueError("Project must use an existing native KubeVirt host")
    provider = db.query(Provider).filter_by(id=host.provider_id).first()
    if provider is None:
        raise ValueError("No provider found for KubeVirt host")
    driver = get_provider_driver(provider)
    status = driver.get_project_status(provider, project_id)
    if not isinstance(status, dict) or status.get("phase") not in {
        "Deploying",
        "Running",
    }:
        raise ValueError(
            "Recovery requires an existing Deploying or Running project CR"
        )
    return project, host, provider, driver


def _record_monitor_failure(db, project, project_id, error):
    db.rollback()
    project.state = "error"
    project.deploy_error = str(error)
    db.commit()
    delete_progress(f"deploy:{project_id}")
    notify_project(
        project_id,
        {"type": "project-state", "state": "error", "deploy_error": str(error)},
    )


def resume_kubevirt_deploy_monitor(project_id: str):
    """Attach to the existing operator CR and finish the normal deploy hooks.

    This deliberately bypasses CR creation, disk preparation, credential
    generation, and topology initialization. Validation errors leave DB state
    untouched; failures after attaching become normal deployment errors.
    """
    db = SessionLocal()
    try:
        project, host, provider, driver = _load_recovery_context(db, project_id)
        progress = {
            "step": "vms",
            "detail": "Resuming monitoring of existing operator deployment",
            "percent": 0,
        }
        # Seed before committing deploying so the timer sees a live monitor.
        set_progress(f"deploy:{project_id}", progress, ttl=10800)
        project.state = "deploying"
        project.deploy_error = None
        project.deploy_progress = progress
        db.commit()
        try:
            notify_project(
                project_id,
                {"type": "project-state", "state": "deploying", "deploy_error": None},
            )
            topology = copy.deepcopy(project.topology or {})
            # Canvas saves scrub capabilities; resume with the deployed token.
            from app.services.showroom_scaffold import SHOWROOM_ACCESS_TOKEN_KEY

            deployed = getattr(project, "deployed_topology", None) or {}
            if deployed.get(SHOWROOM_ACCESS_TOKEN_KEY):
                topology[SHOWROOM_ACCESS_TOKEN_KEY] = deployed[
                    SHOWROOM_ACCESS_TOKEN_KEY
                ]
            _poll_kubevirt_deploy(project_id, project, provider, driver, topology, db)
            if project.state == "active":
                _deploy_after_special_host(
                    db, host, project_id, project, topology, project.vni_map or {}
                )
        except Exception as error:
            _record_monitor_failure(db, project, project_id, error)
            raise
    finally:
        db.close()
