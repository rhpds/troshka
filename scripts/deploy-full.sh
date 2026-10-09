#!/usr/bin/env bash
set -euo pipefail

# Full deploy cycle: push → wait CI → promote → restart operators → wait ArgoCD
# Usage: scripts/deploy-full.sh [--skip-push] [--skip-operators] [--skip-project-pods]
#                               [--sync-ns-rbac]
#
# --sync-ns-rbac  Force per-namespace RoleBinding sync even when ClusterRoles
#                 are unchanged (default: skip that churn — the slow path).

SKIP_PUSH=false
SKIP_OPERATORS=false
SKIP_PROJECT_PODS=false
FORCE_NS_RBAC=false
for arg in "$@"; do
  case "$arg" in
    --skip-push) SKIP_PUSH=true ;;
    --skip-operators) SKIP_OPERATORS=true ;;
    --skip-project-pods) SKIP_PROJECT_PODS=true ;;
    --sync-ns-rbac) FORCE_NS_RBAC=true ;;
  esac
done

KC="$HOME/secrets/ocpv-infra01.dal12.infra.demo.redhat.com.kubeconfig"

_cluster_name() {
  basename "$1" .kubeconfig | cut -d. -f1
}

_clusterrole_rv() {
  local kc=$1 name=$2
  oc get clusterrole "$name" --kubeconfig="$kc" \
    -o jsonpath='{.metadata.resourceVersion}' 2>/dev/null || echo "missing"
}

