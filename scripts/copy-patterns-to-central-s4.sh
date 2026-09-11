#!/usr/bin/env bash
#
# Copy Troshka pattern disks from prod S4 to central gold S4 (in-cluster).
#
# All data movement stays on the cluster — nothing through the operator laptop.
# Uses rclone copyto per object with settings tuned for same-node S4→S4 on
# ocpv-infra01 (~250–300 MiB/s when the job is scheduled on the S4 node and
# central S4 has adequate CPU).
#
# Prerequisites:
#   - oc logged in / kubeconfig for ocpv-infra01
#   - Prod Troshka S4 in namespace troshka (troshka-s4.troshka.svc:7480)
#   - Central gold S4 in troshka-images (s4.troshka-images.svc:7480)
#
# Usage:
#   ./scripts/copy-patterns-to-central-s4.sh status
#   ./scripts/copy-patterns-to-central-s4.sh launch <pattern-uuid> [more-uuids...]
#   ./scripts/copy-patterns-to-central-s4.sh logs [job-name]
#   ./scripts/copy-patterns-to-central-s4.sh wait [job-name]
#   ./scripts/copy-patterns-to-central-s4.sh cancel [job-name]
#   ./scripts/copy-patterns-to-central-s4.sh verify <pattern-uuid> [more-uuids...]
#
# Environment overrides:
#   KC_INFRA          kubeconfig (default: ~/secrets/ocpv-infra01...kubeconfig)
#   NS_PROD           prod namespace (default: troshka)
#   NS_CENTRAL        central namespace (default: troshka-images)
#   BOOST_S4_CPU      patch central S4 to 2 CPU before copy (default: 1)
#   S4_NODE           force job node (default: node running central S4 pod)
#   JOB_NAME          rclone job name (default: copy-patterns-<short-id>)
#   SKIP_EXISTING     skip objects already on central (default: 1)
#
set -euo pipefail

KC_INFRA="${KC_INFRA:-$HOME/secrets/ocpv-infra01.dal12.infra.demo.redhat.com.kubeconfig}"
NS_PROD="${NS_PROD:-troshka}"
NS_CENTRAL="${NS_CENTRAL:-troshka-images}"
SRC_BUCKET="${SRC_BUCKET:-troshka-images}"
DST_BUCKET="${DST_BUCKET:-troshka-gold-images}"
SRC_ENDPOINT="${SRC_ENDPOINT:-http://troshka-s4.troshka.svc:7480}"
DST_ENDPOINT="${DST_ENDPOINT:-http://s4:7480}"
S4_ROUTE="${S4_ROUTE:-https://s4-troshka-images.apps.ocpv-infra01.dal12.infra.demo.redhat.com}"

SECRET_CREDS="${SECRET_CREDS:-copy-patterns-rclone-creds}"
LABEL_APP="${LABEL_APP:-copy-patterns-central-s4}"

# Only needed if central S4 is still on legacy 500m limits (see infra/central-s4/deploy.yaml).
BOOST_S4_CPU="${BOOST_S4_CPU:-0}"
SKIP_EXISTING="${SKIP_EXISTING:-1}"
S4_CPU_BOOST="${S4_CPU_BOOST:-4}"
S4_CPU_RESTORE="${S4_CPU_RESTORE:-2}"

# Validated on ocpv-infra01 S4→S4 (co-located job + central S4 CPU ≥ 2).
RCLONE_FLAGS=(
  --s3-chunk-size 128M
  --s3-upload-concurrency 8
  --s3-disable-checksum
  --multi-thread-streams 4
  --multi-thread-chunk-size 128M
  --multi-thread-cutoff 256M
  --buffer-size 128M
  --log-level INFO
  --stats 15s
  --stats-one-line
)

oc_infra() { oc --kubeconfig="$KC_INFRA" "$@"; }

log() { echo "[copy-patterns] $*"; }
die() { log "ERROR: $*"; exit 1; }

