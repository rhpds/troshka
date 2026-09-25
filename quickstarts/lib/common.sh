#!/usr/bin/env bash
# Shared helpers for Troshka quickstart scripts.
set -euo pipefail

_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUICKSTARTS_ROOT="$(cd "${_lib_dir}/.." && pwd)"
REPO_ROOT="$(cd "${QUICKSTARTS_ROOT}/.." && pwd)"

export REPO_ROOT QUICKSTARTS_ROOT

# Apply --profile / --region from argv before any aws calls.
# Usage: apply_aws_cli_args "$@"
# Sets/exports AWS_PROFILE and AWS_REGION when flags are present.
apply_aws_cli_args() {
  local args=("$@")
  local i=0
  while (( i < ${#args[@]} )); do
    case "${args[$i]}" in
      --profile)
        i=$((i + 1))
        if (( i >= ${#args[@]} )); then
          echo "error: --profile requires a value" >&2
          exit 1
        fi
        export AWS_PROFILE="${args[$i]}"
        ;;
      --profile=*)
        export AWS_PROFILE="${args[$i]#--profile=}"
        ;;
      --region)
        i=$((i + 1))
        if (( i >= ${#args[@]} )); then
          echo "error: --region requires a value" >&2
          exit 1
        fi
        export AWS_REGION="${args[$i]}"
        ;;
      --region=*)
        export AWS_REGION="${args[$i]#--region=}"
        ;;
    esac
    i=$((i + 1))
  done
}

# Resolve AWS region and print where it came from.
# Sets: REGION, REGION_SOURCE (exported).
resolve_aws_region() {
  if [[ -n "${AWS_REGION:-}" ]]; then
    REGION="${AWS_REGION}"
    REGION_SOURCE="AWS_REGION env"
  elif [[ -n "${AWS_DEFAULT_REGION:-}" ]]; then
    REGION="${AWS_DEFAULT_REGION}"
    REGION_SOURCE="AWS_DEFAULT_REGION env"
  else
    local cfg
    cfg="$(aws configure get region 2>/dev/null || true)"
    if [[ -n "${cfg}" ]]; then
      REGION="${cfg}"
      REGION_SOURCE="aws configure (~/.aws/config)"
    else
      REGION="us-east-1"
      REGION_SOURCE="script default (set AWS_REGION to override)"
    fi
  fi
  export REGION REGION_SOURCE
}

# Default dedicated kubeconfig for the EKS quickstart (avoids clobbering ~/.kube/config).
troshka_eks_kubeconfig_path() {
  local cluster="${TROSHKA_EKS_CLUSTER:-troshka-quickstart}"
  echo "${HOME}/.kube/troshka-eks-${cluster}.yaml"
}

# If KUBECONFIG is unset, warn and offer a Troshka-specific file.
# With --yes/--quiet/--no-verify: auto-use the Troshka path.
# Sets/exports KUBECONFIG. Pass script "$@" so skip_confirm works.
ensure_troshka_kubeconfig() {
  local default_kc
  default_kc="$(troshka_eks_kubeconfig_path)"

  if [[ -n "${KUBECONFIG:-}" ]]; then
    echo "KUBECONFIG=${KUBECONFIG}"
    export KUBECONFIG
    return 0
  fi

  echo
  echo "WARNING: KUBECONFIG is not set."
  echo "  Without it, aws eks update-kubeconfig writes into ~/.kube/config and can mix with other clusters."
  echo "  Recommended Troshka path: ${default_kc}"
  echo

  if skip_confirm "$@"; then
    mkdir -p "$(dirname "${default_kc}")"
    export KUBECONFIG="${default_kc}"
    echo "Using Troshka kubeconfig (quiet/no-verify): ${KUBECONFIG}"
    return 0
  fi

  read -r -p "Use dedicated Troshka kubeconfig at ${default_kc}? [Y/n] " reply
  case "${reply}" in
    ""|y|Y|yes|YES)
      mkdir -p "$(dirname "${default_kc}")"
      export KUBECONFIG="${default_kc}"
      echo "Using KUBECONFIG=${KUBECONFIG}"
      ;;
    *)
      echo "Continuing with default kubectl config (~/.kube/config)."
      echo "Tip: export KUBECONFIG=${default_kc}"
      ;;
  esac
}

# Describe how AWS credentials are being resolved (never print secrets).
# Sets: AWS_CREDS_SOURCE (exported).
resolve_aws_credential_source() {
  if [[ -n "${AWS_PROFILE:-}" ]]; then
    AWS_CREDS_SOURCE="AWS_PROFILE=${AWS_PROFILE}"
  elif [[ -n "${AWS_ACCESS_KEY_ID:-}" ]]; then
    local key_hint="${AWS_ACCESS_KEY_ID:0:4}…${AWS_ACCESS_KEY_ID: -4}"
    if [[ -n "${AWS_SESSION_TOKEN:-}" ]]; then
      AWS_CREDS_SOURCE="env AWS_ACCESS_KEY_ID (${key_hint}) + AWS_SESSION_TOKEN"
    else
      AWS_CREDS_SOURCE="env AWS_ACCESS_KEY_ID (${key_hint}) + AWS_SECRET_ACCESS_KEY"
    fi
  elif [[ -n "${AWS_SECRET_ACCESS_KEY:-}" ]]; then
    AWS_CREDS_SOURCE="env AWS_SECRET_ACCESS_KEY set but AWS_ACCESS_KEY_ID missing"
  elif [[ -n "${AWS_DEFAULT_PROFILE:-}" ]]; then
    AWS_CREDS_SOURCE="AWS_DEFAULT_PROFILE=${AWS_DEFAULT_PROFILE} / shared credentials file"
  else
    AWS_CREDS_SOURCE="default profile (~/.aws/credentials) or instance role"
  fi
  export AWS_CREDS_SOURCE
}

# Shared tip block for EKS install/teardown abort / help.
print_eks_tips() {
  local default_kc
  default_kc="$(troshka_eks_kubeconfig_path)"
  cat <<EOF
Tips:
  # Credentials (pick one)
  export AWS_PROFILE=my-profile
  ./quickstarts/eks/install.sh --profile my-profile
  # or without a profile:
  export AWS_ACCESS_KEY_ID=<YOUR_ACCESS_KEY_ID>
  export AWS_SECRET_ACCESS_KEY=<YOUR_SECRET_ACCESS_KEY>
  export AWS_SESSION_TOKEN=<YOUR_SESSION_TOKEN>   # if using temporary/STS credentials

  # Region / kubeconfig / stack
  export AWS_REGION=us-west-2            # (or your preferred region) and re-run
  ./quickstarts/eks/install.sh --region us-west-2
  export AWS_DEFAULT_REGION=us-west-2     # fallback if AWS_REGION unset
  export KUBECONFIG=${default_kc}
  export TROSHKA_EKS_STACK=my-stack      # CloudFormation stack name
  export TROSHKA_EKS_CLUSTER=my-cluster  # EKS cluster name

  # TLS / DNS / auth
  ./quickstarts/eks/install.sh                              # sslip.io + LE + basic auth
  ./quickstarts/eks/install.sh --domain troshka.example.com # Route53 CNAME + LE + basic auth
  ./quickstarts/eks/install.sh --production --domain troshka.example.com
  #   --production = RDS + S3 + ElastiCache + Cognito OIDC (requires --domain)

  # Managed data plane without Cognito
  ./quickstarts/eks/install.sh --use-rds
  ./quickstarts/eks/install.sh --use-s3
  ./quickstarts/eks/install.sh --use-elasticache

  # Non-interactive
  ./quickstarts/eks/install.sh --yes     # also --quiet / --no-verify
EOF
}

# Poll CloudFormation until a terminal status (does not hang on ROLLBACK like aws wait).
# Usage: wait_cloudformation_stack <stack> <region> [timeout_sec]
# Echoes final status to stdout; returns 0 on CREATE/UPDATE_COMPLETE or DELETE (gone), 1 otherwise.
wait_cloudformation_stack() {
  local stack="$1"
  local region="$2"
  local timeout_sec="${3:-5400}"
  local interval_sec="${TROSHKA_CFN_POLL_INTERVAL:-15}"
  local deadline=$((SECONDS + timeout_sec))
  local status=""

  while (( SECONDS < deadline )); do
    status="$(aws cloudformation describe-stacks --stack-name "${stack}" --region "${region}" \
      --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DELETE_COMPLETE")"

    case "${status}" in
      CREATE_COMPLETE|UPDATE_COMPLETE|DELETE_COMPLETE)
        echo "${status}"
        return 0
        ;;
      CREATE_FAILED|ROLLBACK_COMPLETE|ROLLBACK_FAILED|UPDATE_FAILED|UPDATE_ROLLBACK_COMPLETE|UPDATE_ROLLBACK_FAILED|DELETE_FAILED|IMPORT_ROLLBACK_COMPLETE|IMPORT_ROLLBACK_FAILED)
        echo "${status}"
        return 1
        ;;
      *_IN_PROGRESS|*_IN_PROGRESS_*|REVIEW_IN_PROGRESS|"")
        echo "  stack status: ${status:-unknown} (polling every ${interval_sec}s)..." >&2
        sleep "${interval_sec}"
        ;;
      *)
        # Unexpected but non-terminal — keep polling briefly
        echo "  stack status: ${status} (polling)..." >&2
        sleep "${interval_sec}"
        ;;
    esac
  done

  echo "${status:-TIMEOUT}"
  return 1
}

