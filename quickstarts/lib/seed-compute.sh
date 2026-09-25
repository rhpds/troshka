#!/usr/bin/env bash
# Seed one EC2 provider + one host after an EKS (or any) Troshka install.
#
# Expects: TROSHKA_API_URL, REGION, CLUSTER_NAME (or TROSHKA_EKS_CLUSTER)
# Optional: PRODUCTION=true → RHEL 9 Hourly AMI + larger host
#           TROSHKA_HOST_INSTANCE_TYPE, TROSHKA_HOST_DISK_GB, TROSHKA_HOST_AMI
# Auth: same as wipe — oauth off = auto-admin via port-forward;
#       oauth on = set TROSHKA_FORWARDED_EMAIL (admin email).
set -euo pipefail

_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${_lib_dir}/common.sh"

require_cmd aws curl jq

CLUSTER_NAME="${CLUSTER_NAME:-${TROSHKA_EKS_CLUSTER:-troshka-quickstart}}"
REGION="${REGION:-${AWS_REGION:-us-east-1}}"
PRODUCTION="${PRODUCTION:-false}"
PROVIDER_NAME="${TROSHKA_PROVIDER_NAME:-ec2-${CLUSTER_NAME}}"
IAM_USER="${CLUSTER_NAME}-compute"
SECRET_NAME="${CLUSTER_NAME}/compute"
POLICY_FILE="${REPO_ROOT}/infra/iam-policy.json"

if [[ -z "${TROSHKA_API_URL:-}" ]]; then
  echo "error: TROSHKA_API_URL is required (use start_backend_port_forward)" >&2
  exit 1
fi

if [[ "${PRODUCTION}" == "true" ]]; then
  INSTANCE_TYPE="${TROSHKA_HOST_INSTANCE_TYPE:-m8i.2xlarge}"
  DISK_GB="${TROSHKA_HOST_DISK_GB:-500}"
  AMI_FLAVOR="rhel"
else
  INSTANCE_TYPE="${TROSHKA_HOST_INSTANCE_TYPE:-m8i.xlarge}"
  DISK_GB="${TROSHKA_HOST_DISK_GB:-100}"
  AMI_FLAVOR="fedora"
fi

