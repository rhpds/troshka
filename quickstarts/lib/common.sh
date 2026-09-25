#!/usr/bin/env bash
# Shared helpers for Troshka quickstart scripts.
set -euo pipefail

_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUICKSTARTS_ROOT="$(cd "${_lib_dir}/.." && pwd)"
REPO_ROOT="$(cd "${QUICKSTARTS_ROOT}/.." && pwd)"

export REPO_ROOT QUICKSTARTS_ROOT

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

# Shared tip block for EKS install/teardown abort / help.
print_eks_tips() {
  local default_kc
  default_kc="$(troshka_eks_kubeconfig_path)"
  cat <<EOF
Tips:
  export AWS_PROFILE=my-profile          # alternate AK/SK via named profile
  export AWS_REGION=us-west-2            # (or your preferred region) and re-run
  export AWS_DEFAULT_REGION=us-west-2    # fallback if AWS_REGION unset
  export KUBECONFIG=${default_kc}
  export TROSHKA_EKS_STACK=my-stack      # CloudFormation stack name
  export TROSHKA_EKS_CLUSTER=my-cluster  # EKS cluster name
  ./quickstarts/eks/install.sh --yes     # skip confirms (also --quiet / --no-verify)
EOF
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

api_get() {
  local path="$1"
  curl -fsS "${TROSHKA_API_URL}${path}"
}

api_delete() {
  local path="$1"
  curl -fsS -X DELETE "${TROSHKA_API_URL}${path}"
}
