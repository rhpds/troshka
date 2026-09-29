"""Pre-deploy pull-through registry reachability / auth probe."""

from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest


def test_probe_succeeds_on_http_200():
    from app.services.ocp.pull_through_registry import probe_pull_through_registry

    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.status = 200

    with patch(
        "app.services.ocp.pull_through_registry.urllib.request.urlopen",
        return_value=resp,
    ) as urlopen:
        probe_pull_through_registry("registry.example.com", "user", "pass")
        req = urlopen.call_args[0][0]
        assert req.full_url == "https://registry.example.com/v2/"
        assert req.get_header("Authorization").startswith("Basic ")


def test_probe_succeeds_via_bearer_challenge():
    from app.services.ocp.pull_through_registry import probe_pull_through_registry

    challenge = HTTPError(
        "https://registry.example.com/v2/",
        401,
        "Unauthorized",
        hdrs={
            "WWW-Authenticate": (
                'Bearer realm="https://registry.example.com/v2/auth",'
                'service="registry.example.com"'
            )
        },
        fp=None,
    )
    token_resp = MagicMock()
    token_resp.__enter__ = MagicMock(return_value=token_resp)
    token_resp.__exit__ = MagicMock(return_value=False)
    token_resp.status = 200
    token_resp.read = MagicMock(return_value=b'{"token":"abc"}')

    v2_ok = MagicMock()
    v2_ok.__enter__ = MagicMock(return_value=v2_ok)
    v2_ok.__exit__ = MagicMock(return_value=False)
    v2_ok.status = 200

    with patch(
        "app.services.ocp.pull_through_registry.urllib.request.urlopen",
        side_effect=[challenge, token_resp, v2_ok],
    ) as urlopen:
        probe_pull_through_registry("registry.example.com", "user", "pass")
    urls = [c[0][0].full_url for c in urlopen.call_args_list]
    assert urls[0] == "https://registry.example.com/v2/"
    assert urls[1].startswith("https://registry.example.com/v2/auth?")
    assert "service=registry.example.com" in urls[1]
    assert urlopen.call_args_list[2][0][0].get_header("Authorization") == "Bearer abc"


def test_probe_raises_on_unauthorized():
    from app.services.ocp.pull_through_registry import (
        PullThroughRegistryError,
        probe_pull_through_registry,
    )

    err = HTTPError(
        "https://registry.example.com/v2/",
        401,
        "Unauthorized",
        hdrs=None,
        fp=None,
    )
    with patch(
        "app.services.ocp.pull_through_registry.urllib.request.urlopen",
        side_effect=err,
    ):
        with pytest.raises(
            PullThroughRegistryError, match="401|Unauthorized|credential"
        ):
            probe_pull_through_registry("registry.example.com", "user", "bad")


def test_probe_raises_when_bearer_auth_rejects_credentials():
    from app.services.ocp.pull_through_registry import (
        PullThroughRegistryError,
        probe_pull_through_registry,
    )

    challenge = HTTPError(
        "https://registry.example.com/v2/",
        401,
        "Unauthorized",
        hdrs={
            "WWW-Authenticate": (
                'Bearer realm="https://registry.example.com/v2/auth",'
                'service="registry.example.com"'
            )
        },
        fp=None,
    )
    auth_fail = HTTPError(
        "https://registry.example.com/v2/auth",
        401,
        "Unauthorized",
        hdrs=None,
        fp=None,
    )
    with patch(
        "app.services.ocp.pull_through_registry.urllib.request.urlopen",
        side_effect=[challenge, auth_fail],
    ):
        with pytest.raises(PullThroughRegistryError, match="401|credential"):
            probe_pull_through_registry("registry.example.com", "user", "bad")


def test_probe_raises_on_connection_error():
    from app.services.ocp.pull_through_registry import (
        PullThroughRegistryError,
        probe_pull_through_registry,
    )

    with patch(
        "app.services.ocp.pull_through_registry.urllib.request.urlopen",
        side_effect=URLError("timed out"),
    ):
        with pytest.raises(
            PullThroughRegistryError, match="timed out|connect|unreachable"
        ):
            probe_pull_through_registry("registry.example.com", "user", "pass")


