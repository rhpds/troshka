#!/usr/bin/env bash
# One-button Troshka on Amazon EKS: CloudFormation VPC+EKS, then Helm.
#
# Default: ingress-nginx + sslip.io + Let's Encrypt + nginx basic auth.
# --production --domain FQDN: RDS+S3+ElastiCache + Route53 + Cognito OIDC
#   (oauth2-proxy). Requires a Route53 public zone that can hold FQDN (subdomain).
set -euo pipefail

_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "${_script_dir}/../lib/common.sh"

STACK_NAME="${TROSHKA_EKS_STACK:-troshka-eks-quickstart}"
CLUSTER_NAME="${TROSHKA_EKS_CLUSTER:-troshka-quickstart}"
NAMESPACE="${TROSHKA_NAMESPACE:-troshka}"
RELEASE="${TROSHKA_RELEASE:-troshka}"
CFN_TEMPLATE="${REPO_ROOT}/deploy/eks/cloudformation/troshka-eks.yaml"
INGRESS_NS="${TROSHKA_INGRESS_NS:-ingress-nginx}"
CERT_MANAGER_NS="${TROSHKA_CERT_MANAGER_NS:-cert-manager}"
ACME_EMAIL="${TROSHKA_ACME_EMAIL:-admin@sslip.io}"

apply_aws_cli_args "$@"

USE_RDS="${TROSHKA_EKS_USE_RDS:-false}"
USE_S3="${TROSHKA_EKS_USE_S3:-false}"
USE_ELASTICACHE="${TROSHKA_EKS_USE_ELASTICACHE:-false}"
USE_COGNITO=false
PRODUCTION=false
APP_DOMAIN="${TROSHKA_DOMAIN:-}"
ADMIN_EMAIL="${TROSHKA_ADMIN_EMAIL:-}"
ADMIN_PASSWORD="${TROSHKA_ADMIN_PASSWORD:-}"

args=("$@")
i=0
while (( i < ${#args[@]} )); do
  case "${args[$i]}" in
    --use-rds) USE_RDS=true ;;
    --use-s3) USE_S3=true ;;
    --use-elasticache|--use-redis) USE_ELASTICACHE=true ;;
    --production)
      PRODUCTION=true
      USE_RDS=true
      USE_S3=true
      USE_ELASTICACHE=true
      USE_COGNITO=true
      ;;
    --domain)
      i=$((i + 1))
      APP_DOMAIN="${args[$i]:-}"
      ;;
    --domain=*)
      APP_DOMAIN="${args[$i]#--domain=}"
      ;;
    --admin-email)
      i=$((i + 1))
      ADMIN_EMAIL="${args[$i]:-}"
      ;;
    --admin-email=*)
      ADMIN_EMAIL="${args[$i]#--admin-email=}"
      ;;
    --admin-password)
      i=$((i + 1))
      ADMIN_PASSWORD="${args[$i]:-}"
      ;;
    --admin-password=*)
      ADMIN_PASSWORD="${args[$i]#--admin-password=}"
      ;;
  esac
  i=$((i + 1))
done

if [[ "${TROSHKA_EKS_PRODUCTION:-}" == "1" || "${TROSHKA_EKS_PRODUCTION:-}" == "true" ]]; then
  PRODUCTION=true
  USE_RDS=true
  USE_S3=true
  USE_ELASTICACHE=true
  USE_COGNITO=true
fi

if [[ "${PRODUCTION}" == "true" && -z "${APP_DOMAIN}" ]]; then
  echo "error: --production requires --domain <fqdn> (Route53 + Cognito callback URL)" >&2
  echo "  Example: ./quickstarts/eks/install.sh --production --domain troshka.example.com" >&2
  exit 1
fi

if [[ "${USE_COGNITO}" == "true" && -z "${APP_DOMAIN}" ]]; then
  echo "error: Cognito requires --domain <fqdn>" >&2
  exit 1
fi

CFN_PARAMETERS=(
  "ParameterKey=ClusterName,ParameterValue=${CLUSTER_NAME}"
  "ParameterKey=UseRDS,ParameterValue=${USE_RDS}"
  "ParameterKey=UseS3,ParameterValue=${USE_S3}"
  "ParameterKey=UseElastiCache,ParameterValue=${USE_ELASTICACHE}"
  "ParameterKey=UseCognito,ParameterValue=${USE_COGNITO}"
  "ParameterKey=AppHostname,ParameterValue=${APP_DOMAIN}"
)

