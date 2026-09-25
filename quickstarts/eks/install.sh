#!/usr/bin/env bash
# One-button Troshka on Amazon EKS: CloudFormation VPC+EKS, then Helm.
set -euo pipefail

_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "${_script_dir}/../lib/common.sh"

STACK_NAME="${TROSHKA_EKS_STACK:-troshka-eks-quickstart}"
CLUSTER_NAME="${TROSHKA_EKS_CLUSTER:-troshka-quickstart}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
NAMESPACE="${TROSHKA_NAMESPACE:-troshka}"
RELEASE="${TROSHKA_RELEASE:-troshka}"
CFN_TEMPLATE="${REPO_ROOT}/deploy/eks/cloudformation/troshka-eks.yaml"

require_cmd aws helm kubectl curl jq

echo "Caller identity:"
aws sts get-caller-identity --region "${REGION}"

echo "Deploying CloudFormation stack ${STACK_NAME} in ${REGION}..."
if aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" >/dev/null 2>&1; then
  aws cloudformation update-stack \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}" \
    --template-body "file://${CFN_TEMPLATE}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameters "ParameterKey=ClusterName,ParameterValue=${CLUSTER_NAME}" \
    || true
else
  aws cloudformation create-stack \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}" \
    --template-body "file://${CFN_TEMPLATE}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameters "ParameterKey=ClusterName,ParameterValue=${CLUSTER_NAME}"
fi

echo "Waiting for stack CREATE/UPDATE_COMPLETE..."
aws cloudformation wait stack-create-complete --stack-name "${STACK_NAME}" --region "${REGION}" 2>/dev/null \
  || aws cloudformation wait stack-update-complete --stack-name "${STACK_NAME}" --region "${REGION}" 2>/dev/null \
  || true

STATUS="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" \
  --query 'Stacks[0].StackStatus' --output text)"
echo "Stack status: ${STATUS}"
case "${STATUS}" in
  *COMPLETE) ;;
  *)
    echo "error: stack not complete (${STATUS})" >&2
    exit 1
    ;;
esac

aws eks update-kubeconfig --name "${CLUSTER_NAME}" --region "${REGION}"

echo "Installing AWS Load Balancer Controller (Helm)..."
helm repo add eks https://aws.github.io/eks-charts >/dev/null 2>&1 || true
helm repo update eks >/dev/null 2>&1 || true

# Best-effort IRSA-less install for quickstart demos (controller uses node role / attached policies).
# Production should use IRSA; see AWS docs.
kubectl apply -k "github.com/aws/eks-charts/stable/aws-load-balancer-controller/crds?ref=master" 2>/dev/null || true
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  -n kube-system \
  --set clusterName="${CLUSTER_NAME}" \
  --set serviceAccount.create=true \
  --set region="${REGION}" \
  --set vpcId="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='VpcId'].OutputValue" --output text)" \
  --wait --timeout 10m || {
    echo "warning: ALB controller helm install failed — Ingress may stay pending" >&2
  }

echo "Installing Troshka Helm chart..."
helm upgrade --install "${RELEASE}" "${REPO_ROOT}/deploy/helm" \
  --namespace "${NAMESPACE}" \
  --create-namespace \
  -f "${REPO_ROOT}/deploy/helm/values-eks.yaml" \
  --wait \
  --timeout 20m

echo "Waiting for Ingress address..."
ADDR=""
for _ in $(seq 1 90); do
  ADDR="$(kubectl -n "${NAMESPACE}" get ingress troshka -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
  if [[ -n "${ADDR}" ]]; then
    break
  fi
  sleep 10
done

export TROSHKA_API_URL="http://${ADDR}"
if [[ -n "${ADDR}" ]]; then
  TROSHKA_WAIT_TIMEOUT=300 "${_script_dir}/../lib/wait-for-api.sh" || true
fi

cat <<EOF

Troshka is installed on EKS (dev auth — auto-admin).

  Stack:   ${STACK_NAME}
  Cluster: ${CLUSTER_NAME}
  UI:      http://${ADDR:-<ingress-pending>}

Permissions used by this script are documented in docs/quickstarts/eks.md
and deploy/eks/iam-deployer-policy.json

Compute (lab VMs): not in this stack — add EC2/Azure/GCP/KubeVirt or a Linux host.
Teardown: ${_script_dir}/teardown.sh

EOF
