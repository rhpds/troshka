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
usage() {
  echo "Usage: scripts/deploy-full.sh [--tag vNAME] [--skip-push] [--skip-operators] [--skip-project-pods] [--sync-ns-rbac]"
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
OPERATOR_KUBECONFIGS=()
for cluster in ocpv01.dfw3 ocpv02.dfw3 ocpv06.dal10 ocpv07.wdc06 ocpv08.dal10 ocpv09.dal13 ocpvdev01.dal13; do
  OPERATOR_KUBECONFIGS+=("$HOME/secrets/$cluster.infra.demo.redhat.com.kubeconfig")
done

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

STATE=$(mktemp -d)
trap 'rm -rf "$STATE"' EXIT
echo "=== Preflight and promote all ten images to production ==="
python3 scripts/deploy_release.py images --tag "$RELEASE_TAG" --state "$STATE/digests.json"
oc get deployment troshka-backend -n troshka --kubeconfig="$KC" --request-timeout=30s >/dev/null
if [ "$SKIP_OPERATORS" = false ]; then
  for kc in "${OPERATOR_KUBECONFIGS[@]}"; do
    oc get deployment troshka-operator -n troshka-operator --kubeconfig="$kc" --request-timeout=30s >/dev/null
  done
fi
python3 scripts/deploy_release.py idle --kubeconfig "$KC"
./scripts/promote-to-production.sh "$RELEASE_TAG"

apply_rbac() {
  local kc=$1 before after ns role sa
  before=$(oc get clusterrole troshka-provider-namespaced troshka-operator-namespaced \
    --ignore-not-found --kubeconfig="$kc" --request-timeout=30s -o jsonpath='{range .items[*]}{.metadata.resourceVersion}{" "}{end}')
  oc apply -f src/operator/crds/ --kubeconfig="$kc" --request-timeout=30s
  oc apply -f src/operator/deploy/clusterrole.yaml --kubeconfig="$kc" --request-timeout=30s
  oc apply -f "$STATE/provider-rbac.yaml" --kubeconfig="$kc" --request-timeout=30s
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
      EXTRA=()
      [ "$SKIP_PROJECT_PODS" = false ] || EXTRA+=(--skip-project-pods)
      python3 scripts/deploy_release.py cluster --kubeconfig "$kc" --state "$STATE/digests.json" ${EXTRA[@]+"${EXTRA[@]}"}
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

echo "=== Verify Argo application, workers, tunnels, runtime schema and health ==="
oc annotate applications.argoproj.io troshka -n openshift-gitops \
  argocd.argoproj.io/refresh=normal --overwrite --kubeconfig="$KC" --request-timeout=30s
python3 scripts/deploy_release.py application --kubeconfig "$KC" --state "$STATE/digests.json"
if [ "$FAILED" = true ]; then
  echo "Release promoted, but one or more cluster rollouts failed. See errors above." >&2
  exit 1
fi
echo "=== Release $RELEASE_TAG deployed and verified ==="