stack_output() {
  aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null || true
}

urlencode() {
  python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
}

random_password() {
  # 24 chars, Cognito-friendly (upper/lower/digit, no ambiguous symbols).
  python3 -c 'import secrets,string; a=string.ascii_letters+string.digits; print("".join(secrets.choice(a) for _ in range(24))+"Aa1")'
}

echo "Pre-run check (aws, helm, kubectl, curl, jq)..."
require_cmd aws helm kubectl curl jq
resolve_aws_region
ensure_troshka_kubeconfig "$@"
resolve_aws_credential_source

if ! aws sts get-caller-identity --region "${REGION}" >/dev/null 2>&1; then
  echo "Pre-run check failed — AWS credentials not configured for region ${REGION}." >&2
  echo "  Run: aws configure   (or export AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN)" >&2
  echo "  Then: aws sts get-caller-identity --region ${REGION}" >&2
  print_eks_tips >&2
  exit 1
fi

ACCOUNT_ID="$(aws sts get-caller-identity --region "${REGION}" --query Account --output text)"
CALLER_ARN="$(aws sts get-caller-identity --region "${REGION}" --query Arn --output text)"
CALLER_USER="$(aws sts get-caller-identity --region "${REGION}" --query UserId --output text)"

PG_MODE="in-cluster Postgres"
S3_MODE="in-cluster S4"
REDIS_MODE="in-cluster Redis"
AUTH_MODE="nginx basic auth + sslip.io HTTPS"
[[ "${USE_RDS}" == "true" ]] && PG_MODE="RDS PostgreSQL"
[[ "${USE_S3}" == "true" ]] && S3_MODE="Amazon S3"
[[ "${USE_ELASTICACHE}" == "true" ]] && REDIS_MODE="ElastiCache Redis"
if [[ "${USE_COGNITO}" == "true" ]]; then
  AUTH_MODE="Cognito OIDC (oauth2-proxy) + Route53 HTTPS (${APP_DOMAIN})"
elif [[ -n "${APP_DOMAIN}" ]]; then
  AUTH_MODE="nginx basic auth + Route53 HTTPS (${APP_DOMAIN})"
fi

cat <<EOF

WARNING: About to create/update an EKS cluster and VPC in this AWS account.
This incurs cost (EKS control plane, NAT Gateway, EC2 nodes, NLB, etc.).

  Account:    ${ACCOUNT_ID}
  Identity:   ${CALLER_ARN}
  UserId:     ${CALLER_USER}
  Creds:      ${AWS_CREDS_SOURCE}
  Region:     ${REGION}  ← from ${REGION_SOURCE}
  Stack:      ${STACK_NAME}
  Cluster:    ${CLUSTER_NAME}
  KUBECONFIG: ${KUBECONFIG:-~/.kube/config (default)}
  Postgres:   ${PG_MODE}
  Objects:    ${S3_MODE}
  Redis:      ${REDIS_MODE}
  Auth/TLS:   ${AUTH_MODE}

  Default: sslip.io + Let's Encrypt + basic auth + Fedora host
  --domain FQDN: Route53 CNAME + LE (subdomain, not zone apex)
  --production --domain FQDN: RDS+S3+ElastiCache + Cognito OIDC + RHEL host
  Skip this prompt: --yes / --quiet / --no-verify

EOF

confirm "Proceed in account ${ACCOUNT_ID} / region ${REGION}?" "$@" || {
  echo "Aborted — no changes made."
  print_eks_tips
  exit 1
}

echo "Deploying CloudFormation stack ${STACK_NAME} in ${REGION}..."
EXISTING_STATUS="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true)"

case "${EXISTING_STATUS}" in
  ROLLBACK_COMPLETE|ROLLBACK_FAILED|CREATE_FAILED|DELETE_FAILED|UPDATE_ROLLBACK_COMPLETE|UPDATE_ROLLBACK_FAILED|UPDATE_FAILED|ROLLBACK_IN_PROGRESS)
    if [[ "${EXISTING_STATUS}" == "ROLLBACK_IN_PROGRESS" ]]; then
      echo "Stack ${STACK_NAME} is still rolling back — waiting..."
      wait_cloudformation_stack "${STACK_NAME}" "${REGION}" "${TROSHKA_CFN_WAIT_TIMEOUT:-5400}" || true
      EXISTING_STATUS="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" \
        --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true)"
    fi
    echo
    echo "Found existing failed stack ${STACK_NAME} (${EXISTING_STATUS})."
    echo "It must be deleted before a new install can create the same stack name."
    echo
    confirm "Delete failed stack ${STACK_NAME} in ${REGION} and continue install?" "$@" || {
      echo "Aborted — left stack ${STACK_NAME} (${EXISTING_STATUS}) in place."
      echo "Delete manually when ready:"
      echo "  aws cloudformation delete-stack --stack-name ${STACK_NAME} --region ${REGION}"
      exit 1
    }
    delete_cloudformation_stack "${STACK_NAME}" "${REGION}"
    EXISTING_STATUS=""
    ;;
