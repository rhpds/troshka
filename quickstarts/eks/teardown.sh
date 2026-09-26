#!/usr/bin/env bash
# Full wipe + Helm uninstall + CloudFormation delete for EKS quickstart.
set -euo pipefail

_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "${_script_dir}/../lib/common.sh"

STACK_NAME="${TROSHKA_EKS_STACK:-troshka-eks-quickstart}"
CLUSTER_NAME="${TROSHKA_EKS_CLUSTER:-troshka-quickstart}"
NAMESPACE="${TROSHKA_NAMESPACE:-troshka}"
RELEASE="${TROSHKA_RELEASE:-troshka}"
INGRESS_NS="${TROSHKA_INGRESS_NS:-ingress-nginx}"
CERT_MANAGER_NS="${TROSHKA_CERT_MANAGER_NS:-cert-manager}"

apply_aws_cli_args "$@"

echo "Pre-run check (aws, helm, kubectl, curl, jq)..."
require_cmd aws helm kubectl curl jq
resolve_aws_region
ensure_troshka_kubeconfig "$@"
resolve_aws_credential_source

if ! aws sts get-caller-identity --region "${REGION}" >/dev/null 2>&1; then
  echo "Pre-run check failed — AWS credentials not configured for region ${REGION}." >&2
  print_eks_tips >&2
  exit 1
fi

ACCOUNT_ID="$(aws sts get-caller-identity --region "${REGION}" --query Account --output text)"
CALLER_ARN="$(aws sts get-caller-identity --region "${REGION}" --query Arn --output text)"

cat <<EOF

WARNING: About to wipe Troshka projects and DELETE the EKS stack in this AWS account.

  Account:    ${ACCOUNT_ID}
  Identity:   ${CALLER_ARN}
  Creds:      ${AWS_CREDS_SOURCE}
  Region:     ${REGION}  ← from ${REGION_SOURCE}
  Stack:      ${STACK_NAME}
  Cluster:    ${CLUSTER_NAME}
  KUBECONFIG: ${KUBECONFIG:-~/.kube/config (default)}

  Skip this prompt next time: --yes / --quiet / --no-verify

EOF

confirm "Proceed with wipe + delete in account ${ACCOUNT_ID} / region ${REGION}?" "$@" || {
  echo "Aborted."
  print_eks_tips
  exit 1
}

aws eks update-kubeconfig --name "${CLUSTER_NAME}" --region "${REGION}" 2>/dev/null || true

trap stop_backend_port_forward EXIT

# Wipe via port-forward so nginx basic auth / Cognito oauth2-proxy do not block DELETE.
if kubectl -n "${NAMESPACE}" get svc troshka-backend >/dev/null 2>&1; then
  echo "Port-forwarding to troshka-backend for project wipe..."
  start_backend_port_forward "${NAMESPACE}" || {
    echo "warning: could not reach backend — skipping API wipe (namespace will still be deleted)" >&2
  }
  if [[ -n "${TROSHKA_API_URL:-}" ]]; then
    read_troshka_auth_from_cluster "${NAMESPACE}"
    if [[ "${TROSHKA_OAUTH_ENABLED}" == "true" ]]; then
      export TROSHKA_FORWARDED_EMAIL="${TROSHKA_ADMIN_EMAIL:-${TROSHKA_ADMIN_FROM_CM:-}}"
      if [[ -z "${TROSHKA_FORWARDED_EMAIL}" ]]; then
        echo "error: oauth is enabled but no admin email found (set TROSHKA_ADMIN_EMAIL)" >&2
        exit 1
      fi
      echo "Using SSO wipe identity: ${TROSHKA_FORWARDED_EMAIL}"
    fi
    # Destroy projects first — host DELETE returns 409 while any are active/deploying.
    "${_script_dir}/../lib/wipe-workloads.sh"
    "${_script_dir}/../lib/verify-clean.sh"
    echo "Terminating Troshka hosts..."
    host_ids="$(api_get "/api/v1/hosts/" 2>/dev/null | jq -r '.[].id // empty' || true)"
    if [[ -n "${host_ids}" ]]; then
      echo "${host_ids}" | while read -r hid; do
        [[ -z "${hid}" ]] && continue
        echo "  DELETE /api/v1/hosts/${hid}"
        api_delete "/api/v1/hosts/${hid}" >/dev/null || \
          echo "warning: host delete failed for ${hid}" >&2
      done
      sleep 5
    else
      echo "No hosts to terminate."
    fi
  fi
  stop_backend_port_forward
  trap - EXIT
else
  echo "No troshka-backend Service — nothing to wipe via API."
fi