require_kc() {
  oc_infra get ns "$NS_CENTRAL" >/dev/null || die "cannot reach $NS_CENTRAL (KC_INFRA=$KC_INFRA)"
  oc_infra get ns "$NS_PROD" >/dev/null || die "cannot reach $NS_PROD"
}

detect_s4_node() {
  if [[ -n "${S4_NODE:-}" ]]; then
    echo "$S4_NODE"
    return
  fi
  local node
  node=$(oc_infra get pod -n "$NS_CENTRAL" -l app.kubernetes.io/name=s4 \
    -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null || true)
  if [[ -z "$node" ]]; then
    node=$(oc_infra get pod -n "$NS_CENTRAL" -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.nodeName}{"\n"}{end}' \
      | awk '/^s4-/{print $2; exit}')
  fi
  [[ -n "$node" ]] || die "could not detect central S4 node — set S4_NODE"
  echo "$node"
}

ensure_rclone_secret() {
  local src_key src_secret dst_key dst_secret
  src_key=$(oc_infra get secret troshka-secrets -n "$NS_PROD" \
    -o jsonpath='{.data.s3-access-key}' | base64 -d)
  src_secret=$(oc_infra get secret troshka-secrets -n "$NS_PROD" \
    -o jsonpath='{.data.s3-secret-key}' | base64 -d)
  dst_key=$(oc_infra get secret s4-credentials -n "$NS_CENTRAL" \
    -o jsonpath='{.data.access-key}' | base64 -d)
  dst_secret=$(oc_infra get secret s4-credentials -n "$NS_CENTRAL" \
    -o jsonpath='{.data.secret-key}' | base64 -d)

  oc_infra create secret generic "$SECRET_CREDS" -n "$NS_CENTRAL" \
    --from-literal=SRC_ACCESS_KEY_ID="$src_key" \
    --from-literal=SRC_SECRET_ACCESS_KEY="$src_secret" \
    --from-literal=DST_ACCESS_KEY_ID="$dst_key" \
    --from-literal=DST_SECRET_ACCESS_KEY="$dst_secret" \
    --dry-run=client -o yaml | oc_infra apply -f -
  log "Ensured secret $SECRET_CREDS in $NS_CENTRAL"
}

