#!/usr/bin/env bash
set -euo pipefail

# Push main + a release tag → wait for its image build → promote changed images
# → apply CRDs/RBAC only when needed → roll only hosts/roles that changed.
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
SKIP_PUSH=false
SKIP_OPERATORS=false
SKIP_PROJECT_PODS=false
FORCE_NS_RBAC=false
RELEASE_TAG=""
COLLECTOR_ACCESS_PLAN=""
COLLECTOR_SETUP_PLAN=""
ACCOUNTING_MODE=health
VERIFY_STORAGE=false
FULL_ACCOUNTING=false
ACCOUNTING_PROJECTS=()
usage() {
  echo "Usage: scripts/deploy-full.sh [--tag vNAME] [--skip-push] [--skip-operators] [--skip-project-pods] [--sync-ns-rbac] [--collector-access-plan FILE] [--collector-setup-plan FILE]"
  echo "Default: create vYYYY.MM.DD-HHMMSS (UTC) at HEAD and push HEAD to main plus the tag."
  echo "Resume an existing release with --skip-push --tag vNAME from its clean checkout."
  echo "Hosts come from scripts/deploy-hosts.local.yaml (gitignored); see deploy-hosts.local.yaml.example."
  echo "Collector fleet coverage is required. Storage accounting is optional: --verify-storage checks existing evidence; --fresh-accounting scans all projects; --accounting-project UUID scans selected production projects."
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --tag) [ "$#" -ge 2 ] || { usage; exit 2; }; RELEASE_TAG=$2; shift 2 ;;
    --skip-push) SKIP_PUSH=true; shift ;;
    --skip-operators) SKIP_OPERATORS=true; shift ;;
    --skip-project-pods) SKIP_PROJECT_PODS=true; shift ;;
    --sync-ns-rbac) FORCE_NS_RBAC=true; shift ;;
    --verify-storage) VERIFY_STORAGE=true; shift ;;
    --fresh-accounting) VERIFY_STORAGE=true; ACCOUNTING_MODE=fresh; FULL_ACCOUNTING=true; shift ;;
    --accounting-project) [ "$#" -ge 2 ] || { usage; exit 2; }; VERIFY_STORAGE=true; ACCOUNTING_MODE=fresh; ACCOUNTING_PROJECTS+=(--project "$2"); shift 2 ;;
    --collector-access-plan) [ "$#" -ge 2 ] || { usage; exit 2; }; COLLECTOR_ACCESS_PLAN=$2; shift 2 ;;
    --collector-setup-plan) [ "$#" -ge 2 ] || { usage; exit 2; }; COLLECTOR_SETUP_PLAN=$2; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done
if [ "$FULL_ACCOUNTING" = true ] && [ "${#ACCOUNTING_PROJECTS[@]}" -gt 0 ]; then
  echo "Choose --fresh-accounting or --accounting-project, not both." >&2
  exit 2
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "Commit or stash changes before deploying a release." >&2
  exit 1
fi
HEAD_REV=$(git rev-parse HEAD)
if [ -z "$RELEASE_TAG" ]; then
  if [ "$SKIP_PUSH" = true ]; then
    echo "--skip-push requires --tag so the intended release is unambiguous." >&2
    exit 2
  fi
  RELEASE_TAG="v$(date -u +%Y.%m.%d-%H%M%S)"