# --- IAM user + access keys (idempotent) ---
ensure_compute_iam() {
  local policy_name="troshka-compute-${CLUSTER_NAME}"
  local account_id policy_arn

  account_id="$(aws sts get-caller-identity --query Account --output text)"
  policy_arn="arn:aws:iam::${account_id}:policy/${policy_name}"

  if ! aws iam get-user --user-name "${IAM_USER}" >/dev/null 2>&1; then
    echo "Creating IAM user ${IAM_USER}..."
    aws iam create-user --user-name "${IAM_USER}" \
      --tags "Key=ManagedBy,Value=troshka" "Key=troshka:cluster,Value=${CLUSTER_NAME}" >/dev/null
  fi

  if [[ -f "${POLICY_FILE}" ]]; then
    if ! aws iam get-policy --policy-arn "${policy_arn}" >/dev/null 2>&1; then
      echo "Creating managed policy ${policy_name}..."
      aws iam create-policy --policy-name "${policy_name}" \
        --policy-document "file://${POLICY_FILE}" \
        --description "Troshka EC2 compute permissions (${CLUSTER_NAME})" \
        --tags "Key=ManagedBy,Value=troshka" "Key=troshka:cluster,Value=${CLUSTER_NAME}" >/dev/null
    else
      # Keep policy document current (new default version; drop oldest if at limit).
      local ver_count
      ver_count="$(aws iam list-policy-versions --policy-arn "${policy_arn}" \
        --query 'length(Versions)' --output text 2>/dev/null || echo 0)"
      if [[ "${ver_count}" -ge 5 ]]; then
        local old_ver
        old_ver="$(aws iam list-policy-versions --policy-arn "${policy_arn}" \
          --query 'Versions[?IsDefaultVersion==`false`]|sort_by(@,&CreateDate)[0].VersionId' --output text)"
        [[ -n "${old_ver}" && "${old_ver}" != "None" ]] && \
          aws iam delete-policy-version --policy-arn "${policy_arn}" --version-id "${old_ver}" >/dev/null || true
      fi
      aws iam create-policy-version --policy-arn "${policy_arn}" \
        --policy-document "file://${POLICY_FILE}" --set-as-default >/dev/null 2>&1 || true
    fi
    aws iam attach-user-policy --user-name "${IAM_USER}" --policy-arn "${policy_arn}" >/dev/null
    # Drop legacy inline policy if present (2KB limit).
    aws iam delete-user-policy --user-name "${IAM_USER}" --policy-name troshka-compute >/dev/null 2>&1 || true
  else
    echo "warning: ${POLICY_FILE} missing — IAM user has no compute policy" >&2
  fi

  local secret_json=""
  if secret_json="$(aws secretsmanager get-secret-value --secret-id "${SECRET_NAME}" --region "${REGION}" \
      --query SecretString --output text 2>/dev/null)"; then
    COMPUTE_AK="$(echo "${secret_json}" | jq -r .access_key_id)"
    COMPUTE_SK="$(echo "${secret_json}" | jq -r .secret_access_key)"
    if [[ -n "${COMPUTE_AK}" && "${COMPUTE_AK}" != "null" && -n "${COMPUTE_SK}" && "${COMPUTE_SK}" != "null" ]]; then
      echo "Reusing compute credentials from Secrets Manager (${SECRET_NAME})."
      return 0
    fi
  fi

  # Drop stale keys so we can create a fresh pair (IAM max 2 keys).
  local key_id
  while read -r key_id; do
    [[ -z "${key_id}" ]] && continue
    aws iam delete-access-key --user-name "${IAM_USER}" --access-key-id "${key_id}" >/dev/null || true
  done < <(aws iam list-access-keys --user-name "${IAM_USER}" --query 'AccessKeyMetadata[].AccessKeyId' --output text 2>/dev/null | tr '\t' '\n')

  echo "Creating access keys for ${IAM_USER}..."
  local key_json
  key_json="$(aws iam create-access-key --user-name "${IAM_USER}")"
  COMPUTE_AK="$(echo "${key_json}" | jq -r .AccessKey.AccessKeyId)"
  COMPUTE_SK="$(echo "${key_json}" | jq -r .AccessKey.SecretAccessKey)"

  local payload
  payload="$(jq -n --arg ak "${COMPUTE_AK}" --arg sk "${COMPUTE_SK}" \
    '{access_key_id:$ak,secret_access_key:$sk}')"
  if aws secretsmanager describe-secret --secret-id "${SECRET_NAME}" --region "${REGION}" >/dev/null 2>&1; then
    aws secretsmanager put-secret-value --secret-id "${SECRET_NAME}" --region "${REGION}" \
      --secret-string "${payload}" >/dev/null
  else
    aws secretsmanager create-secret --name "${SECRET_NAME}" --region "${REGION}" \
      --description "Troshka EC2 compute provider keys (${CLUSTER_NAME})" \
      --secret-string "${payload}" \
      --tags "Key=ManagedBy,Value=troshka" "Key=troshka:cluster,Value=${CLUSTER_NAME}" >/dev/null
  fi
}

# --- AMI discovery ---
resolve_host_ami() {
  if [[ -n "${TROSHKA_HOST_AMI:-}" ]]; then
    HOST_AMI="${TROSHKA_HOST_AMI}"
    echo "Using override AMI ${HOST_AMI}"
    return 0
  fi

  if [[ "${AMI_FLAVOR}" == "rhel" ]]; then
    echo "Resolving RHEL 9 Hourly marketplace AMI in ${REGION}..."
    # Hourly/PAYG — available to any AWS account. Do NOT use Access2/Gold (BYOS);
    # that requires Red Hat Cloud Access and fails for most users.
    HOST_AMI="$(aws ec2 describe-images --region "${REGION}" --owners 309956199498 \
      --filters \
        "Name=name,Values=RHEL-9*x86_64*Hourly2-GP3" \
        "Name=state,Values=available" \
        "Name=architecture,Values=x86_64" \
      --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)"
  else
    echo "Resolving Fedora Cloud Base AMI in ${REGION}..."
    # Fedora Project account — prefer stable N.x (exclude ELN/Rawhide).
    HOST_AMI="$(aws ec2 describe-images --region "${REGION}" --owners 125523088429 \
      --filters \
        "Name=name,Values=Fedora-Cloud-Base-AmazonEC2.x86_64-4*" \
        "Name=state,Values=available" \
        "Name=architecture,Values=x86_64" \
      --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text 2>/dev/null || true)"
    if [[ -z "${HOST_AMI}" || "${HOST_AMI}" == "None" ]]; then
      HOST_AMI="$(aws ec2 describe-images --region "${REGION}" --owners 125523088429 \
        --filters \
          "Name=name,Values=Fedora-Cloud-Base-AmazonEC2*x86_64*" \
          "Name=state,Values=available" \
        --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)"
    fi
  fi

  if [[ -z "${HOST_AMI}" || "${HOST_AMI}" == "None" ]]; then
    echo "error: could not resolve a ${AMI_FLAVOR} AMI in ${REGION}" >&2
    exit 1
  fi
  echo "Host AMI: ${HOST_AMI} (${AMI_FLAVOR})"
}

