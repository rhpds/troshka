import datetime

from app.core.auth import hash_password
from app.models.project import Project
from app.models.user import User
from tests.conftest import TestSession

_db = TestSession()
_user = User(
    email="timer-test@example.com",
    display_name="Timer Test",
    role="user",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user)
_db.commit()
_db.refresh(_user)
USER_ID = _user.id
_db.close()


def _create_project(name, **kwargs):
    db = TestSession()
    p = Project(name=name, owner_id=USER_ID, **kwargs)
    db.add(p)
    db.commit()
    db.refresh(p)
    pid = p.id
    db.close()
    return pid


def test_check_timers_fires_auto_stop():
    """Projects with expired auto_stop_expires_at and state=active get stopped."""
    from app.services.project_timer import _check_project_timers

    now = datetime.datetime.now(datetime.UTC)
    pid = _create_project(
        "Auto Stop Test",
        state="active",
        auto_stop_minutes=60,
        auto_stop_started_at=now - datetime.timedelta(hours=2),
        auto_stop_expires_at=now - datetime.timedelta(hours=1),
    )

    stopped_ids = _check_project_timers(_dry_run=True)

    assert pid in stopped_ids["auto_stop"]

    # Clean up
    db = TestSession()
    db.query(Project).filter_by(id=pid).delete()
    db.commit()
    db.close()


def test_check_timers_fires_auto_delete():
    """Projects with expired lifetime_expires_at get deleted."""
    from app.services.project_timer import _check_project_timers

    now = datetime.datetime.now(datetime.UTC)
    pid = _create_project(
        "Auto Delete Test",
        state="stopped",
        auto_delete_minutes=60,
        auto_delete_started_at=now - datetime.timedelta(hours=2),
        lifetime_expires_at=now - datetime.timedelta(hours=1),
    )

    result = _check_project_timers(_dry_run=True)

    assert pid in result["auto_delete"]

    # Clean up
    db = TestSession()
    db.query(Project).filter_by(id=pid).delete()
    db.commit()
    db.close()


def test_check_timers_skips_transitional_states():
    """Projects in deploying/stopping/starting/reconfiguring are skipped."""
    from app.services.project_timer import _check_project_timers

    now = datetime.datetime.now(datetime.UTC)
    pid = _create_project(
        "Transitional Test",
        state="deploying",
        auto_stop_minutes=60,
        auto_stop_started_at=now - datetime.timedelta(hours=2),
        auto_stop_expires_at=now - datetime.timedelta(hours=1),
    )

    result = _check_project_timers(_dry_run=True)

    assert pid not in result["auto_stop"]

    # Clean up
    db = TestSession()
    db.query(Project).filter_by(id=pid).delete()
    db.commit()
    db.close()


def test_check_timers_sends_warning():
    """Projects within 5 min of expiry get a warning flag set."""
    from app.services.project_timer import _check_project_timers

    now = datetime.datetime.now(datetime.UTC)
    pid = _create_project(
        "Warning Test",
        state="active",
        auto_stop_minutes=60,
        auto_stop_started_at=now - datetime.timedelta(minutes=57),
        auto_stop_expires_at=now + datetime.timedelta(minutes=3),
    )

    result = _check_project_timers(_dry_run=True)

    assert pid in result["auto_stop_warned"]

    # Clean up
    db = TestSession()
    db.query(Project).filter_by(id=pid).delete()
    db.commit()
    db.close()


def test_check_timers_no_double_warning():
    """Projects already warned are not warned again."""
    from app.services.project_timer import _check_project_timers

    now = datetime.datetime.now(datetime.UTC)
    pid = _create_project(
        "No Double Warn",
        state="active",
        auto_stop_minutes=60,
        auto_stop_started_at=now - datetime.timedelta(minutes=57),
        auto_stop_expires_at=now + datetime.timedelta(minutes=3),
        auto_stop_warned=True,
    )

    result = _check_project_timers(_dry_run=True)

    assert pid not in result["auto_stop_warned"]

    # Clean up
    db = TestSession()
    db.query(Project).filter_by(id=pid).delete()
    db.commit()
    db.close()