fi
if ! [[ "$RELEASE_TAG" =~ ^v[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || [ "${#RELEASE_TAG}" -gt 128 ]; then
  echo "Release tags must start with v and be valid container tags." >&2
  exit 2
fi

# Log without requiring callers to `| tee` (that masks non-zero exits when the
# caller shell lacks pipefail). Script exit status stays on this process.
DEPLOY_LOG="${DEPLOY_LOG:-/tmp/troshka-deploy-full-${RELEASE_TAG}.log}"
echo "Logging to $DEPLOY_LOG"
exec > >(tee -a "$DEPLOY_LOG") 2>&1

# Local host inventory (gitignored). Absolute paths or short names + domain_suffix.
eval "$(python3 scripts/deploy_hosts.py shell)"
if [ -z "${KC:-}" ] || [ -z "${DEV_KC:-}" ] || [ "${#OPERATOR_KUBECONFIGS[@]}" -eq 0 ]; then
  echo "deploy-hosts.local.yaml did not resolve production/dev/operator kubeconfigs." >&2
  exit 1
fi

join_roles() {
  local IFS=,
  echo "$*"
}

preflight_helper_networks() {
  if [ "$SKIP_OPERATORS" = true ] || [ "${#CLUSTER_ROLES[@]}" -eq 0 ]; then
    echo "Skipping helper-network preflight (no changed cluster image roles)."
    return
  fi
  local kc
  local flags=()
  local role_args
  role_args=$(join_roles "${CLUSTER_ROLES[@]}")
  [ "$SKIP_PROJECT_PODS" = false ] || flags+=(--skip-project-pods)
  for kc in "${OPERATOR_KUBECONFIGS[@]}"; do
    python3 scripts/deploy_release.py preflight --kubeconfig "$kc" --roles "$role_args" "${flags[@]}"
  done
}

COLLECTOR_REPORT=$(mktemp "${TMPDIR:-/tmp}/troshka-collector-preflight.XXXXXX")
echo "Collector configuration report: $COLLECTOR_REPORT"

echo "=== Release $RELEASE_TAG ==="
if [ "$SKIP_PUSH" = false ]; then
  if ! git show-ref --verify --quiet "refs/tags/$RELEASE_TAG"; then
    git tag -a "$RELEASE_TAG" -m "Troshka full release $RELEASE_TAG"
  fi
  [ "$(git rev-parse "refs/tags/$RELEASE_TAG^{commit}")" = "$HEAD_REV" ] || {
    echo "Release tag does not match this checkout." >&2; exit 1;
  }
  git push --atomic origin HEAD:main "refs/tags/$RELEASE_TAG"
fi
# Refresh remote evidence even on resume; never deploy a local-only or moved tag.
git fetch origin main "refs/tags/$RELEASE_TAG:refs/tags/$RELEASE_TAG"
[ "$(git rev-parse "refs/tags/$RELEASE_TAG^{commit}")" = "$HEAD_REV" ] || {
  echo "Check out the requested release before resuming." >&2; exit 1;
}
git merge-base --is-ancestor "$HEAD_REV" origin/main || {
  echo "Release is not on origin/main." >&2; exit 1;
}

echo "=== Wait for the tagged image build ==="
# Tag push → Actions registration can lag a few seconds; poll instead of
# immediately exiting and forcing a manual --skip-push resume.
IMAGE_RUN_WAIT_SECONDS="${IMAGE_RUN_WAIT_SECONDS:-300}"
IMAGE_RUN_POLL_SECONDS="${IMAGE_RUN_POLL_SECONDS:-5}"
IMAGE_RUN=""
IMAGE_WAIT_DEADLINE=$((SECONDS + IMAGE_RUN_WAIT_SECONDS))
while [ -z "$IMAGE_RUN" ]; do
  IMAGE_RUN=$(gh run list --branch "$RELEASE_TAG" --commit "$HEAD_REV" --limit 1 \
    -w build-images.yml --json databaseId --jq '.[0].databaseId // empty' || true)
  if [ -n "$IMAGE_RUN" ]; then
    break
  fi
  if [ "$SECONDS" -ge "$IMAGE_WAIT_DEADLINE" ]; then
    echo "GitHub did not register the tag build within ${IMAGE_RUN_WAIT_SECONDS}s. Resume with --skip-push --tag $RELEASE_TAG." >&2
    exit 1
  fi
  echo "Waiting for GitHub to register tag build $RELEASE_TAG..."
  sleep "$IMAGE_RUN_POLL_SECONDS"
done
gh run watch "$IMAGE_RUN" --exit-status
# watch may return immediately for an already-completed run; check explicitly.
[ "$(gh run view "$IMAGE_RUN" --json conclusion --jq .conclusion)" = success ] || {
  echo "Release build did not succeed." >&2; exit 1;
}
# Both builds publish latest. Wait for the main build before aligning dev with
# the named release, so it cannot overwrite the candidate after verification.
MAIN_IMAGE_RUN=$(gh run list --branch main --commit "$HEAD_REV" --limit 1 \
  -w build-images.yml --json databaseId --jq '.[0].databaseId // empty')
if [ -n "$MAIN_IMAGE_RUN" ]; then
  gh run watch "$MAIN_IMAGE_RUN" --exit-status
  [ "$(gh run view "$MAIN_IMAGE_RUN" --json conclusion --jq .conclusion)" = success ] || {
    echo "Main image build did not succeed." >&2; exit 1;
  }
fi

STATE=$(mktemp -d)
trap 'rm -rf "$STATE"' EXIT
echo "=== Preflight release images ==="
python3 scripts/deploy_release.py images --tag "$RELEASE_TAG" --state "$STATE/digests.json"
python3 scripts/deploy_release.py plan --tag "$RELEASE_TAG" --state "$STATE/plan.json"
CHANGED_ROLES=()
CLUSTER_ROLES=()
APP_ROLES=()
while IFS= read -r role; do
  [ -n "$role" ] || continue
  CHANGED_ROLES+=("$role")
done < <(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))["changed_roles"]))' "$STATE/plan.json")
while IFS= read -r role; do
  [ -n "$role" ] || continue
  CLUSTER_ROLES+=("$role")
