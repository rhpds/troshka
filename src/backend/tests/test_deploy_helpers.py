"""Tests for extracted helper functions in deploy_service.py."""

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ["TROSHKA_DATABASE__URL"] = "sqlite:///./test.db"

from app.services.deploy_service import (
    _is_api_error,
    _parse_node_readiness,
    _parse_operator_status,
)

# ---------------------------------------------------------------------------
# _parse_node_readiness
# ---------------------------------------------------------------------------


class TestParseNodeReadiness(unittest.TestCase):
    def test_none_input(self):
        items, ready, total = _parse_node_readiness(None)
        self.assertEqual(items, [])
        self.assertEqual(ready, 0)
        self.assertEqual(total, 0)

    def test_empty_string(self):
        items, ready, total = _parse_node_readiness("")
        self.assertEqual(items, [])
        self.assertEqual(ready, 0)
        self.assertEqual(total, 0)

    def test_single_ready_node(self):
        output = "master-0   Ready    control-plane   10d   v1.28.6"
        items, ready, total = _parse_node_readiness(output)
        self.assertEqual(total, 1)
        self.assertEqual(ready, 1)
        self.assertEqual(len(items), 1)
        self.assertIn("master-0: Ready", items[0])

    def test_mixed_ready_and_not_ready(self):
        output = (
            "master-0   Ready      control-plane   10d   v1.28.6\n"
            "worker-0   NotReady   worker          2d    v1.28.6"
        )
        items, ready, total = _parse_node_readiness(output)
        self.assertEqual(total, 2)
        self.assertEqual(ready, 1)
        self.assertEqual(len(items), 2)
        self.assertIn("master-0: Ready", items[0])
        self.assertIn("worker-0: NotReady", items[1])

    def test_all_ready(self):
        output = (
            "master-0   Ready   control-plane   10d   v1.28.6\n"
            "master-1   Ready   control-plane   10d   v1.28.6\n"
            "master-2   Ready   control-plane   10d   v1.28.6"
        )
        items, ready, total = _parse_node_readiness(output)
        self.assertEqual(total, 3)
        self.assertEqual(ready, 3)

    def test_trailing_newline_ignored(self):
        output = "node1   Ready   worker   5d   v1.28.6\n"
        items, ready, total = _parse_node_readiness(output)
        self.assertEqual(total, 1)
        self.assertEqual(ready, 1)

    def test_blank_lines_skipped(self):
        output = "\n\nnode1   Ready   worker   5d   v1.28.6\n\n"
        items, ready, total = _parse_node_readiness(output)
        self.assertEqual(total, 1)
        self.assertEqual(ready, 1)

    def test_single_word_line_skipped(self):
        # A line with fewer than 2 parts is ignored
        output = "orphan"
        items, ready, total = _parse_node_readiness(output)
        self.assertEqual(total, 0)
        self.assertEqual(ready, 0)
        self.assertEqual(items, [])


# ---------------------------------------------------------------------------
# _parse_operator_status
# ---------------------------------------------------------------------------


class TestParseOperatorStatus(unittest.TestCase):
    def test_none_input(self):
        items, avail, total = _parse_operator_status(None)
        self.assertEqual(items, [])
        self.assertEqual(avail, 0)
        self.assertEqual(total, 0)

    def test_empty_string(self):
        items, avail, total = _parse_operator_status("")
        self.assertEqual(items, [])
        self.assertEqual(avail, 0)
        self.assertEqual(total, 0)

    def test_all_available(self):
        # oc get co --no-headers columns: NAME VERSION AVAILABLE PROGRESSING DEGRADED
        output = (
            "authentication   4.14.0   True   False   False\n"
            "console          4.14.0   True   False   False"
        )
        items, avail, total = _parse_operator_status(output)
        self.assertEqual(total, 2)
        self.assertEqual(avail, 2)
        self.assertIn("authentication: available", items[0])
        self.assertIn("console: available", items[1])

    def test_degraded_operator(self):
        output = "kube-apiserver   4.14.0   False   False   True"
        items, avail, total = _parse_operator_status(output)
        self.assertEqual(total, 1)
        self.assertEqual(avail, 0)
        self.assertIn("kube-apiserver: degraded", items[0])

    def test_progressing_operator(self):
        # Not available, not degraded → progressing
        output = "etcd   4.14.0   False   True   False"
        items, avail, total = _parse_operator_status(output)
        self.assertEqual(total, 1)
        self.assertEqual(avail, 0)
        self.assertIn("etcd: progressing", items[0])

    def test_mixed_statuses(self):
        output = (
            "authentication   4.14.0   True    False   False\n"
            "etcd             4.14.0   False   True    False\n"
            "kube-apiserver   4.14.0   False   False   True"
        )
        items, avail, total = _parse_operator_status(output)
        self.assertEqual(total, 3)
        self.assertEqual(avail, 1)
        self.assertEqual(items[0], "authentication: available")
        self.assertEqual(items[1], "etcd: progressing")
        self.assertEqual(items[2], "kube-apiserver: degraded")

    def test_line_with_fewer_than_4_parts_skipped(self):
        output = "too   few   cols"
        items, avail, total = _parse_operator_status(output)
        self.assertEqual(total, 0)
        self.assertEqual(items, [])

    def test_no_degraded_column_defaults_to_false(self):
        # Only 4 columns (no degraded) — should label as progressing since avail is False
        output = "dns   4.14.0   False   True"
        items, avail, total = _parse_operator_status(output)
        self.assertEqual(total, 1)
        self.assertEqual(avail, 0)
        # degraded defaults to "False" when missing, so it's "progressing"
        self.assertEqual(items[0], "dns: progressing")


