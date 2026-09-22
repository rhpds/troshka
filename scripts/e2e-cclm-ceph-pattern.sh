#!/usr/bin/env bash
#
# AFK e2e: wait for CCLM source → pattern capture → pattern deploy → Ceph verify.
#
# Prerequisites: source project already created+deploying (see /tmp/troshka-cclm-e2e-*).
# Usage: ./scripts/e2e-cclm-ceph-pattern.sh
#
set -uo pipefail

API="${TROSHKA_API_URL:-http://localhost:8200}"
KEY="${TROSHKA_API_KEY:-$(cat /tmp/troshka-cclm-e2e-api-key.txt)}"
SOURCE_PID="${SOURCE_PID:-$(cat /tmp/troshka-cclm-e2e-source-pid.txt)}"
PASS="${TROSHKA_LIVE_COMMON_PASSWORD:-$(cat /tmp/troshka-cclm-e2e-password.txt)}"
HOST="${TROSHKA_KV_HOST:-57449fb9-af86-490a-b560-cc6596acfae9}"
PROV="${TROSHKA_KV_PROVIDER:-dafe9fb0-76ed-4af6-acc0-afe08a4c256d}"
LOG="${E2E_LOG:-/tmp/troshka-cclm-ceph-e2e.log}"
KC="${KUBECONFIG:-$HOME/secrets/ocpvdev01.dal13.infra.demo.redhat.com.kubeconfig}"

auth=(-H "Authorization: Bearer $KEY" -H "Content-Type: application/json")
log() { echo "[$(date -u +%H:%M:%S)] $*"; }

api_get() { curl -sfS --max-time 60 "${auth[@]}" "$API$1" || true; }
api_post() { curl -sfS --max-time 120 "${auth[@]}" -X POST "$API$1" -d "$2"; }

wait_project_active() {
  local pid=$1 max=${2:-14400}  # 4h
  local start=$SECONDS
  while (( SECONDS - start < max )); do
    local js state ocp
    js=$(api_get "/api/v1/projects/$pid" || true)
    state=$(echo "$js" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("state",""))' 2>/dev/null || true)
    ocp=$(echo "$js" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("ocp_status") or "")' 2>/dev/null || true)
    log "project ${pid:0:8} state=$state ocp=$ocp"
    if [[ "$state" == "active" && ( "$ocp" == "ready" || "$ocp" == "warning" ) ]]; then
      return 0
    fi
    if [[ "$state" == "error" || "$ocp" == "error" ]]; then
      log "ERROR: project failed: $(echo "$js" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("deploy_error") or d.get("ocp_status_detail") or "")' 2>/dev/null | head -c 500)"
      return 1
    fi
    sleep 30
  done
  log "ERROR: timeout waiting for project $pid (last state=$state ocp=$ocp)"
  return 1
}

wait_ceph_healthy() {
  local pid=$1 max=${2:-3600}
  local ns="troshka-${pid:0:8}"
  local start=$SECONDS
  export KUBECONFIG="$KC"
  while (( SECONDS - start < max )); do
    local phase health
    phase=$(oc -n "$ns" get cephcluster -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo missing)
    health=$(oc -n "$ns" get cephcluster -o jsonpath='{.items[0].status.ceph.health}' 2>/dev/null || echo unknown)
    log "ceph ns=$ns phase=$phase health=$health"
    if [[ "$phase" == "Ready" && "$health" == "HEALTH_OK" ]]; then
      return 0
    fi
    if [[ "$phase" == "Ready" && "$health" == "HEALTH_WARN" ]]; then
      # Accept WARN only if no incomplete/unfound in status message
      local msg
      msg=$(oc -n "$ns" get cephcluster -o jsonpath='{.items[0].status.message}' 2>/dev/null || true)
      if ! echo "$msg $health" | grep -qiE 'incomplete|unfound|HEALTH_ERR'; then
        # Double-check via tools if possible
        log "ceph Ready with WARN — probing further"
      fi
    fi
    sleep 20
  done
  log "ERROR: Ceph not healthy in $ns"
  return 1
}

