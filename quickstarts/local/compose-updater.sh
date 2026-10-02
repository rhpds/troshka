#!/usr/bin/env bash
# Host-side helper for local Compose "Apply update".
# Writes running image digests and applies pull+up when the backend requests it.
# No container socket mount — runs on the host next to install.sh / teardown.sh.
set -euo pipefail

_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "${_script_dir}/../lib/common.sh"

COMPOSE_DIR="${COMPOSE_DIR:-${REPO_ROOT}/deploy/compose}"
ENV_FILE="${ENV_FILE:-${COMPOSE_DIR}/.env}"
UPDATER_DIR="${UPDATER_DIR:-${COMPOSE_DIR}/updater}"
PID_FILE="${UPDATER_DIR}/compose-updater.pid"
LOG_FILE="${UPDATER_DIR}/compose-updater.log"
RUNNING_DIGESTS="${UPDATER_DIR}/running-digests"
UPDATE_REQUEST="${UPDATER_DIR}/update-request"
UPDATE_STATUS="${UPDATER_DIR}/update-status"
POLL_INTERVAL="${TROSHKA_COMPOSE_UPDATER_POLL:-30}"

REGISTRY="${TROSHKA_REGISTRY:-quay.io}"
REPO="${TROSHKA_REPO:-redhat-gpte}"

detect_compose() {
  if command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
    echo "podman compose"
    return
  fi
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    echo "docker compose"
    return
  fi
  echo "error: need Podman or Docker with Compose v2" >&2
  exit 1
}

detect_runtime() {
  # First word of compose command (podman or docker) for image inspect.
  local cmd
  cmd="$(detect_compose)"
  echo "${cmd%% *}"
}

image_tag() {
  if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    set -a && source "${ENV_FILE}" && set +a
  fi
  echo "${TROSHKA_IMAGE_TAG:-latest}"
}

normalize_digest() {
  # RepoDigests look like registry/repo/name@sha256:abc — keep sha256:…
  local raw="$1"
  if [[ "${raw}" == *@sha256:* ]]; then
    echo "sha256:${raw##*@sha256:}"
  elif [[ "${raw}" == sha256:* ]]; then
    echo "${raw}"
  else
    echo "${raw}"
  fi
}

inspect_digest() {
  local runtime="$1" ref="$2"
  local out=""
  if [[ "${runtime}" == "podman" ]]; then
    out="$(podman image inspect "${ref}" --format '{{if .Digest}}{{.Digest}}{{else}}{{index .RepoDigests 0}}{{end}}' 2>/dev/null || true)"
  else
    out="$(docker image inspect "${ref}" --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}' 2>/dev/null || true)"
  fi
  if [[ -z "${out}" || "${out}" == "<no value>" ]]; then
    echo ""
    return
  fi
  normalize_digest "${out}"
}

write_running_digests() {
  local runtime tag be fe
  runtime="$(detect_runtime)"
  tag="$(image_tag)"
  be="$(inspect_digest "${runtime}" "${REGISTRY}/${REPO}/troshka-backend:${tag}")"
  fe="$(inspect_digest "${runtime}" "${REGISTRY}/${REPO}/troshka-frontend:${tag}")"
  mkdir -p "${UPDATER_DIR}"
  {
    echo "backend=${be}"
    echo "frontend=${fe}"
    echo "tag=${tag}"
    echo "updated_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"${RUNNING_DIGESTS}.tmp"
  mv "${RUNNING_DIGESTS}.tmp" "${RUNNING_DIGESTS}"
}

write_status() {
  mkdir -p "${UPDATER_DIR}"
  printf '%s\n' "$1" >"${UPDATE_STATUS}.tmp"
  mv "${UPDATE_STATUS}.tmp" "${UPDATE_STATUS}"
}

apply_update() {
  local compose_cmd
  compose_cmd="$(detect_compose)"
  write_status "rolling_out"
  # shellcheck disable=SC2086
  if ! ${compose_cmd} -f "${COMPOSE_DIR}/compose.yaml" --env-file "${ENV_FILE}" pull \
    >>"${LOG_FILE}" 2>&1; then
    write_status "error:compose pull failed — see ${LOG_FILE}"
    rm -f "${UPDATE_REQUEST}"
    return 1
  fi
  # shellcheck disable=SC2086
  if ! ${compose_cmd} -f "${COMPOSE_DIR}/compose.yaml" --env-file "${ENV_FILE}" up -d \
    >>"${LOG_FILE}" 2>&1; then
    write_status "error:compose up failed — see ${LOG_FILE}"
    rm -f "${UPDATE_REQUEST}"
    return 1
  fi
  write_running_digests
  write_status "idle"
  rm -f "${UPDATE_REQUEST}"
}

loop() {
  mkdir -p "${UPDATER_DIR}"
  write_status "idle"
  write_running_digests || true
  while true; do
    if [[ -f "${UPDATE_REQUEST}" ]]; then
      apply_update || true
    else
      write_running_digests || true
    fi
    sleep "${POLL_INTERVAL}"
  done
}

cmd_start() {
  mkdir -p "${UPDATER_DIR}"
  if [[ -f "${PID_FILE}" ]]; then
    local old
    old="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${old}" ]] && kill -0 "${old}" 2>/dev/null; then
      echo "compose-updater already running (pid ${old})"
      return 0
    fi
    rm -f "${PID_FILE}"
  fi
  # Re-exec as background daemon; keep stdout/stderr in the log.
  nohup bash "${BASH_SOURCE[0]}" run >>"${LOG_FILE}" 2>&1 &
  echo $! >"${PID_FILE}"
  echo "compose-updater started (pid $(cat "${PID_FILE}"))"
}

cmd_stop() {
  if [[ ! -f "${PID_FILE}" ]]; then
    echo "compose-updater not running"
    return 0
  fi
  local pid
  pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
    kill "${pid}" 2>/dev/null || true
    # Give pull/up a moment; then force.
    for _ in 1 2 3 4 5; do
      kill -0 "${pid}" 2>/dev/null || break
      sleep 1
    done
    kill -9 "${pid}" 2>/dev/null || true
  fi
  rm -f "${PID_FILE}"
  write_status "idle"
  echo "compose-updater stopped"
}

case "${1:-}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  run) loop ;;
  digests) write_running_digests ;;
  *)
    echo "usage: $0 {start|stop|run|digests}" >&2
    exit 2
    ;;
esac
