#!/usr/bin/env bash
# Full wipe + Helm uninstall Troshka from OpenShift.
set -euo pipefail

_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "${_script_dir}/../lib/common.sh"

NAMESPACE="${TROSHKA_NAMESPACE:-troshka}"
RELEASE="${TROSHKA_RELEASE:-troshka}"

echo "Pre-run check (helm, curl, jq, oc|kubectl)..."
require_cmd helm curl jq
require_kube_cli
if command -v oc >/dev/null 2>&1; then
  KUBECTL=oc
else
  KUBECTL=kubectl
fi

confirm "Destroy all Troshka projects and uninstall release ${RELEASE} from ${NAMESPACE}?" "$@" || {
  echo "Aborted."
  exit 1
}

HOST="$($KUBECTL -n "${NAMESPACE}" get route troshka -o jsonpath='{.spec.host}' 2>/dev/null || true)"
if [[ -n "${HOST}" ]]; then
  export TROSHKA_API_URL="https://${HOST}"
else
  export TROSHKA_API_URL="${TROSHKA_API_URL:-}"
fi

if [[ -z "${TROSHKA_API_URL}" ]]; then
  echo "error: set TROSHKA_API_URL or ensure Route troshka exists so wipe can run" >&2
  exit 1
fi

"${_script_dir}/../lib/wipe-workloads.sh"
"${_script_dir}/../lib/verify-clean.sh"

helm uninstall "${RELEASE}" -n "${NAMESPACE}" || true
$KUBECTL delete namespace "${NAMESPACE}" --wait=true || true

echo "OpenShift Troshka install removed."