# ---------------------------------------------------------------------------
# _is_api_error
# ---------------------------------------------------------------------------


class TestIsApiError(unittest.TestCase):
    def test_none_returns_true(self):
        self.assertTrue(_is_api_error(None))

    def test_empty_string_returns_true(self):
        self.assertTrue(_is_api_error(""))

    def test_error_keyword(self):
        self.assertTrue(_is_api_error("Error from server"))

    def test_refused_keyword(self):
        self.assertTrue(_is_api_error("connection refused"))

    def test_connection_keyword(self):
        self.assertTrue(_is_api_error("Unable to establish connection"))

    def test_normal_output_returns_false(self):
        self.assertFalse(_is_api_error("3 nodes ready"))

    def test_case_insensitive(self):
        self.assertTrue(_is_api_error("ERROR: timeout"))
        self.assertTrue(_is_api_error("Connection reset"))
        self.assertTrue(_is_api_error("REFUSED by peer"))

    def test_clean_output_no_false_positive(self):
        self.assertFalse(
            _is_api_error(
                "NAME          STATUS   ROLES    AGE   VERSION\n"
                "master-0      Ready    master   10d   v1.28.6"
            )
        )


# ---------------------------------------------------------------------------
# _resolve_monitor_context — requires DB mocking
# ---------------------------------------------------------------------------