boost_s4_cpu() {
  [[ "$BOOST_S4_CPU" == "1" ]] || return 0
  log "Patching central S4 CPU to ${S4_CPU_BOOST} (restore with: BOOST_S4_CPU=0 $0 launch ...)"
  oc_infra patch deploy s4 -n "$NS_CENTRAL" --type=json -p="[
    {\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/resources/limits/cpu\",\"value\":\"${S4_CPU_BOOST}\"},
    {\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/resources/requests/cpu\",\"value\":\"1\"}
  ]"
  oc_infra rollout status deploy/s4 -n "$NS_CENTRAL" --timeout=180s
}

default_job_name() {
  local first="${1:-manual}"
  local short
  short=$(echo "$first" | tr -cd '[:alnum:]' | cut -c1-8)
  echo "copy-patterns-${short}"
}

latest_job_name() {
  oc_infra get job -n "$NS_CENTRAL" -l app="$LABEL_APP" \
    --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | tail -1
}

resolve_job_name() {
  local arg="${1:-}"
  if [[ -n "$arg" ]]; then
    echo "$arg"
  else
    latest_job_name
  fi
}

cmd_status() {
  require_kc
  local node
  node=$(detect_s4_node)
  log "kubeconfig: $KC_INFRA"
  log "central S4 node: $node"
  log "src:  $SRC_ENDPOINT / $SRC_BUCKET"
  log "dst:  $DST_ENDPOINT / $DST_BUCKET"
  echo
  oc_infra get deploy s4 -n "$NS_CENTRAL" \
    -o custom-columns=NAME:.metadata.name,READY:.status.readyReplicas,CPU:.spec.template.spec.containers[0].resources.limits.cpu 2>/dev/null \
    || true
  echo
  oc_infra get job -n "$NS_CENTRAL" -l app="$LABEL_APP" 2>/dev/null || log "no copy jobs found"
  local job
  job=$(latest_job_name || true)
  if [[ -n "$job" ]]; then
    echo
    log "Latest job: $job"
    oc_infra logs -n "$NS_CENTRAL" "job/$job" 2>/dev/null | grep -E 'INFO.*GiB|copyto|DONE|ERROR' | tail -5 || true
  fi
}

build_rclone_flags_shell() {
  local out=""
  for f in "${RCLONE_FLAGS[@]}"; do
    out+="$f "
  done
  echo "$out"
}

cmd_launch() {
  shift
  [[ $# -ge 1 ]] || die "usage: $0 launch <pattern-uuid> [more-uuids...]"

  require_kc
  ensure_rclone_secret
  boost_s4_cpu

  local node job_name pattern_ids pattern_shell skip_shell rclone_flags
  node=$(detect_s4_node)
  job_name="${JOB_NAME:-$(default_job_name "$1")}"

  if oc_infra get job "$job_name" -n "$NS_CENTRAL" >/dev/null 2>&1; then
    die "job $job_name already exists — cancel first or set JOB_NAME"
  fi

  pattern_ids="$*"
  pattern_shell=$(printf '%q ' "$@")
  rclone_flags=$(build_rclone_flags_shell)
  skip_shell=$([[ "$SKIP_EXISTING" == "1" ]] && echo "1" || echo "0")

  cat <<EOF | oc_infra apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job_name}
  namespace: ${NS_CENTRAL}
  labels:
    app: ${LABEL_APP}
spec:
  ttlSecondsAfterFinished: 3600
  activeDeadlineSeconds: 14400
  backoffLimit: 1
  template:
    metadata:
      labels:
        app: ${LABEL_APP}
    spec:
      nodeName: ${node}
      restartPolicy: Never
      containers:
      - name: rclone
        image: rclone/rclone:latest
        resources:
          requests:
            cpu: "2"
            memory: 4Gi
          limits:
            cpu: "4"
            memory: 12Gi
        env:
        - name: SRC_ACCESS_KEY_ID
          valueFrom:
            secretKeyRef:
              name: ${SECRET_CREDS}
              key: SRC_ACCESS_KEY_ID
        - name: SRC_SECRET_ACCESS_KEY
          valueFrom:
            secretKeyRef:
              name: ${SECRET_CREDS}
              key: SRC_SECRET_ACCESS_KEY
        - name: DST_ACCESS_KEY_ID
          valueFrom:
            secretKeyRef:
              name: ${SECRET_CREDS}
              key: DST_ACCESS_KEY_ID
        - name: DST_SECRET_ACCESS_KEY
          valueFrom:
            secretKeyRef:
              name: ${SECRET_CREDS}
              key: DST_SECRET_ACCESS_KEY
        command: ["/bin/sh", "-ec"]
        args:
        - |
          set -euo pipefail
          mkdir -p /tmp
          printf '%s\n' \
            '[src]' \
            'type = s3' \
            'provider = Ceph' \
            "access_key_id = \${SRC_ACCESS_KEY_ID}" \
            "secret_access_key = \${SRC_SECRET_ACCESS_KEY}" \
            'endpoint = ${SRC_ENDPOINT}' \
            'no_check_bucket = true' \
            'no_verify_ssl = true' \
            '[dst]' \
            'type = s3' \
            'provider = Ceph' \
            "access_key_id = \${DST_ACCESS_KEY_ID}" \
            "secret_access_key = \${DST_SECRET_ACCESS_KEY}" \
            'endpoint = ${DST_ENDPOINT}' \
            'no_check_bucket = true' \
            'no_verify_ssl = true' \
            > /tmp/rclone.conf

          SKIP_EXISTING=${skip_shell}
          PATTERN_IDS="${pattern_shell}"
          FLAGS="${rclone_flags}"

          object_exists() {
            local key="\$1"
            rclone --config /tmp/rclone.conf lsf "dst:${DST_BUCKET}/\${key}" >/dev/null 2>&1
          }

          for pid in \$PATTERN_IDS; do
            echo "=== Pattern \${pid} ==="
            objects=\$(rclone --config /tmp/rclone.conf lsf \
              "src:${SRC_BUCKET}/patterns/\${pid}/" --files-only | sort)
            if [ -z "\$objects" ]; then
              echo "WARN: no objects under patterns/\${pid}/ on prod S4"
              continue
            fi
            printf '%s\n' "\$objects" | while IFS= read -r obj; do
              [ -n "\$obj" ] || continue
              key="patterns/\${pid}/\${obj}"
              if [ "\$SKIP_EXISTING" = "1" ] && object_exists "\$key"; then
                echo "SKIP existing \${key}"
                continue
              fi
              echo "=== copyto \${key} ==="
              # shellcheck disable=SC2086
              rclone --config /tmp/rclone.conf copyto \
                "src:${SRC_BUCKET}/\${key}" "dst:${DST_BUCKET}/\${key}" \
                \$FLAGS
            done
          done
          echo "All patterns copied"
        volumeMounts: []
EOF

  log "Launched job $job_name on node $node"
  log "  patterns: $pattern_ids"
  log "  monitor:  $0 logs $job_name"
  log "  wait:     $0 wait $job_name"
}

cmd_logs() {
  require_kc
  local job
  job=$(resolve_job_name "${1:-}")
  [[ -n "$job" ]] || die "no job found"
  oc_infra logs -n "$NS_CENTRAL" "job/$job" "${@:2}"
}

cmd_wait() {
  require_kc
  local job
  job=$(resolve_job_name "${1:-}")
  [[ -n "$job" ]] || die "no job found"
  log "Waiting for job $job ..."
  oc_infra wait -n "$NS_CENTRAL" --for=condition=complete "job/$job" --timeout=4h
  log "Job $job completed"
  cmd_logs "$job" | tail -20
}

cmd_cancel() {
  require_kc
  local job
  job=$(resolve_job_name "${1:-}")
  [[ -n "$job" ]] || die "no job found"
  oc_infra delete job "$job" -n "$NS_CENTRAL" --wait=true
  log "Deleted job $job"
}

cmd_verify() {
  shift
  [[ $# -ge 1 ]] || die "usage: $0 verify <pattern-uuid> [more-uuids...]"

  require_kc
  local dst_key dst_secret
  dst_key=$(oc_infra get secret s4-credentials -n "$NS_CENTRAL" \
    -o jsonpath='{.data.access-key}' | base64 -d)
  dst_secret=$(oc_infra get secret s4-credentials -n "$NS_CENTRAL" \
    -o jsonpath='{.data.secret-key}' | base64 -d)

  if ! command -v aws >/dev/null 2>&1; then
    die "aws CLI required for verify (or use: $0 logs <job>)"
  fi

  local pid ok=0
  for pid in "$@"; do
    echo "=== central S4: patterns/$pid ==="
    if AWS_ACCESS_KEY_ID="$dst_key" AWS_SECRET_ACCESS_KEY="$dst_secret" \
      aws s3 ls "s3://${DST_BUCKET}/patterns/${pid}/" \
        --endpoint-url "$S4_ROUTE" --region us-east-1 --human-readable --summarize; then
      ok=1
    else
      log "MISSING or empty: patterns/$pid"
    fi
    echo
  done
  [[ "$ok" == "1" ]] || die "verification failed for all patterns"
}

usage() {
  sed -n '2,22p' "$0" | tr -d '#'
  exit 1
}

main() {
  local cmd="${1:-}"
  case "$cmd" in
    status)  cmd_status ;;
    launch)  cmd_launch "$@" ;;
    logs)    shift; cmd_logs "$@" ;;
    wait)    shift; cmd_wait "$@" ;;
    cancel)  shift; cmd_cancel "$@" ;;
    verify)  cmd_verify "$@" ;;
    -h|--help|help|"") usage ;;
    *) die "unknown command: $cmd (try --help)" ;;
  esac
}

main "$@"