done < <(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))["cluster_roles_changed"]))' "$STATE/plan.json")
while IFS= read -r role; do
  [ -n "$role" ] || continue
  APP_ROLES+=("$role")
done < <(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))["app_roles_changed"]))' "$STATE/plan.json")
APPLY_CRDS_RBAC=$(python3 -c 'import json,sys; print("true" if json.load(open(sys.argv[1]))["apply_crds_rbac"] else "false")' "$STATE/plan.json")
if [ "$FORCE_NS_RBAC" = true ]; then
  APPLY_CRDS_RBAC=true
fi
echo "Changed image roles: ${CHANGED_ROLES[*]:-none}"
echo "Cluster rollout roles: ${CLUSTER_ROLES[*]:-none}"
echo "App wait roles: ${APP_ROLES[*]:-none}"
echo "Apply CRDs/RBAC: $APPLY_CRDS_RBAC"

# Fail closed on helper-network gaps before staging/mutating workloads.
preflight_helper_networks

# Pin carried app images too: chart-only releases must pass the same dev gate.
python3 scripts/deploy_release.py stage-dev --tag "$RELEASE_TAG" --roles backend,frontend \
  --kubeconfig "$DEV_KC" --namespace troshka-dev
echo "=== Require dev Argo on named release images ==="
python3 scripts/deploy_release.py application --kubeconfig "$DEV_KC" \
  --namespace troshka-dev --image-tag "$RELEASE_TAG" --state "$STATE/digests.json" \
  --roles backend,frontend
python3 scripts/collector_fleet_preflight.py --application "$DEV_KC" troshka-dev \
  --output "$STATE/dev-collector-preflight.json"
if [ "$VERIFY_STORAGE" = true ]; then
  python3 scripts/collector_release_check.py --application "$DEV_KC" troshka-dev \
    --mode health --output "$STATE/dev-accounting.json"
fi
oc get deployment troshka-backend -n troshka --kubeconfig="$KC" --request-timeout=30s >/dev/null
if [ "$SKIP_OPERATORS" = false ] && [ "${#CLUSTER_ROLES[@]}" -gt 0 ]; then
  for kc in "${OPERATOR_KUBECONFIGS[@]}"; do
    oc get deployment troshka-operator -n troshka-operator --kubeconfig="$kc" --request-timeout=30s >/dev/null
  done
fi
python3 scripts/deploy_release.py idle --kubeconfig "$KC"
if [ -n "$COLLECTOR_ACCESS_PLAN" ]; then
  python3 scripts/deploy_collector_rbac.py --plan "$COLLECTOR_ACCESS_PLAN" --output "$STATE/collector-access"
  python3 scripts/deploy_collector_rbac.py --apply --output "$STATE/collector-access"
fi