print_cfn_failure_events() {
  local stack="$1"
  local region="$2"
  echo "Failed resource events:" >&2
  aws cloudformation describe-stack-events --stack-name "${stack}" --region "${region}" \
    --query 'StackEvents[?contains(ResourceStatus, `FAILED`)].[Timestamp,LogicalResourceId,ResourceStatus,ResourceStatusReason]' \
    --output table >&2 || true
}

# Delete a CloudFormation stack and wait until it is gone (or fail fast).
delete_cloudformation_stack() {
  local stack="$1"
  local region="$2"
  echo "Deleting CloudFormation stack ${stack} in ${region}..."
  aws cloudformation delete-stack --stack-name "${stack}" --region "${region}"
  local status
  if status="$(wait_cloudformation_stack "${stack}" "${region}" "${TROSHKA_CFN_DELETE_TIMEOUT:-1800}")"; then
    echo "Stack ${stack} deleted (${status})."
    return 0
  fi
  echo "error: stack delete did not finish cleanly (${status})" >&2
  return 1
}


# Install hint for a missing CLI (macOS brew first; Linux notes second).
_cmd_install_hint() {
  case "$1" in
    aws)
      echo "  AWS CLI: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
      echo "    macOS: brew install awscli"
      ;;
    helm)
      echo "  Helm 3: https://helm.sh/docs/intro/install/"
      echo "    macOS: brew install helm"
      ;;
    kubectl)
      echo "  kubectl: https://kubernetes.io/docs/tasks/tools/"
      echo "    macOS: brew install kubectl"
      ;;
    oc)
      echo "  OpenShift CLI (oc): https://docs.openshift.com/container-platform/latest/cli_reference/openshift_cli/getting-started-cli.html"
      echo "    macOS: brew install openshift-cli"
      ;;
    curl)
      echo "  curl: usually preinstalled; macOS: brew install curl"
      ;;
    jq)
      echo "  jq: https://jqlang.github.io/jq/download/"
      echo "    macOS: brew install jq"
      ;;
    docker)
      echo "  Docker Desktop / engine with Compose v2: https://docs.docker.com/get-docker/"
      echo "    macOS: brew install --cask docker"
      ;;
    podman)
      echo "  Podman with Compose: https://podman.io/docs/installation"
      echo "    macOS: brew install podman"
      ;;
    openssl)
      echo "  openssl: macOS: brew install openssl"
      ;;
    *)
      echo "  Install '$1' via your OS package manager (e.g. brew install $1)"
      ;;
  esac
}

