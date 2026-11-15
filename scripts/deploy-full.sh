#!/usr/bin/env bash
set -euo pipefail

# Push main + a release tag → wait for its full build → promote to production
# → apply CRDs/RBAC → roll operators/project services → verify Argo workloads.
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
SKIP_PUSH=false
SKIP_OPERATORS=false
SKIP_PROJECT_PODS=false
FORCE_NS_RBAC=false
RELEASE_TAG=""
COLLECTOR_ACCESS_PLAN=""
COLLECTOR_SETUP_PLAN=""
usage() {
  echo "Usage: scripts/deploy-full.sh [--tag vNAME] [--skip-push] [--skip-operators] [--skip-project-pods] [--sync-ns-rbac] [--collector-access-plan FILE] [--collector-setup-plan FILE]"
  echo "Default: create vYYYY.MM.DD-HHMMSS (UTC) at HEAD and push HEAD to main plus the tag."
  echo "Resume an existing release with --skip-push --tag vNAME from its clean checkout."
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --tag) [ "$#" -ge 2 ] || { usage; exit 2; }; RELEASE_TAG=$2; shift 2 ;;
    --skip-push) SKIP_PUSH=true; shift ;;
    --skip-operators) SKIP_OPERATORS=true; shift ;;
    --skip-project-pods) SKIP_PROJECT_PODS=true; shift ;;
    --sync-ns-rbac) FORCE_NS_RBAC=true; shift ;;
    --collector-access-plan) [ "$#" -ge 2 ] || { usage; exit 2; }; COLLECTOR_ACCESS_PLAN=$2; shift 2 ;;
    --collector-setup-plan) [ "$#" -ge 2 ] || { usage; exit 2; }; COLLECTOR_SETUP_PLAN=$2; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

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

KC="$HOME/secrets/ocpv-infra01.dal12.infra.demo.redhat.com.kubeconfig"
DEV_KC="$HOME/secrets/ocpvdev01.dal13.infra.demo.redhat.com.kubeconfig"
OPERATOR_KUBECONFIGS=()
for cluster in ocpv01.dfw3 ocpv02.dfw3 ocpv06.dal10 ocpv07.wdc06 ocpv08.dal10 ocpv09.dal13 ocpvdev01.dal13; do
  OPERATOR_KUBECONFIGS+=("$HOME/secrets/$cluster.infra.demo.redhat.com.kubeconfig")
done

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

echo "=== Wait for the tagged full image build ==="
IMAGE_RUN=$(gh run list --branch "$RELEASE_TAG" --commit "$HEAD_REV" --limit 1 \
  -w build-images.yml --json databaseId --jq '.[0].databaseId // empty')
if [ -z "$IMAGE_RUN" ]; then
  echo "GitHub has not registered the tag build yet. Resume with --skip-push --tag $RELEASE_TAG." >&2
  exit 1
fi
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
echo "=== Preflight all ten release images ==="
python3 scripts/deploy_release.py images --tag "$RELEASE_TAG" --state "$STATE/digests.json"
python3 scripts/deploy_release.py stage-dev --tag "$RELEASE_TAG"
echo "=== Require dev Argo on this release and fresh accounting before promotion ==="
python3 scripts/deploy_release.py application --kubeconfig "$DEV_KC" \
  --namespace troshka-dev --image-tag latest --state "$STATE/digests.json"
python3 scripts/collector_fleet_preflight.py --application "$DEV_KC" troshka-dev \
  --output "$STATE/dev-collector-preflight.json"
python3 scripts/collector_release_check.py --application "$DEV_KC" troshka-dev \
  --output "$STATE/dev-accounting.json"
oc get deployment troshka-backend -n troshka --kubeconfig="$KC" --request-timeout=30s >/dev/null
if [ "$SKIP_OPERATORS" = false ]; then
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

if [ "$SKIP_OPERATORS" = false ]; then
  # Preserve operator-managed SCC membership while applying provider permissions.
  python3 - infra/ocpvirt-rbac.yaml >"$STATE/provider-rbac.yaml" <<'PY'
import sys
docs = open(sys.argv[1]).read().split("\n---\n")
keep = [d.strip() for d in docs if next(
    (line.split(":", 1)[1].strip() for line in d.splitlines() if line.startswith("kind:")), ""
) not in ("", "SecurityContextConstraints")]
print("\n---\n".join(keep))
PY
  PIDS=()
  for kc in "${OPERATOR_KUBECONFIGS[@]}"; do
    (
      apply_rbac "$kc"
      python3 scripts/deploy_release.py cluster --kubeconfig "$kc" --state "$STATE/digests.json" \
        --image-tag "$RELEASE_TAG" --skip-project-pods
    ) >"$STATE/$(basename "$kc").log" 2>&1 &
    PIDS+=($!)
  done
  FAILED=false
  for pid in "${PIDS[@]}"; do
    wait "$pid" || FAILED=true
  done
  for kc in "${OPERATOR_KUBECONFIGS[@]}"; do
    cat "$STATE/$(basename "$kc").log"
  done
else
  FAILED=false
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
python3 scripts/collector_release_check.py \
  --application "$KC" troshka --application "$DEV_KC" troshka-dev --output "$STATE/pre-promotion-accounting.json"
python3 scripts/deploy_release.py idle --kubeconfig "$KC"
./scripts/promote-to-production.sh "$RELEASE_TAG"
python3 scripts/deploy_release.py pin-application --kubeconfig "$KC" --tag "$RELEASE_TAG"
if [ "$SKIP_OPERATORS" = false ] && [ "$SKIP_PROJECT_PODS" = false ]; then
  for kc in "${OPERATOR_KUBECONFIGS[@]}"; do
    python3 scripts/deploy_release.py cluster --kubeconfig "$kc" --state "$STATE/digests.json"
  done
fi

echo "=== Verify Argo application, workers, tunnels, runtime schema and health ==="
oc annotate applications.argoproj.io troshka -n openshift-gitops \
  argocd.argoproj.io/refresh=normal --overwrite --kubeconfig="$KC" --request-timeout=30s
python3 scripts/deploy_release.py application --kubeconfig "$KC" --state "$STATE/digests.json"
echo "=== Verify collector configuration and fresh RQ accounting ==="
python3 scripts/collector_fleet_preflight.py \
  --application "$KC" troshka \
  --application "$HOME/secrets/ocpvdev01.dal13.infra.demo.redhat.com.kubeconfig" troshka-dev \
  --output "$COLLECTOR_REPORT"
ACCOUNTING_REPORT=$(mktemp "${TMPDIR:-/tmp}/troshka-collector-accounting.XXXXXX")
echo "Accounting evidence will be retained at: $ACCOUNTING_REPORT"
python3 scripts/collector_release_check.py \
  --application "$HOME/secrets/ocpvdev01.dal13.infra.demo.redhat.com.kubeconfig" troshka-dev \
  --application "$KC" troshka \
  --output "$ACCOUNTING_REPORT"
echo "=== Release $RELEASE_TAG deployed and verified ==="