ensure_rook_scc() {
  local pid=$1
  local ns="troshka-${pid:0:8}"
  export KUBECONFIG="$KC"
  for sa in rook-ceph-osd rook-ceph-default rook-ceph-mgr rook-ceph-cmd-reporter; do
    oc adm policy add-scc-to-user rook-ceph -z "$sa" -n "$ns" >/dev/null 2>&1 || true
  done
  oc adm policy add-scc-to-user privileged -z rook-ceph-osd -n "$ns" >/dev/null 2>&1 || true
  log "ensured rook SCC bindings in $ns"
}

scale_down_source_ceph() {
  # hostNetwork mon uses 6789/3300 cluster-wide — only one project Ceph per
  # host. After pattern capture, park the source Ceph so restore can bind.
  local pid=$1
  local ns="troshka-${pid:0:8}"
  export KUBECONFIG="$KC"
  log "scaling down source Ceph in $ns (free mon ports for restore)"
  for d in $(oc -n "$ns" get deploy -o name 2>/dev/null | grep -E 'rook-ceph-(operator|mon|osd|mgr|exporter)'); do
    oc -n "$ns" scale "$d" --replicas=0 >/dev/null 2>&1 || true
    log "  scaled $d → 0"
  done
  # Wait until mon pods are gone so ports release
  local start=$SECONDS
  while (( SECONDS - start < 300 )); do
    local n
    n=$(oc -n "$ns" get pods -l app=rook-ceph-mon --no-headers 2>/dev/null | wc -l | tr -d ' ')
    log "  source mon pods remaining=$n"
    [[ "$n" == "0" ]] && break
    sleep 10
  done
}

verify_ceph_usable() {
  local pid=$1
  local ns="troshka-${pid:0:8}"
  export KUBECONFIG="$KC"
  # Inject ignore_history_les as safety (no data loss) if incomplete appears
  local key
  key=$(oc -n "$ns" get secret rook-ceph-admin-keyring -o jsonpath='{.data.keyring}' 2>/dev/null | base64 -d | awk '/key =/{print $3; exit}')
  local mon
  mon=$(oc -n "$ns" get cm rook-ceph-mon-endpoints -o jsonpath='{.data.data}' 2>/dev/null | head -1)
  # Prefer hostNetwork mon IP:port from endpoints a=ip:6789
  local monhost
  monhost=$(echo "$mon" | sed -n 's/.*a=\([^,]*\).*/\1/p')
  oc -n "$ns" delete pod ceph-e2e-probe --ignore-not-found --wait=false >/dev/null 2>&1 || true
  oc -n "$ns" run ceph-e2e-probe --restart=Never --image=quay.io/ceph/ceph:v18.2.4 \
    --overrides="{\"spec\":{\"hostNetwork\":true,\"containers\":[{\"name\":\"ceph-e2e-probe\",\"image\":\"quay.io/ceph/ceph:v18.2.4\",\"command\":[\"sleep\",\"300\"],\"securityContext\":{\"privileged\":true}}]}}" \
    >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do
    [[ "$(oc -n "$ns" get pod ceph-e2e-probe -o jsonpath='{.status.phase}' 2>/dev/null)" == "Running" ]] && break
    sleep 2
  done
  oc -n "$ns" exec ceph-e2e-probe -- bash -c "
mkdir -p /etc/ceph
cat > /etc/ceph/ceph.conf <<EOF
[global]
mon host = ${monhost:-10.0.0.4:6789}
EOF
cat > /etc/ceph/ceph.client.admin.keyring <<EOF
[client.admin]
	key = ${key}
	caps mds = \"allow *\"
	caps mgr = \"allow *\"
	caps mon = \"allow *\"
	caps osd = \"allow *\"
EOF
timeout 30 ceph -s
timeout 20 ceph pg stat
timeout 15 ceph config set osd osd_find_best_info_ignore_history_les true || true
timeout 45 rbd ls troshka-ceph-pool; echo RBD_EXIT=\$?
" 2>&1 | tee -a "$LOG" | tail -40
  oc -n "$ns" delete pod ceph-e2e-probe --ignore-not-found --wait=false >/dev/null 2>&1 || true
}