esac

if [[ -n "${EXISTING_STATUS}" && "${EXISTING_STATUS}" != "None" ]]; then
  if aws cloudformation update-stack \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}" \
    --template-body "file://${CFN_TEMPLATE}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameters "${CFN_PARAMETERS[@]}" 2>/tmp/troshka-cfn-update.err; then
    :
  else
    if grep -qi "No updates are to be performed" /tmp/troshka-cfn-update.err 2>/dev/null; then
      echo "No CloudFormation updates needed."
      EXISTING_STATUS="UPDATE_COMPLETE"
    else
      cat /tmp/troshka-cfn-update.err >&2 || true
      echo "error: update-stack failed" >&2
      exit 1
    fi
  fi
  if [[ "${EXISTING_STATUS}" != "UPDATE_COMPLETE" ]]; then
    echo "Waiting for stack UPDATE_COMPLETE (polling; exits on rollback/failure)..."
    STATUS="$(wait_cloudformation_stack "${STACK_NAME}" "${REGION}" "${TROSHKA_CFN_WAIT_TIMEOUT:-5400}")" || {
      echo "error: stack not healthy (${STATUS})" >&2
      print_cfn_failure_events "${STACK_NAME}" "${REGION}"
      echo "Re-run install — failed stacks are deleted automatically after confirm." >&2
      exit 1
    }
  else
    STATUS="UPDATE_COMPLETE"
  fi
else
  aws cloudformation create-stack \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}" \
    --template-body "file://${CFN_TEMPLATE}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameters "${CFN_PARAMETERS[@]}"
  echo "Waiting for stack CREATE_COMPLETE (polling; exits on rollback/failure)..."
  STATUS="$(wait_cloudformation_stack "${STACK_NAME}" "${REGION}" "${TROSHKA_CFN_WAIT_TIMEOUT:-5400}")" || {
    echo "error: stack not healthy (${STATUS})" >&2
    print_cfn_failure_events "${STACK_NAME}" "${REGION}"
    echo
    confirm "Delete failed stack ${STACK_NAME} now?" "$@" || {
      echo "Left stack ${STACK_NAME} (${STATUS}) in place. Re-run install later to clean up."
      exit 1
    }
    delete_cloudformation_stack "${STACK_NAME}" "${REGION}" || true
    echo "Re-run ./quickstarts/eks/install.sh after fixing the template/permissions error." >&2
    exit 1
  }
fi

echo "Stack status: ${STATUS}"
case "${STATUS}" in
  CREATE_COMPLETE|UPDATE_COMPLETE) ;;
  *)
    echo "error: unexpected stack status ${STATUS}" >&2
    exit 1
    ;;
esac

aws eks update-kubeconfig --name "${CLUSTER_NAME}" --region "${REGION}"

echo "Installing ingress-nginx (NLB)..."
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx >/dev/null 2>&1 || true
helm repo update ingress-nginx >/dev/null 2>&1 || true
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace "${INGRESS_NS}" \
  --create-namespace \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-type"=nlb \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-scheme"=internet-facing \
  --wait --timeout 10m

echo "Installing cert-manager..."
helm repo add jetstack https://charts.jetstack.io >/dev/null 2>&1 || true
helm repo update jetstack >/dev/null 2>&1 || true
helm upgrade --install cert-manager jetstack/cert-manager \
  --namespace "${CERT_MANAGER_NS}" \
  --create-namespace \
  --set crds.enabled=true \
  --wait --timeout 10m

kubectl apply -f - <<EOF
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: ${ACME_EMAIL}
    privateKeySecretRef:
      name: letsencrypt-prod-account
    solvers:
      - http01:
          ingress:
            class: nginx
EOF

echo "Waiting for ingress-nginx LoadBalancer..."
LB_HOSTNAME="$(wait_lb_hostname "${INGRESS_NS}" ingress-nginx-controller 900)"
echo "NLB hostname: ${LB_HOSTNAME}"