def test_check_returns_none_when_ptr_disabled():
    from app.services.ocp.pull_through_registry import (
        check_pull_through_registry_for_project,
    )

    owner = MagicMock(
        pull_through_registry=False,
        pull_through_registry_url=None,
    )
    project = MagicMock(owner_id="u1")
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = owner

    topology = {"clusters": [{"id": "c1", "installOnDeploy": True}]}
    assert check_pull_through_registry_for_project(db, project, topology) is None


def test_check_returns_none_when_clusters_opt_out():
    from app.services.ocp.pull_through_registry import (
        check_pull_through_registry_for_project,
    )

    owner = MagicMock(
        pull_through_registry=True,
        pull_through_registry_url="registry.example.com",
        pull_through_registry_user="user",
        pull_through_registry_password="enc",
    )
    project = MagicMock(owner_id="u1")
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = owner

    topology = {
        "clusters": [
            {"id": "c1", "installOnDeploy": True, "usePullThroughRegistry": False},
        ]
    }
    assert check_pull_through_registry_for_project(db, project, topology) is None


def test_check_returns_error_when_probe_fails():
    from app.services.ocp.pull_through_registry import (
        PullThroughRegistryError,
        check_pull_through_registry_for_project,
    )

    owner = MagicMock(
        pull_through_registry=True,
        pull_through_registry_url="registry.example.com",
        pull_through_registry_user="user",
        pull_through_registry_password="enc",
    )
    project = MagicMock(owner_id="u1")
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = owner

    topology = {"clusters": [{"id": "c1", "installOnDeploy": True}]}

    with (
        patch(
            "app.services.ocp.pull_through_registry.decrypt",
            return_value="secret",
        ),
        patch(
            "app.services.ocp.pull_through_registry.probe_pull_through_registry",
            side_effect=PullThroughRegistryError(
                "Pull-through registry unreachable or credentials invalid "
                "(https://registry.example.com): 401 Unauthorized. "
                "Update credentials in Settings → OCP Pull Secret."
            ),
        ),
    ):
        err = check_pull_through_registry_for_project(db, project, topology)
    assert err is not None
    assert "Pull-through registry" in err
    assert "Settings" in err


def test_check_restart_only_considers_named_cluster():
    from app.services.ocp.pull_through_registry import (
        check_pull_through_registry_for_project,
    )

    owner = MagicMock(
        pull_through_registry=True,
        pull_through_registry_url="registry.example.com",
        pull_through_registry_user="user",
        pull_through_registry_password="enc",
    )
    project = MagicMock(owner_id="u1")
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = owner

    topology = {
        "clusters": [
            {"id": "c1", "usePullThroughRegistry": False},
            {"id": "c2", "usePullThroughRegistry": True},
        ]
    }
    # Named cluster opted out — skip probe entirely.
    assert (
        check_pull_through_registry_for_project(db, project, topology, cluster_key="c1")
        is None
    )


def test_check_returns_none_when_probe_succeeds():
    from app.services.ocp.pull_through_registry import (
        check_pull_through_registry_for_project,
    )

    owner = MagicMock(
        pull_through_registry=True,
        pull_through_registry_url="registry.example.com",
        pull_through_registry_user="user",
        pull_through_registry_password="enc",
    )
    project = MagicMock(owner_id="u1")
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = owner
    topology = {"clusters": [{"id": "c1", "installOnDeploy": True}]}

    with (
        patch(
            "app.services.ocp.pull_through_registry.decrypt",
            return_value="secret",
        ),
        patch(
            "app.services.ocp.pull_through_registry.probe_pull_through_registry",
        ) as probe,
    ):
        assert check_pull_through_registry_for_project(db, project, topology) is None
        probe.assert_called_once_with("registry.example.com", "user", "secret")


def test_check_returns_error_when_credentials_missing():
    from app.services.ocp.pull_through_registry import (
        check_pull_through_registry_for_project,
    )

    owner = MagicMock(
        pull_through_registry=True,
        pull_through_registry_url="registry.example.com",
        pull_through_registry_user="",
        pull_through_registry_password=None,
    )
    project = MagicMock(owner_id="u1")
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = owner
    topology = {"clusters": [{"id": "c1"}]}

    err = check_pull_through_registry_for_project(db, project, topology)
    assert err is not None
    assert "missing" in err.lower()