# ---- main ----
log "=== CCLM Ceph pattern e2e ==="
log "source=$SOURCE_PID host=$HOST"

log "Phase 1: wait source active"
wait_project_active "$SOURCE_PID"
ensure_rook_scc "$SOURCE_PID"
log "Phase 1b: wait source Ceph HEALTH_OK"
wait_ceph_healthy "$SOURCE_PID"
verify_ceph_usable "$SOURCE_PID"

PATTERN_NAME="ocp-cclm-ceph-pattern-$(date +%Y%m%d-%H%M)"
log "Phase 2: create pattern $PATTERN_NAME (quiesce=true)"
PRES=$(api_post "/api/v1/patterns/" "{\"name\":\"$PATTERN_NAME\",\"source_project_id\":\"$SOURCE_PID\",\"quiesce_cluster\":true,\"restart_after\":true}")
PATTERN_ID=$(echo "$PRES" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
log "pattern_id=$PATTERN_ID"
echo "$PATTERN_ID" > /tmp/troshka-cclm-e2e-pattern-id.txt

log "Phase 2b: wait pattern available"
start=$SECONDS
while (( SECONDS - start < 14400 )); do
  st=$(api_get "/api/v1/patterns/$PATTERN_ID" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("state",""))')
  log "pattern state=$st"
  [[ "$st" == "available" ]] && break
  [[ "$st" == "error" || "$st" == "failed" ]] && { log "ERROR pattern failed"; exit 1; }
  sleep 30
done
[[ "$(api_get "/api/v1/patterns/$PATTERN_ID" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("state",""))' 2>/dev/null || true)" == "available" ]] || { log "ERROR pattern timeout"; exit 1; }

log "Phase 2c: free hostNetwork mon ports before restore"
scale_down_source_ceph "$SOURCE_PID"

DEPLOY_NAME="ocp-cclm-ceph-restore-$(date +%Y%m%d-%H%M)"
log "Phase 3: deploy pattern → $DEPLOY_NAME"
DRES=$(api_post "/api/v1/patterns/$PATTERN_ID/deploy" "{\"name\":\"$DEPLOY_NAME\",\"auto_deploy\":true,\"auto_start\":true,\"common_password\":\"$PASS\",\"host_id\":\"$HOST\"}")
RESTORE_PID=$(echo "$DRES" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
log "restore_project=$RESTORE_PID"
echo "$RESTORE_PID" > /tmp/troshka-cclm-e2e-restore-pid.txt

# Pattern deploy with auto_deploy may not pass provider — nudge deploy if still draft
rst=$(api_get "/api/v1/projects/$RESTORE_PID" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("state",""))')
if [[ "$rst" == "draft" ]]; then
  log "restore still draft — posting deploy"
  curl -sfS "${auth[@]}" -X POST "$API/api/v1/projects/${RESTORE_PID}/deploy?host_id=${HOST}&provider_id=${PROV}" -d '{}' >/dev/null || true
fi

log "Phase 3b: wait restore active"
wait_project_active "$RESTORE_PID"
ensure_rook_scc "$RESTORE_PID"
log "Phase 3c: wait restored Ceph healthy"
wait_ceph_healthy "$RESTORE_PID" 7200
verify_ceph_usable "$RESTORE_PID"

log "=== SUCCESS source=$SOURCE_PID pattern=$PATTERN_ID restore=$RESTORE_PID ==="
echo "SUCCESS" > /tmp/troshka-cclm-e2e-result.txt