echo "Ensuring default gp3 StorageClass (EBS CSI)..."
kubectl apply -f - <<'EOF'
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: ebs.csi.aws.com
parameters:
  type: gp3
  fsType: ext4
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
EOF

# Allow the current AWS caller to use kubectl after install (CFN creator may differ).
CALLER_ARN_RAW="$(aws sts get-caller-identity --region "${REGION}" --query Arn --output text)"
# Normalize assumed-role session ARN → role ARN for access entries.
DEPLOYER_PRINCIPAL="${CALLER_ARN_RAW}"
if [[ "${CALLER_ARN_RAW}" == *":assumed-role/"* ]]; then
  _role_name="${CALLER_ARN_RAW#*:assumed-role/}"
  _role_name="${_role_name%%/*}"
  DEPLOYER_PRINCIPAL="arn:aws:iam::${ACCOUNT_ID}:role/${_role_name}"
fi
if aws eks create-access-entry --cluster-name "${CLUSTER_NAME}" --principal-arn "${DEPLOYER_PRINCIPAL}" --region "${REGION}" 2>/dev/null; then
  aws eks associate-access-policy --cluster-name "${CLUSTER_NAME}" --principal-arn "${DEPLOYER_PRINCIPAL}" \
    --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \
    --access-scope type=cluster --region "${REGION}" >/dev/null 2>&1 || true
  echo "EKS access entry ensured for ${DEPLOYER_PRINCIPAL}"
fi

BASIC_USER="admin"
BASIC_PASS=""
INGRESS_HOST=""
HELM_SETS=(
  -f "${REPO_ROOT}/deploy/helm/values-eks.yaml"
)

if [[ -n "${APP_DOMAIN}" ]]; then
  INGRESS_HOST="${APP_DOMAIN}"
  echo "Looking up Route53 zone for ${APP_DOMAIN}..."
  ZONE_ID="$(find_route53_zone_id "${APP_DOMAIN}")" || {
    echo "error: no public Route53 hosted zone found for ${APP_DOMAIN}" >&2
    echo "  Create a public hosted zone for the parent domain and re-run." >&2
    exit 1
  }
  echo "Upserting CNAME ${APP_DOMAIN} → ${LB_HOSTNAME} (zone ${ZONE_ID})..."
  upsert_route53_cname "${ZONE_ID}" "${APP_DOMAIN}" "${LB_HOSTNAME}"
else
  echo "Resolving NLB to IPv4 for sslip.io..."
  LB_IP=""
  for _ in $(seq 1 30); do
    LB_IP="$(resolve_ipv4 "${LB_HOSTNAME}")"
    if [[ -n "${LB_IP}" ]]; then
      break
    fi
    sleep 10
  done
  if [[ -z "${LB_IP}" ]]; then
    echo "error: could not resolve ${LB_HOSTNAME} to an A record" >&2
    exit 1
  fi
  INGRESS_HOST="troshka.${LB_IP}.sslip.io"
  echo "Using sslip.io host: ${INGRESS_HOST}"
fi

HELM_SETS+=("--set-string" "ingress.host=${INGRESS_HOST}")

if [[ "${USE_COGNITO}" == "true" ]]; then
  COGNITO_POOL="$(stack_output CognitoUserPoolId)"
  COGNITO_CLIENT_ID="$(stack_output CognitoClientId)"
  COGNITO_CLIENT_SECRET="$(stack_output CognitoClientSecret)"
  COGNITO_ISSUER="$(stack_output CognitoIssuerUrl)"
  if [[ -z "${COGNITO_POOL}" || "${COGNITO_POOL}" == "None" ]]; then
    echo "error: Cognito outputs missing from stack — was UseCognito=true?" >&2
    exit 1
  fi
  if [[ -z "${ADMIN_EMAIL}" ]]; then
    ADMIN_EMAIL="admin@${APP_DOMAIN}"
  fi
  if [[ -z "${ADMIN_PASSWORD}" ]]; then
    ADMIN_PASSWORD="$(random_password)"
  fi
  echo "Ensuring Cognito admin user ${ADMIN_EMAIL}..."
  ensure_cognito_admin "${COGNITO_POOL}" "${ADMIN_EMAIL}" "${ADMIN_PASSWORD}" "${REGION}"

  HELM_SETS+=(
    --set auth.oauthEnabled=true
    --set auth.basicAuth.enabled=false
    --set auth.oauth2Proxy.enabled=true
    "--set-string" "auth.oauth2Proxy.clientId=${COGNITO_CLIENT_ID}"
    "--set-string" "auth.oauth2Proxy.clientSecret=${COGNITO_CLIENT_SECRET}"
    "--set-string" "auth.oauth2Proxy.oidcIssuerUrl=${COGNITO_ISSUER}"
    "--set-string" "auth.oauth2Proxy.redirectUrl=https://${INGRESS_HOST}/oauth2/callback"
    "--set-string" "auth.adminUsers=${ADMIN_EMAIL}"
  )
