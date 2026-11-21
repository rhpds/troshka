"""Access window helpers and timer stop selection."""

import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services.access_window import (
    normalize_access_bounds,
    require_owner_access_window,
    require_user_access_window,
    user_within_access_window,
)
from app.services.project_timer import _process_expired_access_windows


def test_normalize_rejects_inverted_bounds():
    start = datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC)
    end = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)
    with pytest.raises(HTTPException) as exc:
        normalize_access_bounds(start, end)
    assert exc.value.status_code == 400


def test_require_user_access_window_raises_before_start():
    user = SimpleNamespace(
        access_starts_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.UTC),
        access_ends_at=None,
    )
    with pytest.raises(HTTPException) as exc:
        require_user_access_window(user, action="deploy")
    assert exc.value.status_code == 403
    assert "deploy" in str(exc.value.detail).lower()


def test_require_owner_access_window_uses_owner(monkeypatch):
    owner = SimpleNamespace(
        id="owner-1",
        access_starts_at=None,
        access_ends_at=datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC),
    )
    project = SimpleNamespace(owner_id="owner-1")
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = owner
    with pytest.raises(HTTPException) as exc:
        require_owner_access_window(db, project, action="start")
    assert exc.value.status_code == 403


def test_process_expired_access_windows_selects_active_owned(monkeypatch):
    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)
    project = SimpleNamespace(
        id="proj-1",
        name="lab",
        owner_id="owner-1",
        state="active",
        auto_stopped=False,
    )
    session = MagicMock()
    session.query.return_value.join.return_value.filter.return_value.all.return_value = [
        project
    ]
    result = {"access_ended_stop": []}
    with patch(
        "app.services.project_timer._timer_quota_preflight", return_value=True
    ), patch("app.services.project_timer._notify"), patch(
        "app.services.project_timer._queue_timer_stop"
    ) as queue:
        _process_expired_access_windows(session, now, result, _dry_run=False)
    assert result["access_ended_stop"] == ["proj-1"]
    assert project.state == "stopping"
    assert project.auto_stopped is True
    queue.assert_called_once()


def test_process_expired_access_windows_dry_run_does_not_mutate():
    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)
    project = SimpleNamespace(
        id="proj-1",
        name="lab",
        owner_id="owner-1",
        state="active",
        auto_stopped=False,
    )
    session = MagicMock()
    session.query.return_value.join.return_value.filter.return_value.all.return_value = [
        project
    ]
    result = {"access_ended_stop": []}
    _process_expired_access_windows(session, now, result, _dry_run=True)
    assert result["access_ended_stop"] == ["proj-1"]
    assert project.state == "active"
    assert user_within_access_window(
        SimpleNamespace(access_starts_at=None, access_ends_at=None)
    )
