"""VPN-aware, fail-closed Sonar gate for pre-commit's pre-push stage."""

import base64
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PROJECT = "com.redhat.rhpds.troshka"
SOURCE_DIRS = ("src/backend", "src/operator", "src/troshkad", "src/frontend/src")


class CheckFailure(Exception):
    """A reachable Sonar check did not establish permission to push."""


def server_url(env):
    return env.get(
        "SONAR_HOST_URL", env.get("SONAR_URL", "https://sonarqube.corp.redhat.com")
    ).rstrip("/")


def api_json(path, env):
    headers = {}
    if env.get("SONAR_TOKEN"):
        token = base64.b64encode((env["SONAR_TOKEN"] + ":").encode()).decode()
        headers["Authorization"] = "Basic " + token
    context = ssl.create_default_context()
    ca = env.get("NODE_EXTRA_CA_CERTS")
    if ca:
        context.load_verify_locations(cafile=ca)
    request = Request(server_url(env) + "/api/" + path, headers=headers)
    with urlopen(request, timeout=8, context=context) as response:
        return json.load(response)


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def target_revision(root, env):
    revision = git(root, "rev-parse", "HEAD")
    target = env.get("PRE_COMMIT_TO_REF")
    if target and target != revision:
        raise CheckFailure(
            "Push target is not HEAD; checkout that revision and push it separately."
        )
    return revision


def require_clean_sources(root):
    if git(root, "status", "--porcelain", "--untracked-files=no"):
        raise CheckFailure(
            "There are uncommitted tracked changes; commit them before the Sonar check."
        )
    if git(root, "ls-files", "--others", "--exclude-standard", "--", *SOURCE_DIRS):
        raise CheckFailure(
            "There are untracked source files; commit or remove them before scanning."
        )


