#!/usr/bin/env bash
# Seed Fedora Cloud Generic qcow2 into the Troshka library via import-url
# (host downloads the URL → S4). No local file transfer.
#
# Expects: TROSHKA_API_URL, and at least one host with agent_status=connected
# Optional: TROSHKA_FEDORA_QCOW_URL, TROSHKA_FEDORA_LIBRARY_NAME,
#           TROSHKA_SKIP_FEDORA_IMAGE=1, NAMESPACE (default troshka)
#
# On EKS, troshka-s4 Ingress is IP-allowlisted to connected host EIPs; backend
# config host_endpoint_url is used for troshkad S3 jobs (pods keep ClusterIP).
set -euo pipefail

_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${_lib_dir}/common.sh"

if [[ "${TROSHKA_SKIP_FEDORA_IMAGE:-}" == "1" || "${TROSHKA_SKIP_FEDORA_IMAGE:-}" == "true" ]]; then
  echo "Skipping Fedora library image (TROSHKA_SKIP_FEDORA_IMAGE)."
  exit 0
fi

require_cmd curl jq kubectl
NAMESPACE="${NAMESPACE:-${TROSHKA_NAMESPACE:-troshka}}"
LIBRARY_NAME="${TROSHKA_FEDORA_LIBRARY_NAME:-Fedora Cloud 43}"
QCOW_URL="${TROSHKA_FEDORA_QCOW_URL:-https://download.fedoraproject.org/pub/fedora/linux/releases/43/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2}"
# Provider endpoint stays ClusterIP so backend boto works behind the allowlist.
S4_INTERNAL="http://troshka-s4.${NAMESPACE}.svc:7480"

if [[ -z "${TROSHKA_API_URL:-}" ]]; then
  echo "error: TROSHKA_API_URL is required" >&2
  exit 1
fi

ensure_s4_provider() {
  local exist ak sk body host_ep
  exist="$(api_get /api/v1/providers/ | jq -r '.[]|select(.type=="s3" and .name=="s4-library")|.id' | head -1)"
  ak="$(kubectl -n "${NAMESPACE}" get secret troshka-secrets -o jsonpath='{.data.s3-access-key}' | base64 -d)"
  sk="$(kubectl -n "${NAMESPACE}" get secret troshka-secrets -o jsonpath='{.data.s3-secret-key}' | base64 -d)"
  if [[ -z "${ak}" || -z "${sk}" ]]; then
    echo "warning: could not read S4 keys from troshka-secrets — skip s4-library provider" >&2
    return 0
  fi
  host_ep="$(resolve_s4_host_endpoint "${NAMESPACE}" || true)"
  if [[ -n "${exist}" && "${exist}" != "null" ]]; then
    echo "Updating s4-library provider endpoint → ${S4_INTERNAL} (host jobs use host_endpoint_url)..."
    body="$(jq -n --arg ak "${ak}" --arg sk "${sk}" --arg ep "${S4_INTERNAL}" \
      --arg desc "${host_ep:+Hosts use ${host_ep}}" \
      '{access_key_id:$ak,secret_access_key:$sk,bucket:"troshka-images",endpoint_url:$ep,description:$desc}')"
    api_patch "/api/v1/providers/${exist}" "${body}" >/dev/null || true
    return 0
  fi
  echo "Creating s4-library provider (ClusterIP; hosts use Ingress host_endpoint_url)..."
  body="$(jq -n --arg ak "${ak}" --arg sk "${sk}" --arg ep "${S4_INTERNAL}" \
    '{name:"s4-library",type:"s3",default_region:"us-east-1",access_key_id:$ak,secret_access_key:$sk,bucket:"troshka-images",endpoint_url:$ep}')"
  api_post /api/v1/providers/ "${body}" >/dev/null
}

wait_connected_host() {
  local deadline=$((SECONDS + ${TROSHKA_HOST_WAIT:-600}))
  local n
  while (( SECONDS < deadline )); do
    n="$(api_get /api/v1/hosts/ | jq '[.[]|select(.agent_status=="connected" and .state=="active")]|length')"
    if [[ "${n}" -ge 1 ]]; then
      echo "Connected host available."
      return 0
    fi
    echo "Waiting for a connected host (import-url needs troshkad)..."
    sleep 10
  done
  echo "error: no connected host within ${TROSHKA_HOST_WAIT:-600}s — cannot import-url" >&2
  return 1
}

wait_item_ready() {
  local item_id="$1"
  local deadline=$((SECONDS + ${TROSHKA_IMPORT_WAIT:-3600}))
  local state
  while (( SECONDS < deadline )); do
    state="$(api_get /api/v1/library/ | jq -r --arg id "${item_id}" \
      '.[]|select(.id==$id)|.state')"
    echo "  library item ${item_id:0:8}: state=${state:-unknown}"
    case "${state}" in
      ready|available) return 0 ;;
      error|failed)
        echo "error: import failed (state=${state})" >&2
        return 1
        ;;
    esac
    sleep 15
  done
  echo "error: import timed out" >&2
  return 1
}

ensure_s4_provider
wait_connected_host
refresh_s4_host_allowlist "${NAMESPACE}"

HOST_EP="$(resolve_s4_host_endpoint "${NAMESPACE}" || true)"
if [[ -n "${HOST_EP}" ]]; then
  echo "Troshkad S3 endpoint (from backend config): ${HOST_EP}"
fi

EXISTING="$(api_get /api/v1/library/ | jq -r --arg n "${LIBRARY_NAME}" \
  '.[]|select(.name==$n and (.state=="ready" or .state=="available"))|.id' | head -1)"
if [[ -n "${EXISTING}" && "${EXISTING}" != "null" ]]; then
  echo "Library image \"${LIBRARY_NAME}\" already ready (${EXISTING:0:8})."
  exit 0
fi

ITEM_ID="$(api_get /api/v1/library/ | jq -r --arg n "${LIBRARY_NAME}" \
  '.[]|select(.name==$n)|.id' | head -1)"
if [[ -z "${ITEM_ID}" || "${ITEM_ID}" == "null" ]]; then
  echo "Creating library item \"${LIBRARY_NAME}\"..."
  ITEM_ID="$(api_post /api/v1/library/ "$(jq -n --arg n "${LIBRARY_NAME}" \
    '{name:$n,description:"Fedora Cloud Base Generic (official qcow2)",type:"image",format:"qcow2",os_variant:"fedora43"}')" \
    | jq -r .id)"
fi

echo "Starting import-url → ${QCOW_URL}"
api_post "/api/v1/library/${ITEM_ID}/import-url" \
  "$(jq -n --arg u "${QCOW_URL}" '{url:$u}')" >/dev/null || true

wait_item_ready "${ITEM_ID}"
api_get /api/v1/library/ | jq --arg id "${ITEM_ID}" \
  '.[]|select(.id==$id)|{id,name,state,size_bytes,format,os_variant}'
echo "Fedora library image ready."