apply_rbac() {
  local kc=$1 before after ns role sa
  before=$(oc get clusterrole troshka-provider-namespaced troshka-operator-namespaced \
    --ignore-not-found --kubeconfig="$kc" --request-timeout=30s -o jsonpath='{range .items[*]}{.metadata.resourceVersion}{" "}{end}')
  oc apply -f src/operator/crds/ --kubeconfig="$kc" --request-timeout=30s
  oc apply -f src/operator/deploy/clusterrole.yaml --kubeconfig="$kc" --request-timeout=30s
  oc apply -f src/operator/deploy/collector-maintenance.yaml --kubeconfig="$kc" --request-timeout=30s
  oc apply -f "$STATE/provider-rbac.yaml" --kubeconfig="$kc" --request-timeout=30s
  oc apply -f infra/storage-collector-base-rbac.yaml --kubeconfig="$kc" --request-timeout=30s
  oc apply -f infra/storage-collector-project-rbac.yaml --kubeconfig="$kc" --request-timeout=30s
  after=$(oc get clusterrole troshka-provider-namespaced troshka-operator-namespaced \
    --kubeconfig="$kc" --request-timeout=30s -o jsonpath='{range .items[*]}{.metadata.resourceVersion}{" "}{end}')
  if [ "$FORCE_NS_RBAC" = true ] || [ "$before" != "$after" ]; then
    oc get ns --kubeconfig="$kc" --request-timeout=30s -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' >"$STATE/$(basename "$kc").namespaces"
    while IFS= read -r ns; do
      [[ "$ns" == troshka* ]] || continue
      for role in troshka-provider-namespaced troshka-operator-namespaced; do
        sa=troshka:troshka
        [ "$role" != troshka-operator-namespaced ] || sa=troshka-operator:troshka-operator
        oc create rolebinding "$role" --clusterrole="$role" --serviceaccount="$sa" \
          -n "$ns" --dry-run=client -o yaml --kubeconfig="$kc" | \
          oc apply -f - --kubeconfig="$kc" --request-timeout=30s
      done
    done <"$STATE/$(basename "$kc").namespaces"
  fi
}

FAILED=false
if [ "$SKIP_OPERATORS" = false ] && { [ "$APPLY_CRDS_RBAC" = true ] || [ "${#CLUSTER_ROLES[@]}" -gt 0 ]; }; then
  if [ "$APPLY_CRDS_RBAC" = true ]; then
    # Preserve operator-managed SCC membership while applying provider permissions.
    python3 - infra/ocpvirt-rbac.yaml >"$STATE/provider-rbac.yaml" <<'PY'
import sys
docs = open(sys.argv[1]).read().split("\n---\n")
keep = [d.strip() for d in docs if next(
    (line.split(":", 1)[1].strip() for line in d.splitlines() if line.startswith("kind:")), ""
) not in ("", "SecurityContextConstraints")]
print("\n---\n".join(keep))
PY
  else
    echo "Skipping CRD/RBAC apply (unchanged since previous release)."
  fi
  PIDS=()
  CLUSTER_ROLE_ARGS=()
  if [ "${#CLUSTER_ROLES[@]}" -gt 0 ]; then
    CLUSTER_ROLE_ARGS=(--roles "$(join_roles "${CLUSTER_ROLES[@]}")")
  fi
  for kc in "${OPERATOR_KUBECONFIGS[@]}"; do
    (
      if [ "$APPLY_CRDS_RBAC" = true ]; then
        apply_rbac "$kc"
      fi
      if [ "${#CLUSTER_ROLES[@]}" -gt 0 ]; then
        python3 scripts/deploy_release.py cluster --kubeconfig "$kc" --state "$STATE/digests.json" \
          --image-tag "$RELEASE_TAG" --skip-project-pods "${CLUSTER_ROLE_ARGS[@]}"
      else
        echo "$(basename "$kc"): skipped cluster image rollout (no changed operator images)"
      fi
    ) >"$STATE/$(basename "$kc").log" 2>&1 &
    PIDS+=($!)
  done
  for pid in "${PIDS[@]}"; do
    wait "$pid" || FAILED=true
  done
  for kc in "${OPERATOR_KUBECONFIGS[@]}"; do
    cat "$STATE/$(basename "$kc").log"
  done
else
  echo "Skipping operator host CRD/image stage (unchanged or --skip-operators)."
fi