class TestResolveMonitorContext(unittest.TestCase):
    @patch("app.core.database.SessionLocal")
    def test_project_not_found(self, mock_session_cls):
        from app.services.deploy_service import _resolve_monitor_context

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        mock_session_cls.return_value = mock_db

        result = _resolve_monitor_context("nonexistent-project-id")
        self.assertIsNone(result)
        mock_db.close.assert_called_once()

    @patch("app.core.database.SessionLocal")
    def test_project_wrong_state(self, mock_session_cls):
        from app.services.deploy_service import _resolve_monitor_context

        project = MagicMock()
        project.state = "deploying"
        project.ocp_status = "monitoring"

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = project
        mock_session_cls.return_value = mock_db

        result = _resolve_monitor_context("some-project-id")
        self.assertIsNone(result)

    @patch("app.core.database.SessionLocal")
    def test_project_wrong_ocp_status(self, mock_session_cls):
        from app.services.deploy_service import _resolve_monitor_context

        project = MagicMock()
        project.state = "active"
        project.ocp_status = "installing"

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = project
        mock_session_cls.return_value = mock_db

        result = _resolve_monitor_context("some-project-id")
        self.assertIsNone(result)

    @patch("app.services.deploy_service._has_ocp_monitor", return_value=True)
    @patch("app.core.database.SessionLocal")
    def test_host_not_found(self, mock_session_cls, _mock_has):
        from app.services.deploy_service import _resolve_monitor_context

        project = MagicMock()
        project.id = "proj-1234"
        project.state = "active"
        project.ocp_status = "monitoring"
        project.host_id = "host-missing"

        mock_db = MagicMock()

        def fake_query(model):
            m = MagicMock()
            if model.__name__ == "Project":
                m.filter_by.return_value.first.return_value = project
            else:
                m.filter_by.return_value.first.return_value = None
            return m

        mock_db.query.side_effect = fake_query
        mock_session_cls.return_value = mock_db

        result = _resolve_monitor_context("proj-1234")
        self.assertIsNone(result)

    @patch("app.services.deploy_service._has_ocp_monitor", return_value=True)
    @patch("app.core.database.SessionLocal")
    def test_success_returns_tuple(self, mock_session_cls, _mock_has):
        from app.services.deploy_service import _resolve_monitor_context

        project = MagicMock()
        project.id = "proj-1234"
        project.state = "active"
        project.ocp_status = "monitoring"
        project.host_id = "host-5678"
        project.deployed_topology = {
            "nodes": [{"type": "vmNode", "data": {"ocpMonitor": True}}]
        }
        project.topology = None
        project.ocp_install_elapsed = None
        project.deploy_started_at = MagicMock()
        project.deploy_started_at.timestamp.return_value = 1700000000.0
        project.ocp_monitor_started_at = None

        host = MagicMock()
        host.id = "host-5678"
        host.host_type = "ec2"
        host.agent_status = "connected"

        mock_db = MagicMock()

        def fake_query(model):
            m = MagicMock()
            if model.__name__ == "Project":
                m.filter_by.return_value.first.return_value = project
            else:
                m.filter_by.return_value.first.return_value = host
            return m

        mock_db.query.side_effect = fake_query
        mock_session_cls.return_value = mock_db

        result = _resolve_monitor_context("proj-1234")
        self.assertIsNotNone(result)
        r_project, r_host, r_topo, r_deploy_start = result
        self.assertEqual(r_project.id, "proj-1234")
        self.assertEqual(r_host.id, "host-5678")
        self.assertEqual(r_deploy_start, 1700000000.0)

    @patch("app.services.deploy_service._has_ocp_monitor", return_value=True)
    @patch("app.core.database.SessionLocal")
    def test_host_disconnected_skipped(self, mock_session_cls, _mock_has):
        from app.services.deploy_service import _resolve_monitor_context

        project = MagicMock()
        project.id = "proj-1234"
        project.state = "active"
        project.ocp_status = "monitoring"
        project.host_id = "host-5678"

        host = MagicMock()
        host.id = "host-5678"
        host.host_type = "ec2"
        host.agent_status = "disconnected"

        mock_db = MagicMock()

        def fake_query(model):
            m = MagicMock()
            if model.__name__ == "Project":
                m.filter_by.return_value.first.return_value = project
            else:
                m.filter_by.return_value.first.return_value = host
            return m

        mock_db.query.side_effect = fake_query
        mock_session_cls.return_value = mock_db

        result = _resolve_monitor_context("proj-1234")
        self.assertIsNone(result)

    @patch("app.services.deploy_service._has_ocp_monitor", return_value=True)
    @patch("app.core.database.SessionLocal")
    def test_kubevirt_cluster_skips_agent_check(self, mock_session_cls, _mock_has):
        from app.services.deploy_service import _resolve_monitor_context

        project = MagicMock()
        project.id = "proj-1234"
        project.state = "active"
        project.ocp_status = "monitoring"
        project.host_id = "host-5678"
        project.deployed_topology = {
            "nodes": [{"type": "vmNode", "data": {"ocpMonitor": True}}]
        }
        project.topology = None
        project.ocp_install_elapsed = None
        project.deploy_started_at = None
        project.ocp_monitor_started_at = MagicMock()
        project.ocp_monitor_started_at.timestamp.return_value = 1700000500.0

        host = MagicMock()
        host.id = "host-5678"
        host.host_type = "kubevirt-cluster"
        host.agent_status = "disconnected"  # should be ignored for kubevirt

        mock_db = MagicMock()

        def fake_query(model):
            m = MagicMock()
            if model.__name__ == "Project":
                m.filter_by.return_value.first.return_value = project
            else:
                m.filter_by.return_value.first.return_value = host
            return m

        mock_db.query.side_effect = fake_query
        mock_session_cls.return_value = mock_db

        result = _resolve_monitor_context("proj-1234")
        self.assertIsNotNone(result)
        _, _, _, deploy_start = result
        self.assertEqual(deploy_start, 1700000500.0)

    @patch("app.services.deploy_service._has_ocp_monitor", return_value=True)
    @patch("app.core.database.SessionLocal")
    def test_already_completed_skipped(self, mock_session_cls, _mock_has):
        from app.services.deploy_service import _resolve_monitor_context

        project = MagicMock()
        project.id = "proj-1234"
        project.state = "active"
        project.ocp_status = "monitoring"
        project.host_id = "host-5678"
        project.deployed_topology = {
            "nodes": [{"type": "vmNode", "data": {"ocpMonitor": True}}]
        }
        project.topology = None
        project.ocp_install_elapsed = 3600  # already completed

        host = MagicMock()
        host.host_type = "ec2"
        host.agent_status = "connected"

        mock_db = MagicMock()

        def fake_query(model):
            m = MagicMock()
            if model.__name__ == "Project":
                m.filter_by.return_value.first.return_value = project
            else:
                m.filter_by.return_value.first.return_value = host
            return m

        mock_db.query.side_effect = fake_query
        mock_session_cls.return_value = mock_db

        result = _resolve_monitor_context("proj-1234")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _verify_bastion_browser — uses callable args, mock time.sleep