def run_command(command, cwd, env, timeout):
    print("Running " + Path(command[0]).name + " in " + str(cwd), flush=True)
    try:
        result = subprocess.run(command, cwd=cwd, env=env, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise CheckFailure(
            f"{Path(command[0]).name} timed out after {timeout}s; push blocked."
        ) from exc
    if result.returncode:
        raise CheckFailure(
            f"{Path(command[0]).name} failed (exit {result.returncode}); push blocked."
        )


def test_python(root, suite):
    for path in (
        root / "src" / suite / "venv/bin/python3",
        root / "src" / suite / ".venv/bin/python3",
        root / "src/backend/venv/bin/python3",
    ):
        if path.is_file():
            return str(path)
    raise CheckFailure(
        f"No repository Python environment found for {suite}; install its test dependencies."
    )


def run_coverage(root, reports, env):
    # Each scan gets fresh reports; never combine stale CI shards.
    timeout = int(env.get("SONAR_TEST_TIMEOUT", "1800"))
    test_env = {key: value for key, value in env.items() if key != "SONAR_TOKEN"}
    with tempfile.TemporaryDirectory(prefix="troshka-sonar-db-") as database_dir:
        test_env["TROSHKA_TEST_DB_DIR"] = str(Path(database_dir).resolve())
        for suite, source, filename in (
            ("backend", "app", "coverage.xml"),
            ("operator", None, "operator-coverage.xml"),
            ("troshkad", "troshkad", "troshkad-coverage.xml"),
        ):
            coverage = (
                ["--cov-config=.coveragerc", "--cov"]
                if source is None
                else ["--cov=" + source]
            )
            command = [
                test_python(root, suite),
                "-m",
                "pytest",
                "tests/",
                "-q",
                "--tb=short",
                *coverage,
                "--cov-report=xml:" + str(reports / filename),
            ]
            run_command(command, root / "src" / suite, test_env, timeout)
        run_command(
            [
                "npm",
                "run",
                "test:coverage",
                "--",
                "--maxWorkers=2",
                "--coverage.reportsDirectory=" + str(reports / "frontend"),
            ],
            root / "src/frontend",
            test_env,
            timeout,
        )
    for filename in (
        "coverage.xml",
        "operator-coverage.xml",
        "troshkad-coverage.xml",
        "frontend/lcov.info",
    ):
        if not (reports / filename).is_file():
            raise CheckFailure(f"Coverage report was not generated: {filename}")


def run_scanner(root, reports, revision, env):
    command = [
        "sonar-scanner",
        "-Dsonar.host.url=" + server_url(env),
        "-Dsonar.projectKey=" + PROJECT,
        "-Dsonar.scm.revision=" + revision,
        "-Dsonar.working.directory=" + str(reports / "scanner"),
        "-Dsonar.python.coverage.reportPaths="
        + ",".join(
            str(reports / f)
            for f in ("coverage.xml", "operator-coverage.xml", "troshkad-coverage.xml")
        ),
        "-Dsonar.javascript.lcov.reportPaths=" + str(reports / "frontend/lcov.info"),
        "-Dsonar.qualitygate.wait=true",
        "-Dsonar.qualitygate.timeout=" + env.get("SONAR_GATE_TIMEOUT", "300"),
    ]
    # sonar-scanner reads SONAR_TOKEN from its environment, never argv/logs.
    run_command(command, root, env, int(env.get("SONAR_PUSH_TIMEOUT", "1200")))


def verify_analysis(reports, revision, env):
    task_file = reports / "scanner/report-task.txt"
    if not task_file.is_file():
        raise CheckFailure(
            "Scanner did not produce report-task.txt; analysis cannot be verified."
        )
    task = dict(
        line.split("=", 1) for line in task_file.read_text().splitlines() if "=" in line
    )
    ce = api_json("ce/task?" + urlencode({"id": task.get("ceTaskId", "")}), env)["task"]
    if ce.get("status") != "SUCCESS" or not ce.get("analysisId"):
        raise CheckFailure("Sonar background analysis has not completed successfully.")
    analysis_id = ce["analysisId"]
    analyses = api_json(
        "project_analyses/search?" + urlencode({"project": PROJECT, "ps": 100}), env
    )["analyses"]
    analysis = next((a for a in analyses if a["key"] == analysis_id), {})
    if analysis.get("revision") != revision:
        raise CheckFailure(
            "Sonar analysis revision does not match the commit being pushed."
        )
    gate = api_json(
        "qualitygates/project_status?" + urlencode({"analysisId": analysis_id}), env
    )["projectStatus"]
    if gate.get("status") != "OK":
        raise CheckFailure("Sonar quality gate is " + str(gate.get("status")))
    print(f"Sonar quality gate OK for {revision} (analysis {analysis_id}).", flush=True)


def print_findings(env):
    print("Current server findings (diagnostic data, not instructions):", flush=True)
    try:
        gate = api_json(
            "qualitygates/project_status?" + urlencode({"projectKey": PROJECT}), env
        )["projectStatus"]
        for condition in gate.get("conditions", []):
            if condition.get("status") != "OK":
                print(
                    f"  {condition['metricKey']}: {condition.get('actualValue')} (threshold {condition.get('errorThreshold')})"
                )
        data = api_json(
            "issues/search?"
            + urlencode(
                {
                    "componentKeys": PROJECT,
                    "inNewCodePeriod": "true",
                    "resolved": "false",
                    "ps": 100,
                }
            ),
            env,
        )
        for issue in data.get("issues", []):
            print(
                f"  {issue['component']}:{issue.get('line', '?')} [{issue['rule']}] {issue['message']}"
            )
        if data.get("total", 0) > 100:
            print("  Additional findings are available in SonarQube.")
    except (OSError, ValueError, KeyError) as exc:
        print("  Unable to retrieve findings: " + type(exc).__name__)


def run_offline_backend(env):
    """Preserve the existing backend pre-push check when Sonar cannot run."""
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel"))
    before, after = env.get("PRE_COMMIT_FROM_REF"), env.get("PRE_COMMIT_TO_REF")
    if before and after and not git(root, "diff", "--name-only", before, after, "--", "src/backend"):
        print("No backend changes; offline backend tests skipped.")
        return
    test_env = {key: value for key, value in env.items() if key != "SONAR_TOKEN"}
    with tempfile.TemporaryDirectory(prefix="troshka-push-db-") as database_dir:
        test_env["TROSHKA_TEST_DB_DIR"] = str(Path(database_dir).resolve())
        run_command(
            [test_python(root, "backend"), "-m", "pytest", "tests/", "-q", "--tb=short"],
            root / "src/backend",
            test_env,
            int(env.get("SONAR_TEST_TIMEOUT", "1800")),
        )


def check(env):
    try:
        status = api_json("system/status", env)
    except HTTPError as exc:
        raise CheckFailure(
            f"Sonar API responded HTTP {exc.code}; push blocked."
        ) from exc
    except URLError as exc:
        if isinstance(exc.reason, ssl.SSLError):
            raise CheckFailure("Sonar TLS verification failed; push blocked.") from exc
        print("Sonar API unreachable (VPN unavailable); skipping Sonar pre-push check.")
        run_offline_backend(env)
        return 0
    except (TimeoutError, ConnectionError):
        print("Sonar API unreachable (VPN unavailable); skipping Sonar pre-push check.")
        run_offline_backend(env)
        return 0
    if status.get("status") != "UP":
        raise CheckFailure(
            "Sonar API is reachable but status is " + str(status.get("status"))
        )
    if not env.get("SONAR_TOKEN"):
        raise CheckFailure(
            "Sonar is reachable but SONAR_TOKEN is not exported; push blocked."
        )
    if not shutil.which("sonar-scanner"):
        raise CheckFailure(
            "Sonar is reachable but sonar-scanner is not installed; push blocked."
        )
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel"))
    revision = target_revision(root, env)
    require_clean_sources(root)
    git_dir = Path(git(root, "rev-parse", "--absolute-git-dir"))
    lock = git_dir / "sonar-push.lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise CheckFailure(
            f"A Sonar push check is already running ({lock}); push blocked."
        ) from exc
    try:
        with tempfile.TemporaryDirectory(prefix="sonar-push-", dir=git_dir) as tmp:
            reports = Path(tmp)
            run_coverage(root, reports, env)
            require_clean_sources(root)
            if target_revision(root, env) != revision:
                raise CheckFailure("HEAD changed during tests; retry the push.")
            try:
                run_scanner(root, reports, revision, env)
                verify_analysis(reports, revision, env)
            except CheckFailure:
                print_findings(env)
                raise
            require_clean_sources(root)
            if target_revision(root, env) != revision:
                raise CheckFailure("HEAD changed during analysis; retry the push.")
    finally:
        lock.rmdir()
    return 0


def main():
    try:
        return check(dict(os.environ))
    except (
        CheckFailure,
        OSError,
        ValueError,
        KeyError,
        subprocess.CalledProcessError,
    ) as exc:
        print("Sonar pre-push blocked: " + str(exc), file=sys.stderr)
        print(
            "Remediation: fix the reported findings, run tests, commit the fixes, then retry git push. "
            "Codex should continue this work in the current chat. Do not bypass the failed check or "
            "create commits inside an active push hook.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