if [ "$FAILED" = true ]; then
  echo "Operator cluster rollouts failed; production application was not promoted." >&2
  exit 1
fi
echo "=== Configure collectors after host prerequisites, before app promotion ==="
if [ -n "$COLLECTOR_SETUP_PLAN" ]; then
  COLLECTOR_STATE=$(mktemp -d "${TMPDIR:-/tmp}/troshka-collector-setup.XXXXXX")
  echo "Private collector setup evidence will be retained at: $COLLECTOR_STATE"
  python3 scripts/collector_provision.py --plan "$COLLECTOR_SETUP_PLAN" --output "$COLLECTOR_STATE/prepared" \
    --application "$KC" troshka --application "$DEV_KC" troshka-dev
  python3 scripts/collector_provision.py --apply --output "$COLLECTOR_STATE/prepared"
fi
python3 scripts/collector_fleet_preflight.py \
  --application "$KC" troshka --application "$DEV_KC" troshka-dev --output "$COLLECTOR_REPORT"
if [ "$VERIFY_STORAGE" = true ]; then
  ACCOUNTING_BASELINE=$(mktemp "${TMPDIR:-/tmp}/troshka-accounting-baseline.XXXXXX")
  echo "Existing accounting baseline will be retained at: $ACCOUNTING_BASELINE"
  python3 scripts/collector_release_check.py \
    --mode health --application "$KC" troshka --application "$DEV_KC" troshka-dev --output "$ACCOUNTING_BASELINE"
fi
python3 scripts/deploy_release.py idle --kubeconfig "$KC"
preflight_helper_networks
if [ "${#CHANGED_ROLES[@]}" -gt 0 ]; then
  python3 scripts/deploy_release.py promote --tag "$RELEASE_TAG" --roles "$(join_roles "${CHANGED_ROLES[@]}")" \
    --state "$STATE/digests.json"
else
  echo "Skipping promote (all production digests already match $RELEASE_TAG)."
fi
python3 scripts/deploy_release.py pin-application --kubeconfig "$KC" --tag "$RELEASE_TAG" --roles backend,frontend
if [ "$SKIP_OPERATORS" = false ] && [ "$SKIP_PROJECT_PODS" = false ] && [ "${#CLUSTER_ROLES[@]}" -gt 0 ]; then
  for kc in "${OPERATOR_KUBECONFIGS[@]}"; do
    python3 scripts/deploy_release.py cluster --kubeconfig "$kc" --state "$STATE/digests.json" \
      --roles "$(join_roles "${CLUSTER_ROLES[@]}")"
  done
fi

echo "=== Verify Argo application, workers, tunnels, runtime schema and health ==="
oc annotate applications.argoproj.io troshka -n openshift-gitops \
  argocd.argoproj.io/refresh=normal --overwrite --kubeconfig="$KC" --request-timeout=30s
python3 scripts/deploy_release.py application --kubeconfig "$KC" --state "$STATE/digests.json" \
  --image-tag "$RELEASE_TAG" --roles backend,frontend
echo "=== Verify collector configuration across the fleet ==="
python3 scripts/collector_fleet_preflight.py \
  --application "$KC" troshka \
  --application "$DEV_KC" troshka-dev --output "$COLLECTOR_REPORT"
if [ "$VERIFY_STORAGE" = true ]; then
  echo "=== Verify storage accounting ($ACCOUNTING_MODE) ==="
  ACCOUNTING_REPORT=$(mktemp "${TMPDIR:-/tmp}/troshka-collector-accounting.XXXXXX")
  echo "Accounting evidence will be retained at: $ACCOUNTING_REPORT"
  python3 scripts/collector_release_check.py \
    --application "$DEV_KC" troshka-dev \
    --mode "$ACCOUNTING_MODE" --output "$ACCOUNTING_REPORT.dev"
  python3 scripts/collector_release_check.py \
    --application "$KC" troshka --mode "$ACCOUNTING_MODE" "${ACCOUNTING_PROJECTS[@]}" \
    --output "$ACCOUNTING_REPORT"
else
  echo "Storage accounting verification not requested; use --verify-storage or --fresh-accounting to enable it."
fi
echo "=== Release $RELEASE_TAG deployed and verified ==="