_sync_ns_rolebindings() {
  local kc=$1
  while IFS= read -r ns; do
    [ -z "$ns" ] && continue
    oc create rolebinding troshka-provider-namespaced \
      --clusterrole=troshka-provider-namespaced \
      --serviceaccount=troshka:troshka \
      -n "$ns" --kubeconfig="$kc" >/dev/null 2>&1 || true
    oc create rolebinding troshka-operator-namespaced \
      --clusterrole=troshka-operator-namespaced \
      --serviceaccount=troshka-operator:troshka-operator \
      -n "$ns" --kubeconfig="$kc" >/dev/null 2>&1 || true
  done < <(oc get ns -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' \
    --kubeconfig="$kc" 2>/dev/null | grep -E '^troshka' || true)
}

# Apply CRDs + RBAC on one cluster; write a one-line status to $out.
_apply_cluster_rbac() {
  local kc=$1 provider_rbac=$2 out=$3 force_ns=$4
  local cluster crd_ok=false operator_rbac_ok=false provider_rbac_ok=false
  local before_op before_prov after_op after_prov ns_note=""
  cluster=$(_cluster_name "$kc")

  before_op=$(_clusterrole_rv "$kc" troshka-operator-namespaced)
  before_prov=$(_clusterrole_rv "$kc" troshka-provider-namespaced)

  oc apply -f src/operator/crds/ --kubeconfig="$kc" >/dev/null 2>&1 && crd_ok=true
  oc apply -f src/operator/deploy/clusterrole.yaml --kubeconfig="$kc" >/dev/null 2>&1 \
    && operator_rbac_ok=true
  oc apply -f "$provider_rbac" --kubeconfig="$kc" >/dev/null 2>&1 && provider_rbac_ok=true

  after_op=$(_clusterrole_rv "$kc" troshka-operator-namespaced)
  after_prov=$(_clusterrole_rv "$kc" troshka-provider-namespaced)

  if [ "$provider_rbac_ok" = true ] && [ "$operator_rbac_ok" = true ]; then
    if [ "$force_ns" = true ] \
      || [ "$before_op" != "$after_op" ] \
      || [ "$before_prov" != "$after_prov" ]; then
      _sync_ns_rolebindings "$kc"
      ns_note="; ns RoleBindings synced"
    else
      ns_note="; ns RoleBindings skipped (ClusterRoles unchanged)"
    fi
  fi

  if [ "$crd_ok" = true ] && [ "$operator_rbac_ok" = true ] && [ "$provider_rbac_ok" = true ]; then
    echo "CRDs + operator/provider RBAC applied${ns_note}" >"$out"
  else
    echo "FAILED (crds=$crd_ok operator_rbac=$operator_rbac_ok provider_rbac=$provider_rbac_ok)" >"$out"
  fi
}

_restart_operator_if_stale() {
  local kc=$1 expected=$2 out=$3
  local cluster running
  cluster=$(_cluster_name "$kc")
  running=$(oc get deploy troshka-operator -n troshka-operator --kubeconfig="$kc" \
    -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo "unknown")

  if [ -n "$expected" ] && echo "$running" | grep -qF "$expected"; then
    echo "current" >"$out"
    return 0
  fi
  if oc rollout restart deployment/troshka-operator -n troshka-operator \
    --kubeconfig="$kc" >/dev/null 2>&1; then
    echo "restarted" >"$out"
  else
    echo "FAILED" >"$out"
  fi
}

_verify_operator_digest() {
  local kc=$1 expected=$2 out=$3
  local cluster attempt new_image
  cluster=$(_cluster_name "$kc")
  for attempt in 1 2 3 4 5 6; do
    new_image=$(oc get pods -n troshka-operator --kubeconfig="$kc" \
      -l app=troshka-operator \
      -o jsonpath='{.items[0].status.containerStatuses[0].imageID}' 2>/dev/null || echo "")
    if echo "$new_image" | grep -qF "$expected"; then
      echo "verified" >"$out"
      return 0
    fi
    sleep 5
  done
  echo "NOT verified (may still be rolling out)" >"$out"
}

_restart_stale_project_pods() {
  local kc=$1 out=$2
  shift 2
  # remaining: role_name digest pairs as name|digest
  local cluster restarted=0 role expected pod_image deploys
  cluster=$(_cluster_name "$kc")

  for pair in "$@"; do
    role="${pair%%|*}"
    expected="${pair#*|}"
    [ -z "$expected" ] && continue

    pod_image=$(oc get pods --all-namespaces -l "troshka-role=$role" \
      -o jsonpath='{.items[0].status.containerStatuses[0].imageID}' \
      --kubeconfig="$kc" 2>/dev/null || echo "")
    if [ -z "$pod_image" ]; then
      pod_image=$(oc get pods --all-namespaces -l "app=troshka-$role" \
        -o jsonpath='{.items[0].status.containerStatuses[0].imageID}' \
        --kubeconfig="$kc" 2>/dev/null || echo "")
    fi

    if [ -z "$pod_image" ] || echo "$pod_image" | grep -qF "$expected"; then
      continue
    fi

    deploys=$(oc get deploy --all-namespaces -l "troshka-role=$role" \
      -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name \
      --no-headers --kubeconfig="$kc" 2>/dev/null || echo "")
    if [ -z "$(echo "$deploys" | tr -d '[:space:]')" ]; then
      deploys=$(oc get deploy --all-namespaces -l "app=troshka-$role" \
        -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name \
        --no-headers --kubeconfig="$kc" 2>/dev/null || echo "")
    fi
    while read -r ns name; do
      [ -z "$ns" ] && continue
      oc rollout restart "deploy/$name" -n "$ns" --kubeconfig="$kc" 2>/dev/null || true
      restarted=$((restarted + 1))
    done <<< "$deploys"
  done

  if [ "$restarted" -eq 0 ]; then
    echo "all current" >"$out"
  else
    echo "restarted $restarted deployments" >"$out"
  fi
}

_wait_jobs() {
  local pid
  for pid in "$@"; do
    wait "$pid" || true
  done
}

_print_job_results() {
  local dir=$1
  local f cluster
  # Stable order matching OPERATOR_KUBECONFIGS discovery
  for kc in "${OPERATOR_KUBECONFIGS[@]}"; do
    cluster=$(_cluster_name "$kc")
    f="$dir/$cluster"
    if [ -f "$f" ]; then
      printf "  %s: %s\n" "$cluster" "$(cat "$f")"
    fi
  done
}

echo "=== Step 1: Push to main ==="
if [ "$SKIP_PUSH" = true ]; then
  echo "  Skipped (--skip-push)"
else
  git push origin main
fi

echo ""
echo "=== Step 2: Wait for CI ==="
HEAD_SHA=$(git rev-parse HEAD)
for _attempt in $(seq 1 20); do
  IMAGE_RUN=$(gh run list --commit "$HEAD_SHA" --limit 1 --json databaseId --jq '.[0].databaseId' -w "Build and Push Container Images")
  [ -n "$IMAGE_RUN" ] && break
  echo "  Waiting for CI run to appear... (${_attempt}/20)"
  sleep 5
done
if [ -z "$IMAGE_RUN" ]; then
  echo "  ERROR: No CI run found for $HEAD_SHA after 100s"
  exit 1
fi
echo "  Watching image build (run $IMAGE_RUN, sha ${HEAD_SHA:0:10})..."
gh run watch "$IMAGE_RUN" --exit-status 2>&1 | tail -3
echo "  CI complete"

echo ""
echo "=== Step 3: Promote images ==="
./scripts/promote-to-production.sh | grep -E "^  |^Done"

OPERATOR_KUBECONFIGS=()
for kc in ~/secrets/ocpv{01,02,06,07,08,09}*.kubeconfig; do
  [ -f "$kc" ] && OPERATOR_KUBECONFIGS+=("$kc")
done
OPERATOR_KUBECONFIGS+=("$HOME/secrets/ocpvdev01.dal13.infra.demo.redhat.com.kubeconfig")

if [ "$SKIP_OPERATORS" = false ]; then
  echo ""
  echo "=== Step 3b: Apply operator CRDs + RBAC (parallel) ==="
  # Operator images are promoted above, but CRD schema changes (new spec fields)
  # only reach a cluster when the CRDs are applied. Do this before the operator
  # restart so the new reconcile logic sees the updated schema (unknown fields are
  # otherwise pruned on write). Applying CRDs is additive/idempotent.
  #
  # Operator ClusterRole (src/operator/deploy/clusterrole.yaml) is only created at
  # install time; rules added later never reach live clusters unless re-applied.
  #
  # Provider RBAC (infra/ocpvirt-rbac.yaml) is applied at cluster onboarding, so
  # permissions ADDED later never reach already-onboarded clusters. Re-apply —
  # but EXCLUDE SecurityContextConstraints (operator manages per-project SA lists).
  #
  # Per-NS RoleBindings are only synced when namespaced ClusterRoles change
  # (or --sync-ns-rbac); listing every troshka* NS on every deploy was the
  # dominant Step 3b cost.
  PROVIDER_RBAC=$(mktemp)
  python3 - infra/ocpvirt-rbac.yaml >"$PROVIDER_RBAC" <<'PY'
import sys
docs = open(sys.argv[1]).read().split("\n---\n")
keep = [
    d.strip()
    for d in docs
    if next(
        (l.split(":", 1)[1].strip() for l in d.splitlines() if l.strip().startswith("kind:")),
        "",
    )
    not in ("", "SecurityContextConstraints")
]
print("\n---\n".join(keep))
PY
  RBAC_DIR=$(mktemp -d)
  RBAC_PIDS=()
  for kc in "${OPERATOR_KUBECONFIGS[@]}"; do
    cluster=$(_cluster_name "$kc")
    _apply_cluster_rbac "$kc" "$PROVIDER_RBAC" "$RBAC_DIR/$cluster" "$FORCE_NS_RBAC" &
    RBAC_PIDS+=($!)
  done
  _wait_jobs "${RBAC_PIDS[@]}"
  _print_job_results "$RBAC_DIR"
  rm -rf "$RBAC_DIR"
  rm -f "$PROVIDER_RBAC"
fi

if [ "$SKIP_OPERATORS" = false ]; then
  echo ""
  echo "=== Step 4: Restart stale operators (parallel) ==="

  EXPECTED_DIGEST=$(skopeo inspect --format '{{.Digest}}' \
    "docker://quay.io/redhat-gpte/troshka-operator:production" 2>/dev/null || echo "")
  if [ -z "$EXPECTED_DIGEST" ]; then
    echo "  WARNING: Could not fetch production digest from registry, restarting all"
  else
    echo "  Production digest: ${EXPECTED_DIGEST:0:19}..."
  fi

  OP_DIR=$(mktemp -d)
  OP_PIDS=()
  for kc in "${OPERATOR_KUBECONFIGS[@]}"; do
    cluster=$(_cluster_name "$kc")
    _restart_operator_if_stale "$kc" "$EXPECTED_DIGEST" "$OP_DIR/$cluster" &
    OP_PIDS+=($!)
  done
  _wait_jobs "${OP_PIDS[@]}"
  _print_job_results "$OP_DIR"

  RESTARTED_CLUSTERS=()
  for kc in "${OPERATOR_KUBECONFIGS[@]}"; do
    cluster=$(_cluster_name "$kc")
    if [ "$(cat "$OP_DIR/$cluster" 2>/dev/null || true)" = "restarted" ]; then
      RESTARTED_CLUSTERS+=("$kc|$cluster")
    fi
  done
  rm -rf "$OP_DIR"

  if [ ${#RESTARTED_CLUSTERS[@]} -gt 0 ] && [ -n "$EXPECTED_DIGEST" ]; then
    echo ""
    echo "  Verifying ${#RESTARTED_CLUSTERS[@]} restarted operator(s) (parallel)..."
    sleep 10
    VER_DIR=$(mktemp -d)
    VER_PIDS=()
    for entry in "${RESTARTED_CLUSTERS[@]}"; do
      kc="${entry%%|*}"
      cluster="${entry##*|}"
      _verify_operator_digest "$kc" "$EXPECTED_DIGEST" "$VER_DIR/$cluster" &
      VER_PIDS+=($!)
    done
    _wait_jobs "${VER_PIDS[@]}"
    for entry in "${RESTARTED_CLUSTERS[@]}"; do
      cluster="${entry##*|}"
      printf "    %s: %s\n" "$cluster" "$(cat "$VER_DIR/$cluster")"
    done
    rm -rf "$VER_DIR"
  fi
fi

if [ "$SKIP_OPERATORS" = false ] && [ "$SKIP_PROJECT_PODS" = false ]; then
  echo ""
  echo "=== Step 4b: Restart stale per-project pods (parallel) ==="

  ROLE_NAMES=("vnc-proxy" "dnsmasq" "gateway" "bmc")
  ROLE_IMAGES=("troshka-vnc-proxy" "troshka-dnsmasq" "troshka-gateway" "troshka-bmc")
  ROLE_PAIRS=()
  for idx in "${!ROLE_NAMES[@]}"; do
    digest=$(skopeo inspect --format '{{.Digest}}' \
      "docker://quay.io/redhat-gpte/${ROLE_IMAGES[$idx]}:production" 2>/dev/null || echo "")
    ROLE_PAIRS+=("${ROLE_NAMES[$idx]}|$digest")
  done

  POD_DIR=$(mktemp -d)
  POD_PIDS=()
  for kc in "${OPERATOR_KUBECONFIGS[@]}"; do
    cluster=$(_cluster_name "$kc")
    _restart_stale_project_pods "$kc" "$POD_DIR/$cluster" "${ROLE_PAIRS[@]}" &
    POD_PIDS+=($!)
  done
  _wait_jobs "${POD_PIDS[@]}"
  _print_job_results "$POD_DIR"
  rm -rf "$POD_DIR"
fi

echo ""
echo "=== Step 5: Wait for ArgoCD (infra01) ==="
# troshka-tunnel runs the backend image (app.tunnel_main) — must roll with API.
ARGO_DEPLOYS=("troshka-backend" "troshka-frontend" "troshka-tunnel")
ARGO_LABELS=(
  "app.kubernetes.io/name=troshka-backend"
  "app.kubernetes.io/name=troshka-frontend"
  "app.kubernetes.io/name=troshka-tunnel"
)
ARGO_IMAGES=("troshka-backend" "troshka-frontend" "troshka-backend")

ARGO_DIGESTS=()
ALL_OK=true
for img in "${ARGO_IMAGES[@]}"; do
  digest=$(skopeo inspect --format '{{.Digest}}' \
    "docker://quay.io/redhat-gpte/${img}:production" 2>/dev/null || echo "")
  ARGO_DIGESTS+=("$digest")
  if [ -z "$digest" ]; then
    echo "  WARNING: Could not fetch ${img} production digest"
    ALL_OK=false
  fi
done

if [ "$ALL_OK" = true ]; then
  # 60 × 10s = 10 min max (same budget, faster discovery when Argo is quick)
  for i in $(seq 1 60); do
    ALL_MATCH=true
    for idx in "${!ARGO_DEPLOYS[@]}"; do
      if [ "${ARGO_DEPLOYS[$idx]}" = "troshka-tunnel" ] \
        && ! oc get deploy troshka-tunnel -n troshka --kubeconfig="$KC" >/dev/null 2>&1; then
        continue
      fi
      pod_image=$(oc get pods -n troshka --kubeconfig="$KC" \
        -l "${ARGO_LABELS[$idx]}" -o jsonpath='{.items[0].status.containerStatuses[0].imageID}' 2>/dev/null || echo "")
      if ! echo "$pod_image" | grep -qF "${ARGO_DIGESTS[$idx]}"; then
        ALL_MATCH=false
        break
      fi
    done
    if [ "$ALL_MATCH" = true ]; then
      echo "  ArgoCD synced all images"
      for dep in "${ARGO_DEPLOYS[@]}"; do
        if [ "$dep" = "troshka-tunnel" ] \
          && ! oc get deploy troshka-tunnel -n troshka --kubeconfig="$KC" >/dev/null 2>&1; then
          continue
        fi
        oc rollout status "deploy/${dep}" -n troshka --kubeconfig="$KC" --timeout=120s 2>/dev/null || true
      done
      break
    fi
    if [ "$i" -eq 60 ]; then
      echo "  Timed out (10 min) — check ArgoCD manually"
    fi
    echo "  Waiting for ArgoCD... ($((i * 10))s)"
    sleep 10
  done
fi

echo ""
echo "=== Done ==="
oc get pods -n troshka --kubeconfig="$KC" | grep -E "backend|frontend|worker|tunnel" | head -8
