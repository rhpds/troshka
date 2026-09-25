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

echo "Pre-run check (aws, helm, kubectl, curl, jq)..."
require_cmd aws helm kubectl curl jq
resolve_aws_region
ensure_troshka_kubeconfig "$@"

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

ADDR="$(kubectl -n "${NAMESPACE}" get ingress troshka -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
if [[ -n "${ADDR}" ]]; then
  export TROSHKA_API_URL="http://${ADDR}"
elif [[ -z "${TROSHKA_API_URL:-}" ]]; then
  echo "error: set TROSHKA_API_URL or ensure Ingress has an address so wipe can run" >&2
  exit 1
fi

"${_script_dir}/../lib/wipe-workloads.sh"
"${_script_dir}/../lib/verify-clean.sh"

helm uninstall "${RELEASE}" -n "${NAMESPACE}" || true
kubectl delete namespace "${NAMESPACE}" --wait=true || true
helm uninstall aws-load-balancer-controller -n kube-system || true

echo "Deleting CloudFormation stack ${STACK_NAME} (this removes the EKS cluster and VPC)..."
aws cloudformation delete-stack --stack-name "${STACK_NAME}" --region "${REGION}"
aws cloudformation wait stack-delete-complete --stack-name "${STACK_NAME}" --region "${REGION}"

echo "EKS Troshka quickstart removed."
echo "If delete stuck: check leftover ENIs/ALBs/security groups tagged for the VPC, then retry stack delete."