# ---------------------------------------------------------------------------


class TestVerifyBastionBrowser(unittest.TestCase):
    @patch("time.sleep", return_value=None)
    def test_immediately_ready(self, _mock_sleep):
        from app.services.deploy_service import _verify_bastion_browser

        exec_fn = MagicMock(return_value="ca:ok\nlogins:ok")
        push_fn = MagicMock()

        result = _verify_bastion_browser(exec_fn, push_fn, "proj-1234", "bastion")
        self.assertTrue(result)
        exec_fn.assert_called_once()

    @patch("time.sleep", return_value=None)
    def test_stale_ca_triggers_update(self, _mock_sleep):
        from app.services.deploy_service import _verify_bastion_browser

        # First call: stale CA + stale logins; second call: all ok
        exec_fn = MagicMock(
            side_effect=[
                "ca:stale\nlogins:stale",  # verify
                "updated ca",  # CA update cmd
                "",  # kill browser
                "autologin done",  # autologin cmd
                "ca:ok\nlogins:ok",  # verify again
            ]
        )
        push_fn = MagicMock()

        result = _verify_bastion_browser(exec_fn, push_fn, "proj-1234")
        self.assertTrue(result)
        # push_fn should have been called with stale message
        push_fn.assert_any_call(
            "browser", "bastion CA cert, browser credentials stale, updating..."
        )

    @patch("time.sleep", return_value=None)
    def test_pending_waits_then_succeeds(self, _mock_sleep):
        from app.services.deploy_service import _verify_bastion_browser

        # First call: ca:pending (no fix needed, just wait); second: ok
        exec_fn = MagicMock(
            side_effect=[
                "ca:pending\nlogins:missing",  # verify → logins:missing triggers fix
                "",  # kill browser
                "autologin done",  # autologin cmd
                "ca:ok\nlogins:ok",  # verify again
            ]
        )
        push_fn = MagicMock()

        result = _verify_bastion_browser(exec_fn, push_fn, "proj-1234", "bastion")
        self.assertTrue(result)

    @patch("time.sleep", return_value=None)
    def test_kills_browser_before_autologin(self, _mock_sleep):
        from app.services.deploy_service import (
            _KILL_BROWSER_CMD,
            _verify_bastion_browser,
        )

        exec_fn = MagicMock(
            side_effect=[
                "ca:ok\nlogins:missing",
                "",
                "autologin done",
                "ca:ok\nlogins:ok",
            ]
        )
        push_fn = MagicMock()

        result = _verify_bastion_browser(exec_fn, push_fn, "proj-1234")
        self.assertTrue(result)
        calls = [c[0][0] for c in exec_fn.call_args_list]
        self.assertIn(_KILL_BROWSER_CMD, calls)
        kill_idx = calls.index(_KILL_BROWSER_CMD)
        autologin_idx = next(
            i for i, cmd in enumerate(calls) if "ocp-autologin.py" in cmd
        )
        self.assertLess(kill_idx, autologin_idx)

    @patch("time.sleep", return_value=None)
    def test_stale_logins_when_password_newer(self, _mock_sleep):
        from app.services.deploy_service import _verify_bastion_browser

        exec_fn = MagicMock(
            side_effect=[
                "ca:ok\nlogins:stale",
                "",
                "Password saved to Firefox",
                "ca:ok\nlogins:ok",
            ]
        )
        push_fn = MagicMock()

        result = _verify_bastion_browser(exec_fn, push_fn, "proj-1234")
        self.assertTrue(result)
        push_fn.assert_any_call(
            "browser", "bastion browser credentials stale, updating..."
        )

    @patch("time.sleep", return_value=None)
    def test_timeout_returns_false(self, _mock_sleep):
        from app.services.deploy_service import _verify_bastion_browser

        # Never returns ok — always pending with no fixable items
        exec_fn = MagicMock(return_value="ca:pending\nlogins:pending_something")
        push_fn = MagicMock()

        result = _verify_bastion_browser(exec_fn, push_fn, "proj-1234")
        self.assertFalse(result)
        # Should have been called 18 times (max retries)
        self.assertEqual(exec_fn.call_count, 18)

    @patch("time.sleep", return_value=None)
    def test_exec_fn_returns_none(self, _mock_sleep):
        from app.services.deploy_service import _verify_bastion_browser

        # exec_fn always returns None (command failure)
        exec_fn = MagicMock(return_value=None)
        push_fn = MagicMock()

        result = _verify_bastion_browser(exec_fn, push_fn, "proj-1234")
        self.assertFalse(result)
        self.assertEqual(exec_fn.call_count, 18)


if __name__ == "__main__":
    unittest.main()