else
  BASIC_PASS="$(random_password)"
  HELM_SETS+=(
    --set auth.oauthEnabled=false
    --set auth.oauth2Proxy.enabled=false
    --set auth.basicAuth.enabled=true
    "--set-string" "auth.basicAuth.username=${BASIC_USER}"
    "--set-string" "auth.basicAuth.password=${BASIC_PASS}"
  )
fi

# Persist credentials before helm --wait so a later script/edit failure still leaves them.
CREDS_DIR="${HOME}/.troshka"
mkdir -p "${CREDS_DIR}"
CREDS_FILE="${CREDS_DIR}/eks-${CLUSTER_NAME}-creds.txt"
umask 077
{
  echo "cluster=${CLUSTER_NAME}"
  echo "region=${REGION}"
  echo "url=https://${INGRESS_HOST}"
  if [[ "${USE_COGNITO}" == "true" ]]; then
    echo "auth=cognito"
    echo "admin_email=${ADMIN_EMAIL}"
    echo "admin_password=${ADMIN_PASSWORD}"
  else
    echo "auth=basic"
    echo "username=${BASIC_USER}"
    echo "password=${BASIC_PASS}"
  fi
} > "${CREDS_FILE}"
echo "Credentials saved to ${CREDS_FILE} (printed again when install finishes)."

if [[ "${USE_RDS}" == "true" ]]; then
  require_cmd python3
  RDS_HOST="$(stack_output RdsEndpoint)"
  RDS_SECRET_ARN="$(stack_output RdsSecretArn)"
  RDS_PASS="$(aws secretsmanager get-secret-value --secret-id "${RDS_SECRET_ARN}" --region "${REGION}" \
    --query SecretString --output text | jq -r .password)"
  RDS_PASS_ENC="$(urlencode "${RDS_PASS}")"
  DB_URL="postgresql+psycopg2://troshka:${RDS_PASS_ENC}@${RDS_HOST}:5432/troshka"
  HELM_SETS+=(--set postgres.deploy=false "--set-string" "secrets.databaseUrl=${DB_URL}" "--set-string" "postgres.password=${RDS_PASS}")
fi

if [[ "${USE_S3}" == "true" ]]; then
  S3_BUCKET="$(stack_output S3Bucket)"
  S3_SECRET_ARN="$(stack_output S3SecretArn)"
  S3_JSON="$(aws secretsmanager get-secret-value --secret-id "${S3_SECRET_ARN}" --region "${REGION}" --query SecretString --output text)"
  S3_AK="$(echo "${S3_JSON}" | jq -r .access_key_id)"
  S3_SK="$(echo "${S3_JSON}" | jq -r .secret_access_key)"
  HELM_SETS+=(
    --set s4.deploy=false
    --set s3.useAws=true
    "--set-string" "s3.bucket=${S3_BUCKET}"
    "--set-string" "secrets.s3AccessKey=${S3_AK}"
    "--set-string" "secrets.s3SecretKey=${S3_SK}"
  )
fi

if [[ "${USE_ELASTICACHE}" == "true" ]]; then
  require_cmd python3
  REDIS_HOST="$(stack_output RedisPrimaryEndpoint)"
  REDIS_SECRET_ARN="$(stack_output RedisSecretArn)"
  REDIS_PASS="$(aws secretsmanager get-secret-value --secret-id "${REDIS_SECRET_ARN}" --region "${REGION}" --query SecretString --output text)"
  REDIS_PASS_ENC="$(urlencode "${REDIS_PASS}")"
  REDIS_URL="rediss://:${REDIS_PASS_ENC}@${REDIS_HOST}:6379/0"
  HELM_SETS+=(--set redis.deploy=false "--set-string" "redis.url=${REDIS_URL}")
fi

