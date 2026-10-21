# SonarQube pre-push gate

Install the repository hooks with:

```bash
pre-commit install --hook-type pre-commit --hook-type pre-push
```

The `sonarqube-push` hook runs for every push through the existing pre-commit
configuration. It probes the internal Sonar API with an eight-second timeout.
If the API cannot be reached, it reports a skip so work can be pushed off VPN.
An HTTP error or an unhealthy server blocks the push.

When the API is reachable, export `SONAR_TOKEN` in the calling shell and have
`sonar-scanner`, Node/npm, frontend dependencies, and repository Python test
environments installed. The token is passed through the scanner environment,
not command-line arguments. The script does not read shell startup files or
copy credentials. `SONAR_HOST_URL` (or the older `SONAR_URL`) can override the
default `https://sonarqube.corp.redhat.com` instance.

The check generates fresh coverage for the backend, operator, host agent, and
frontend, then runs the scanner and waits for the quality gate. Python test
environments are selected from each component's `venv` or `.venv`, falling back
to the backend venv when a component has none. The operator environment needs
its pinned runtime dependencies, including `kopf`, plus pytest/pytest-cov.
Frontend coverage uses two workers. Test failures, missing dependencies,
scanner errors, timeouts, and failed gates all block the push.

Reports are generated in a temporary directory under the Git directory and
removed when the hook exits. The scanner receives only these fresh reports,
not old local reports or CI coverage shards. Backend test databases use a
separate temporary directory. Concurrent checks in the same checkout are
blocked by `.git/sonar-push.lock`; after an interrupted process, remove a stale
lock only after verifying no check is running.

The hook verifies the submitted analysis ID, revision, and quality-gate result.
Push the checked-out HEAD; a push targeting another revision is rejected.
Uncommitted tracked changes and untracked source files are rejected during the
scan. Pre-commit normally stashes unrelated unstaged edits for the hook and
restores them afterward. The check does not amend commits or launch another AI
process from inside `git push`.

## Remediation

A failure prints the failing gate metrics and up to 100 current new-code
findings. In Codex, continue in the current chat: investigate the findings,
make the smallest appropriate fixes, test them, commit, and retry the push.
Do not bypass the check to get a failed analysis through. A new fix commit
requires a new push attempt because Git selected the original commit IDs
before invoking the hook. Review false positives individually with source
evidence; do not weaken the quality gate or add blanket exclusions.

`SONAR_TEST_TIMEOUT` defaults to 1800 seconds per suite,
`SONAR_PUSH_TIMEOUT` to 1200 seconds for the scanner, and
`SONAR_GATE_TIMEOUT` to 300 seconds for server-side quality-gate processing.
Reaching a timeout blocks the push; it never counts as an offline skip.

Run the hook's offline unit tests with:

```bash
python3 -m unittest discover -s scripts/tests -p test_sonarqube_push.py
```

To run the real check manually on committed, clean source:

```bash
bash scripts/sonarqube-check.sh
```