# Fail once with every missing tool + install hints (pre-run check).
# Usage: require_cmd aws helm kubectl curl jq
require_cmd() {
  local missing=()
  local c
  for c in "$@"; do
    if ! command -v "$c" >/dev/null 2>&1; then
      missing+=("$c")
    fi
  done
  if ((${#missing[@]} == 0)); then
    return 0
  fi
  echo "Pre-run check failed — missing required command(s): ${missing[*]}" >&2
  echo >&2
  echo "Install the missing tools, then re-run this script:" >&2
  for c in "${missing[@]}"; do
    _cmd_install_hint "$c" >&2
    echo >&2
  done
  exit 1
}

# Local rail: need either "podman compose" or "docker compose".
require_compose() {
  if command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
    return 0
  fi
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    return 0
  fi
  echo "Pre-run check failed — need Podman or Docker with Compose v2." >&2
  echo >&2
  echo "Install one of:" >&2
  _cmd_install_hint podman >&2
  echo >&2
  _cmd_install_hint docker >&2
  echo >&2
  exit 1
}

# OCP rail: need oc or kubectl.
require_kube_cli() {
  if command -v oc >/dev/null 2>&1 || command -v kubectl >/dev/null 2>&1; then
    return 0
  fi
  echo "Pre-run check failed — need oc or kubectl." >&2
  echo >&2
  _cmd_install_hint oc >&2
  echo >&2
  _cmd_install_hint kubectl >&2
  echo >&2
  exit 1
}

# True if the user opted out of interactive confirmations.
# Flags: --yes / -y / --quiet / -q / --no-verify
# Env:   TROSHKA_YES=1 or TROSHKA_NO_VERIFY=1
skip_confirm() {
  local arg
  for arg in "$@"; do
    case "$arg" in
      --yes|-y|--quiet|-q|--no-verify) return 0 ;;
    esac
  done
  if [[ "${TROSHKA_YES:-}" == "1" || "${TROSHKA_NO_VERIFY:-}" == "1" ]]; then
    return 0
  fi
  return 1
}

# Backward-compatible alias.
has_yes_flag() {
  skip_confirm "$@"
}

confirm() {
  local prompt="$1"
  shift || true
  if skip_confirm "$@"; then
    echo "Skipping confirm (--yes/--quiet/--no-verify): proceeding with defaults."
    return 0
  fi
  read -r -p "${prompt} [y/N] " reply
  [[ "$reply" == "y" || "$reply" == "Y" || "$reply" == "yes" ]]
}

# curl helpers that honor Troshka auth env vars (basic and/or forwarded SSO headers).
# Env: TROSHKA_BASIC_USER, TROSHKA_BASIC_PASSWORD, TROSHKA_FORWARDED_EMAIL,
#      TROSHKA_CURL_INSECURE=1 (-k)
troshka_curl() {
  local -a args=(-fsS)
  if [[ "${TROSHKA_CURL_INSECURE:-}" == "1" || "${TROSHKA_CURL_INSECURE:-}" == "true" ]]; then
    args+=(-k)
  fi
  if [[ -n "${TROSHKA_BASIC_USER:-}" && -n "${TROSHKA_BASIC_PASSWORD:-}" ]]; then
    args+=(-u "${TROSHKA_BASIC_USER}:${TROSHKA_BASIC_PASSWORD}")
  fi
  if [[ -n "${TROSHKA_FORWARDED_EMAIL:-}" ]]; then
    args+=(-H "X-Forwarded-Email: ${TROSHKA_FORWARDED_EMAIL}")
    args+=(-H "X-Forwarded-User: ${TROSHKA_FORWARDED_EMAIL}")
  fi
  curl "${args[@]}" "$@"
}

api_get() {
  local path="$1"
  troshka_curl "${TROSHKA_API_URL}${path}"
}

api_post() {
  local path="$1"
  local body="${2:-}"
  if [[ -n "${body}" ]]; then
    troshka_curl -X POST -H "Content-Type: application/json" -d "${body}" \
      "${TROSHKA_API_URL}${path}"
  else
    troshka_curl -X POST "${TROSHKA_API_URL}${path}"
  fi
}

api_delete() {
  local path="$1"
  troshka_curl -X DELETE "${TROSHKA_API_URL}${path}"
}

# Port-forward to in-cluster backend for API wipe (bypasses ingress auth / TLS).
# Usage: start_backend_port_forward <namespace> [local_port]
# Sets TROSHKA_API_URL and TROSHKA_PF_PID; call stop_backend_port_forward after.
start_backend_port_forward() {
  local ns="$1"
  local local_port="${2:-18200}"
  require_cmd kubectl
  kubectl -n "${ns}" rollout status deployment/troshka-backend --timeout=60s >/dev/null 2>&1 || true
  kubectl -n "${ns}" port-forward svc/troshka-backend "${local_port}:8200" >/tmp/troshka-pf.log 2>&1 &
  TROSHKA_PF_PID=$!
  export TROSHKA_PF_PID
  local i
  for i in $(seq 1 30); do
    if curl -fsS -o /dev/null "http://127.0.0.1:${local_port}/api/v1/health" 2>/dev/null; then
      export TROSHKA_API_URL="http://127.0.0.1:${local_port}"
      return 0
    fi
    sleep 1
  done
  echo "error: backend port-forward did not become ready (see /tmp/troshka-pf.log)" >&2
  stop_backend_port_forward || true
  return 1
}

stop_backend_port_forward() {
  if [[ -n "${TROSHKA_PF_PID:-}" ]]; then
    kill "${TROSHKA_PF_PID}" 2>/dev/null || true
    wait "${TROSHKA_PF_PID}" 2>/dev/null || true
    unset TROSHKA_PF_PID
  fi
}

# Read oauth_enabled + first admin_users email from the live ConfigMap (if present).
# Sets: TROSHKA_OAUTH_ENABLED (true/false), TROSHKA_ADMIN_FROM_CM (email or empty)
read_troshka_auth_from_cluster() {
  local ns="${1:-troshka}"
  TROSHKA_OAUTH_ENABLED=false
  TROSHKA_ADMIN_FROM_CM=""
  local cm
  cm="$(kubectl -n "${ns}" get configmap troshka-config -o jsonpath='{.data.config\.yaml}' 2>/dev/null || true)"
  if [[ -z "${cm}" ]]; then
    export TROSHKA_OAUTH_ENABLED TROSHKA_ADMIN_FROM_CM
    return 0
  fi
  if echo "${cm}" | grep -qE 'oauth_enabled:[[:space:]]*true'; then
    TROSHKA_OAUTH_ENABLED=true
  fi
  TROSHKA_ADMIN_FROM_CM="$(echo "${cm}" | sed -n 's/.*admin_users:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1 | cut -d, -f1 | tr -d ' ')"
  export TROSHKA_OAUTH_ENABLED TROSHKA_ADMIN_FROM_CM
}

# Wait for a Service LoadBalancer hostname/IP. Echoes address to stdout.
# Usage: wait_lb_hostname <namespace> <service> [timeout_sec]
wait_lb_hostname() {
  local ns="$1"
  local svc="$2"
  local timeout_sec="${3:-600}"
  local deadline=$((SECONDS + timeout_sec))
  local addr=""
  while (( SECONDS < deadline )); do
    addr="$(kubectl -n "${ns}" get svc "${svc}" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
    if [[ -z "${addr}" ]]; then
      addr="$(kubectl -n "${ns}" get svc "${svc}" -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
    fi
    if [[ -n "${addr}" ]]; then
      echo "${addr}"
      return 0
    fi
    sleep 10
  done
  echo "error: timed out waiting for ${ns}/${svc} LoadBalancer address" >&2
  return 1
}

# Resolve a DNS name to the first A record (IPv4). Echoes IP or empty.
resolve_ipv4() {
  local name="$1"
  # dig preferred; getent/host as fallbacks
  if command -v dig >/dev/null 2>&1; then
    dig +short A "${name}" | awk '/^[0-9]+\./ { print; exit }'
    return 0
  fi
  if command -v getent >/dev/null 2>&1; then
    getent ahostsv4 "${name}" 2>/dev/null | awk '{ print $1; exit }'
    return 0
  fi
  python3 -c 'import socket,sys; print(socket.gethostbyname(sys.argv[1]))' "${name}" 2>/dev/null || true
}

# Find a Route53 public hosted zone that can hold records for fqdn.
# Walks parent labels (a.b.example.com → b.example.com → example.com).
# Echoes zone id (without /hostedzone/ prefix) or empty.
find_route53_zone_id() {
  local fqdn="${1%.}"
  local candidate="${fqdn}"
  local zone_id=""
  while [[ "${candidate}" == *.* ]]; do
    zone_id="$(aws route53 list-hosted-zones-by-name --dns-name "${candidate}." \
      --query "HostedZones[?Name=='${candidate}.' && Config.PrivateZone==\`false\`].Id | [0]" \
      --output text 2>/dev/null || true)"
    zone_id="${zone_id##*/}"
    if [[ -n "${zone_id}" && "${zone_id}" != "None" && "${zone_id}" != "null" ]]; then
      echo "${zone_id}"
      return 0
    fi
    candidate="${candidate#*.}"
  done
  return 1
}

# Upsert a CNAME to an ELB/NLB DNS name (use a subdomain, not the zone apex).
# Usage: upsert_route53_cname <zone_id> <fqdn> <target_dns>
upsert_route53_cname() {
  local zone_id="$1"
  local fqdn="${2%.}"
  local target="${3%.}"
  local change
  change="$(cat <<EOF
{
  "Comment": "Troshka EKS quickstart",
  "Changes": [{
    "Action": "UPSERT",
    "ResourceRecordSet": {
      "Name": "${fqdn}.",
      "Type": "CNAME",
      "TTL": 60,
      "ResourceRecords": [{ "Value": "${target}" }]
    }
  }]
}
EOF
)"
  aws route53 change-resource-record-sets --hosted-zone-id "${zone_id}" --change-batch "${change}" >/dev/null
}

# Create a Cognito admin user with a permanent password (idempotent).
# Usage: ensure_cognito_admin <user_pool_id> <email> <password> <region>
ensure_cognito_admin() {
  local pool="$1"
  local email="$2"
  local password="$3"
  local region="$4"
  if aws cognito-idp admin-get-user --user-pool-id "${pool}" --username "${email}" --region "${region}" >/dev/null 2>&1; then
    aws cognito-idp admin-set-user-password \
      --user-pool-id "${pool}" \
      --username "${email}" \
      --password "${password}" \
      --permanent \
      --region "${region}" >/dev/null
    return 0
  fi
  aws cognito-idp admin-create-user \
    --user-pool-id "${pool}" \
    --username "${email}" \
    --user-attributes Name=email,Value="${email}" Name=email_verified,Value=true \
    --message-action SUPPRESS \
    --region "${region}" >/dev/null
  aws cognito-idp admin-set-user-password \
    --user-pool-id "${pool}" \
    --username "${email}" \
    --password "${password}" \
    --permanent \
    --region "${region}" >/dev/null
}
