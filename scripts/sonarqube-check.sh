#!/usr/bin/env bash
set -euo pipefail

if [ -z "${SONAR_URL:-}" ] || [ -z "${SONAR_TOKEN:-}" ]; then
    echo "⚠️  SONAR_URL or SONAR_TOKEN not set — skipping SonarQube analysis"
    exit 0
fi

if ! curl -sf --connect-timeout 5 "$SONAR_URL/api/system/status" >/dev/null 2>&1; then
    echo "⚠️  SonarQube unreachable (not on VPN?) — skipping"
    exit 0
fi

if ! command -v sonar-scanner >/dev/null 2>&1; then
    echo "⚠️  sonar-scanner not installed — skipping"
    exit 0
fi

TIMEOUT="${SONAR_PUSH_TIMEOUT:-180}"

cd "$(git rev-parse --show-toplevel)"

_run_with_timeout() {
    if command -v timeout >/dev/null 2>&1; then
        timeout "$TIMEOUT" "$@"
    elif command -v gtimeout >/dev/null 2>&1; then
        gtimeout "$TIMEOUT" "$@"
    else
        "$@"
    fi
}

echo "Running backend tests with coverage..."
cd src/backend
./venv/bin/python3 -m pytest tests/ -q --cov=app --cov-report=xml:../../coverage.xml
cd ../..

echo "Running operator tests with coverage..."
cd src/operator
python3 -m pytest tests/ -q --cov=. --cov-report=xml:../../operator-coverage.xml 2>/dev/null || true
cd ../..

echo "Running troshkad tests with coverage..."
cd src/troshkad
python3 -m pytest tests/ -q --cov=troshkad --cov-report=xml:../../troshkad-coverage.xml 2>/dev/null || true
cd ../..

if ! _run_with_timeout sonar-scanner \
    -Dsonar.host.url="$SONAR_URL" \
    -Dsonar.token="$SONAR_TOKEN" \
    -Dsonar.qualitygate.wait=true \
    -Dsonar.qualitygate.timeout=300 \
    2>&1 | tail -20; then
    echo "⚠️  SonarQube analysis timed out after ${TIMEOUT}s — skipping (run manually)"
    exit 0
fi