# Remove IAM compute user + secret created by seed-compute.sh
IAM_USER="${CLUSTER_NAME}-compute"
SECRET_NAME="${CLUSTER_NAME}/compute"
POLICY_NAME="troshka-compute-${CLUSTER_NAME}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${POLICY_NAME}"
echo "Cleaning compute IAM user ${IAM_USER} (if present)..."
if aws iam get-user --user-name "${IAM_USER}" >/dev/null 2>&1; then
  aws iam detach-user-policy --user-name "${IAM_USER}" --policy-arn "${POLICY_ARN}" >/dev/null 2>&1 || true
  while read -r key_id; do
    [[ -z "${key_id}" ]] && continue
    aws iam delete-access-key --user-name "${IAM_USER}" --access-key-id "${key_id}" >/dev/null || true
  done < <(aws iam list-access-keys --user-name "${IAM_USER}" --query 'AccessKeyMetadata[].AccessKeyId' --output text 2>/dev/null | tr '\t' '\n')
  aws iam delete-user-policy --user-name "${IAM_USER}" --policy-name troshka-compute >/dev/null 2>&1 || true
  aws iam delete-user --user-name "${IAM_USER}" >/dev/null 2>&1 || \
    echo "warning: could not delete IAM user ${IAM_USER}" >&2
fi
if aws iam get-policy --policy-arn "${POLICY_ARN}" >/dev/null 2>&1; then
  while read -r ver; do
    [[ -z "${ver}" || "${ver}" == "None" ]] && continue
    aws iam delete-policy-version --policy-arn "${POLICY_ARN}" --version-id "${ver}" >/dev/null 2>&1 || true
  done < <(aws iam list-policy-versions --policy-arn "${POLICY_ARN}" \
    --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text 2>/dev/null | tr '\t' '\n')
  aws iam delete-policy --policy-arn "${POLICY_ARN}" >/dev/null 2>&1 || \
    echo "warning: could not delete policy ${POLICY_NAME}" >&2
fi
aws secretsmanager delete-secret --secret-id "${SECRET_NAME}" --region "${REGION}" \
  --force-delete-without-recovery >/dev/null 2>&1 || true

# Seeded compute VPC(s) — separate from the EKS CFN VPC (Name=troshka-vpc).
delete_troshka_compute_vpcs "${REGION}"

# Short kubectl/helm deadline — once the API is gone, aws eks get-token can hang forever.
_k8s_timeout() {
  # usage: _k8s_timeout <seconds> <cmd> [args...]
  local secs="$1"
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "${secs}" "$@"
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout "${secs}" "$@"
  else
    "$@"
  fi
}

_cluster_api_reachable() {
  # Cheap probe; fail fast if EKS auth or apiserver is already dead.
  _k8s_timeout 15 kubectl --request-timeout=10s get --raw=/readyz >/dev/null 2>&1
}

echo "Uninstalling Troshka Helm release..."
if _cluster_api_reachable; then
  _k8s_timeout 60 helm uninstall "${RELEASE}" -n "${NAMESPACE}" 2>/dev/null || true
  _k8s_timeout 300 kubectl delete namespace "${NAMESPACE}" --wait=true --timeout=5m 2>/dev/null || true
else
  echo "  Cluster API unreachable — skip Helm/namespace uninstall (CFN delete will remove the cluster)."
fi

if _cluster_api_reachable; then
  echo "Uninstalling ingress-nginx / cert-manager (quickstart-managed)..."
  _k8s_timeout 60 helm uninstall ingress-nginx -n "${INGRESS_NS}" 2>/dev/null || true
  _k8s_timeout 30 kubectl --request-timeout=15s delete namespace "${INGRESS_NS}" --wait=false 2>/dev/null || true
  _k8s_timeout 60 helm uninstall cert-manager -n "${CERT_MANAGER_NS}" 2>/dev/null || true
  _k8s_timeout 30 kubectl --request-timeout=15s delete clusterissuer letsencrypt-prod --ignore-not-found 2>/dev/null || true
  _k8s_timeout 30 kubectl --request-timeout=15s delete namespace "${CERT_MANAGER_NS}" --wait=false 2>/dev/null || true
  # Legacy ALB controller from earlier quickstart revisions
  _k8s_timeout 60 helm uninstall aws-load-balancer-controller -n kube-system 2>/dev/null || true
else
  echo "Cluster API unreachable — skip ingress-nginx / cert-manager cleanup; CFN delete tears the cluster down."
fi

echo "Deleting CloudFormation stack ${STACK_NAME} (this removes the EKS cluster and VPC)..."
delete_cloudformation_stack "${STACK_NAME}" "${REGION}" || {
  echo "If delete stuck: check leftover ENIs/NLBs/security groups tagged for the VPC, then retry stack delete." >&2
  exit 1
}

echo "EKS Troshka quickstart removed."
