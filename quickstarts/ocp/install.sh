#!/usr/bin/env bash
# One-button Troshka install on OpenShift (Helm).
set -euo pipefail

_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "${_script_dir}/../lib/common.sh"

NAMESPACE="${TROSHKA_NAMESPACE:-troshka}"
RELEASE="${TROSHKA_RELEASE:-troshka}"
IMAGE_TAG="${TROSHKA_IMAGE_TAG:-latest}"

require_cmd helm
if command -v oc >/dev/null 2>&1; then
  KUBECTL=oc
elif command -v kubectl >/dev/null 2>&1; then
  KUBECTL=kubectl
else
  echo "error: need oc or kubectl" >&2
  exit 1
fi

echo "Installing Troshka into namespace ${NAMESPACE} (release ${RELEASE})..."
helm upgrade --install "${RELEASE}" "${REPO_ROOT}/deploy/helm" \
  --namespace "${NAMESPACE}" \
  --create-namespace \
  --set postgres.deploy=true \
  --set s4.deploy=true \
  --set auth.oauthEnabled=false \
  --set redis.deploy=true \
  --set worker.replicas=2 \
  --set backend.image.tag="${IMAGE_TAG}" \
  --set frontend.image.tag="${IMAGE_TAG}" \
  --wait \
  --timeout 15m

echo "Waiting for Route..."
HOST=""
for _ in $(seq 1 60); do
  HOST="$($KUBECTL -n "${NAMESPACE}" get route troshka -o jsonpath='{.spec.host}' 2>/dev/null || true)"
  if [[ -n "${HOST}" ]]; then
    break
  fi
  sleep 5
done

if [[ -z "${HOST}" ]]; then
  echo "warning: Route host not ready yet — check: ${KUBECTL} -n ${NAMESPACE} get route" >&2
else
  export TROSHKA_API_URL="https://${HOST}"
  # Projects API may need a moment after pods Ready
  TROSHKA_WAIT_TIMEOUT=300 "${_script_dir}/../lib/wait-for-api.sh" || true
fi

cat <<EOF

Troshka is installed on OpenShift (dev auth — auto-admin).

  UI: https://${HOST:-<pending-route>}

Compute (lab VMs): add a local/remote libvirt host, or configure EC2 / Azure / GCP / KubeVirt / OCP Virt.
See docs/quickstarts/ocp.md

Teardown: ${_script_dir}/teardown.sh

EOF