# --- Troshka API seed ---
seed_provider_and_host() {
  local providers provider_id host_count

  providers="$(api_get "/api/v1/providers/" || echo '[]')"
  provider_id="$(echo "${providers}" | jq -r --arg n "${PROVIDER_NAME}" \
    '.[] | select(.name==$n and .type=="ec2") | .id' | head -1)"

  if [[ -z "${provider_id}" || "${provider_id}" == "null" ]]; then
    echo "Creating EC2 provider ${PROVIDER_NAME}..."
    local body
    body="$(jq -n \
      --arg name "${PROVIDER_NAME}" \
      --arg region "${REGION}" \
      --arg ak "${COMPUTE_AK}" \
      --arg sk "${COMPUTE_SK}" \
      '{name:$name,type:"ec2",default_region:$region,access_key_id:$ak,secret_access_key:$sk}')"
    provider_id="$(api_post "/api/v1/providers/" "${body}" | jq -r .id)"
  else
    echo "Provider ${PROVIDER_NAME} already exists (${provider_id:0:8})."
  fi

  if [[ -z "${provider_id}" || "${provider_id}" == "null" ]]; then
    echo "error: failed to create/find provider ${PROVIDER_NAME}" >&2
    exit 1
  fi

  # Prefer list endpoint — some builds 405 on GET /providers/{id}.
  local vpc
  vpc="$(api_get "/api/v1/providers/" | jq -r --arg id "${provider_id}" \
    '.[] | select(.id==$id) | .vpc_id // empty')"
  if [[ -z "${vpc}" || "${vpc}" == "null" ]]; then
    echo "Creating Troshka compute VPC (create-vpc)..."
    api_post "/api/v1/providers/${provider_id}/create-vpc" >/dev/null
  else
    echo "Provider already has VPC ${vpc}."
  fi

  echo "Setting default image ${HOST_AMI}..."
  api_post "/api/v1/providers/${provider_id}/set-image?image_id=${HOST_AMI}" >/dev/null

  host_count="$(api_get "/api/v1/hosts/" | jq -r --arg p "${provider_id}" \
    '[.[] | select(.provider_id==$p and .state!="terminated")] | length')"
  if [[ "${host_count}" != "0" ]]; then
    echo "Provider already has ${host_count} host(s) — skipping provision."
    return 0
  fi

  echo "Provisioning host (${INSTANCE_TYPE}, ${DISK_GB} GiB, ${AMI_FLAVOR})..."
  local host_body host_id
  host_body="$(jq -n \
    --arg pid "${provider_id}" \
    --arg itype "${INSTANCE_TYPE}" \
    --arg ami "${HOST_AMI}" \
    --argjson disk "${DISK_GB}" \
    '{provider_id:$pid,instance_type:$itype,image_id:$ami,disk_gb:$disk}')"
  host_id="$(api_post "/api/v1/hosts/" "${host_body}" | jq -r .id)"
  echo "Host ${host_id:0:8} provisioning (agent install continues in background)."
  echo "  Watch: Admin → Hosts, or GET /api/v1/hosts/"
}

echo "Seeding compute (provider=${PROVIDER_NAME}, flavor=${AMI_FLAVOR})..."
ensure_compute_iam
resolve_host_ami
seed_provider_and_host
echo "Compute seed done."
