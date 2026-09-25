#!/usr/bin/env bash
# Shared helpers for Troshka quickstart scripts.
set -euo pipefail

_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUICKSTARTS_ROOT="$(cd "${_lib_dir}/.." && pwd)"
REPO_ROOT="$(cd "${QUICKSTARTS_ROOT}/.." && pwd)"

export REPO_ROOT QUICKSTARTS_ROOT

TROSHKA_API_URL="${TROSHKA_API_URL:-http://localhost:8200}"
export TROSHKA_API_URL

require_cmd() {
  local c
  for c in "$@"; do
    if ! command -v "$c" >/dev/null 2>&1; then
      echo "error: required command not found: $c" >&2
      exit 1
    fi
  done
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
