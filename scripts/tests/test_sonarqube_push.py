import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError


SPEC = importlib.util.spec_from_file_location(
    "sonarqube_push", Path(__file__).parents[1] / "sonarqube_push.py"
)
sonar = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sonar)


class SonarPushTests(unittest.TestCase):
    def test_unreachable_server_skips_before_credentials_or_tests(self):
        with (
            patch.object(sonar, "api_json", side_effect=URLError("offline")),
            patch.object(sonar, "run_coverage") as coverage,
        ):
            self.assertEqual(sonar.check({}), 0)
            coverage.assert_not_called()

    def test_reachable_server_without_token_blocks(self):
        with patch.object(sonar, "api_json", return_value={"status": "UP"}):
            with self.assertRaisesRegex(sonar.CheckFailure, "SONAR_TOKEN"):
                sonar.check({})

    def test_http_error_is_not_treated_as_offline(self):
        error = HTTPError("https://sonar.example", 403, "Forbidden", {}, None)
        with patch.object(sonar, "api_json", side_effect=error):
            with self.assertRaisesRegex(sonar.CheckFailure, "403"):
                sonar.check({})

    def test_unhealthy_server_blocks(self):
        with patch.object(sonar, "api_json", return_value={"status": "DOWN"}):
            with self.assertRaisesRegex(sonar.CheckFailure, "DOWN"):
                sonar.check({})

    def test_tls_verification_failure_blocks(self):
        error = URLError(sonar.ssl.SSLCertVerificationError("untrusted certificate"))
        with patch.object(sonar, "api_json", side_effect=error):
            with self.assertRaisesRegex(sonar.CheckFailure, "TLS"):
                sonar.check({})

    def test_non_head_push_blocks(self):
        with patch.object(sonar, "git", return_value="abc"):
            with self.assertRaisesRegex(sonar.CheckFailure, "checkout"):
                sonar.target_revision(Path("."), {"PRE_COMMIT_TO_REF": "def"})

    def test_dirty_tracked_files_block(self):
        with patch.object(sonar, "git", side_effect=[" M source.py", ""]):
            with self.assertRaisesRegex(sonar.CheckFailure, "uncommitted"):
                sonar.require_clean_sources(Path("."))

    def test_untracked_source_files_block(self):
        with patch.object(sonar, "git", side_effect=["", "src/operator/new.py"]):
            with self.assertRaisesRegex(sonar.CheckFailure, "untracked"):
                sonar.require_clean_sources(Path("."))

    def test_command_failure_blocks(self):
        with patch.object(
            sonar.subprocess,
            "run",
            return_value=sonar.subprocess.CompletedProcess([], 1),
        ):
            with self.assertRaisesRegex(sonar.CheckFailure, "failed"):
                sonar.run_command(["scanner"], Path("."), {}, 10)

    def test_command_timeout_blocks(self):
        with patch.object(
            sonar.subprocess,
            "run",
            side_effect=sonar.subprocess.TimeoutExpired("scanner", 10),
        ):
            with self.assertRaisesRegex(sonar.CheckFailure, "timed out"):
                sonar.run_command(["scanner"], Path("."), {}, 10)

    def test_scanner_uses_env_token_and_only_fresh_reports(self):
        env = {
            "SONAR_TOKEN": "unit-test-value",
            "SONAR_HOST_URL": "https://sonar.example",
        }
        with patch.object(sonar, "run_command") as run:
            sonar.run_scanner(Path("/repo"), Path("/reports"), "abc", env)
        command = run.call_args.args[0]
        self.assertNotIn(env["SONAR_TOKEN"], " ".join(command))
        self.assertIn("-Dsonar.scm.revision=abc", command)
        self.assertIn("-Dsonar.qualitygate.wait=true", command)
        reports = next(
            arg
            for arg in command
            if arg.startswith("-Dsonar.python.coverage.reportPaths=")
        )
        self.assertNotIn("*", reports)
        self.assertIn("/reports/operator-coverage.xml", reports)

    def test_gate_is_verified_for_submitted_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            (reports / "scanner").mkdir()
            (reports / "scanner" / "report-task.txt").write_text("ceTaskId=task-123\n")
            responses = [
                {"task": {"status": "SUCCESS", "analysisId": "analysis-123"}},
                {"analyses": [{"key": "analysis-123", "revision": "abc"}]},
                {"projectStatus": {"status": "OK"}},
            ]
            with patch.object(sonar, "api_json", side_effect=responses) as api:
                sonar.verify_analysis(reports, "abc", {})
            self.assertIn("analysisId=analysis-123", api.call_args.args[0])

    def test_other_revision_cannot_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            (reports / "scanner").mkdir()
            (reports / "scanner" / "report-task.txt").write_text("ceTaskId=task-123\n")
            with patch.object(
                sonar,
                "api_json",
                side_effect=[
                    {"task": {"status": "SUCCESS", "analysisId": "analysis-123"}},
                    {"analyses": [{"key": "analysis-123", "revision": "wrong"}]},
                ],
            ):
                with self.assertRaisesRegex(sonar.CheckFailure, "revision"):
                    sonar.verify_analysis(reports, "abc", {})

    def test_default_url_is_internal_instance(self):
        self.assertEqual(sonar.server_url({}), "https://sonarqube.corp.redhat.com")


if __name__ == "__main__":
    unittest.main()