echo "Installing Troshka Helm chart..."
helm upgrade --install "${RELEASE}" "${REPO_ROOT}/deploy/helm" \
  --namespace "${NAMESPACE}" \
  --create-namespace \
  "${HELM_SETS[@]}" \
  --wait \
  --timeout 20m

echo "Waiting for TLS certificate (Let's Encrypt)..."
for _ in $(seq 1 60); do
  if kubectl -n "${NAMESPACE}" get secret troshka-tls >/dev/null 2>&1; then
    echo "TLS secret troshka-tls present."
    break
  fi
  sleep 10
done

UI_URL="https://${INGRESS_HOST}"
export TROSHKA_API_URL="${UI_URL}"
# Health is skip-auth on oauth2-proxy; basic-auth path needs credentials.
export TROSHKA_HEALTH_PATH="/api/v1/health"
if [[ "${USE_COGNITO}" != "true" ]]; then
  export TROSHKA_BASIC_USER="${BASIC_USER}"
  export TROSHKA_BASIC_PASSWORD="${BASIC_PASS}"
fi
# Allow a brief window if the cert is still propagating.
export TROSHKA_CURL_INSECURE=1
TROSHKA_WAIT_TIMEOUT=300 "${_script_dir}/../lib/wait-for-api.sh" || true
unset TROSHKA_CURL_INSECURE

# Seed EC2 provider + one host (Fedora quickstart / RHEL --production).
# Port-forward bypasses nginx basic auth and Cognito oauth2-proxy.
echo "Seeding Troshka compute provider + host..."
trap stop_backend_port_forward EXIT
if start_backend_port_forward "${NAMESPACE}"; then
  read_troshka_auth_from_cluster "${NAMESPACE}"
  if [[ "${TROSHKA_OAUTH_ENABLED}" == "true" ]]; then
    export TROSHKA_FORWARDED_EMAIL="${ADMIN_EMAIL:-${TROSHKA_ADMIN_FROM_CM:-}}"
    if [[ -z "${TROSHKA_FORWARDED_EMAIL}" ]]; then
      echo "warning: oauth enabled but no admin email — compute seed may 401" >&2
    fi
  fi
  # Clear ingress basic-auth so port-forward calls are unauthenticated at the edge.
  unset TROSHKA_BASIC_USER TROSHKA_BASIC_PASSWORD || true
  export PRODUCTION CLUSTER_NAME REGION
  if ! "${_script_dir}/../lib/seed-compute.sh"; then
    echo "warning: compute seed failed — UI is up; add provider/host manually (docs/install-aws.md)" >&2
  fi
  if ! NAMESPACE="${NAMESPACE}" "${_script_dir}/../lib/seed-fedora-image.sh"; then
    echo "warning: Fedora library image seed failed — upload manually from Admin → Library" >&2
  fi
  stop_backend_port_forward
  trap - EXIT
else
  echo "warning: could not port-forward backend — skipping compute seed" >&2
fi

cat <<EOF

Troshka is installed on EKS.

  Stack:   ${STACK_NAME}
  Cluster: ${CLUSTER_NAME}
  UI:      ${UI_URL}

EOF

if [[ "${USE_COGNITO}" == "true" ]]; then
  cat <<EOF
  Auth:    Cognito Hosted UI (oauth2-proxy)
  Admin:   ${ADMIN_EMAIL}
  Password:${ADMIN_PASSWORD}

  Sign in via the Cognito page; Troshka maps X-Forwarded-Email to your user.
  OpenShift oauth-proxy is NOT used on EKS - route.enabled stays false.
  Credentials file: ${CREDS_FILE}

EOF
else
  cat <<EOF
  Auth:    HTTP basic auth (nginx)
  User:    ${BASIC_USER}
  Password:${BASIC_PASS}

  Behind basic auth the app runs in oauth-off / auto-admin mode.
  Credentials file: ${CREDS_FILE}

EOF
fi

cat <<EOF
Permissions: docs/quickstarts/eks.md and deploy/eks/iam-deployer-policy.json
Compute: EC2 provider + one host seeded automatically
  Default:      Fedora Cloud AMI, m8i.xlarge, 100 GiB (in-cluster S4)
  --production: RHEL 9 Hourly AMI, m8i.2xlarge, 500 GiB (+ RDS/S3/ElastiCache)
  Override:     TROSHKA_HOST_AMI / TROSHKA_HOST_INSTANCE_TYPE / TROSHKA_HOST_DISK_GB
Teardown: ${_script_dir}/teardown.sh

EOF
