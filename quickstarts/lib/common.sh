#!/usr/bin/env bash
# Shared helpers for Troshka quickstart scripts.
set -euo pipefail

_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUICKSTARTS_ROOT="$(cd "${_lib_dir}/.." && pwd)"
REPO_ROOT="$(cd "${QUICKSTARTS_ROOT}/.." && pwd)"

export REPO_ROOT QUICKSTARTS_ROOT

TROSHKA_API_URL="${TROSHKA_API_URL:-http://localhost:8200}"
export TROSHKA_API_URL

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

has_yes_flag() {
  local arg
  for arg in "$@"; do
    if [[ "$arg" == "--yes" ]]; then
      return 0
    fi
  done
  return 1
}

confirm() {
  local prompt="$1"
  shift || true
  if has_yes_flag "$@"; then
    return 0
  fi
  if [[ "${TROSHKA_YES:-}" == "1" ]]; then
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
