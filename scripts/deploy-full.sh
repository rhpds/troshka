#!/usr/bin/env bash
set -euo pipefail

# Full deploy cycle: push → wait CI → promote → restart operators → wait ArgoCD
# Usage: scripts/deploy-full.sh [--skip-push] [--skip-operators]

SKIP_PUSH=false
SKIP_OPERATORS=false
for arg in "$@"; do
  case "$arg" in
    --skip-push) SKIP_PUSH=true ;;
    --skip-operators) SKIP_OPERATORS=true ;;
  esac
done

KC="$HOME/secrets/ocpv-infra01.dal12.infra.demo.redhat.com.kubeconfig"

echo "=== Step 1: Push to main ==="
if [ "$SKIP_PUSH" = true ]; then
  echo "  Skipped (--skip-push)"
else
  git push origin main
fi

echo ""
echo "=== Step 2: Wait for CI ==="
IMAGE_RUN=$(gh run list --limit 1 --json databaseId --jq '.[0].databaseId' -w "Build and Push Container Images")
echo "  Watching image build (run $IMAGE_RUN)..."
gh run watch "$IMAGE_RUN" --exit-status 2>&1 | tail -3
echo "  CI complete"

# (ArgoCD detection uses registry digest, not before/after diff)

echo ""
echo "=== Step 3: Promote images ==="
./scripts/promote-to-production.sh | grep -E "^  |^Done"

if [ "$SKIP_OPERATORS" = false ]; then
  echo ""
  echo "=== Step 4: Restart stale operators ==="

  # Get the expected digest from the freshly-promoted production tag
  EXPECTED_DIGEST=$(skopeo inspect --format '{{.Digest}}' \
    "docker://quay.io/redhat-gpte/troshka-operator:production" 2>/dev/null || echo "")
  if [ -z "$EXPECTED_DIGEST" ]; then
    echo "  WARNING: Could not fetch production digest from registry, restarting all"
  else
    echo "  Production digest: ${EXPECTED_DIGEST:0:19}..."
  fi

  OPERATOR_KUBECONFIGS=()
  for kc in ~/secrets/ocpv{01,03,05,06,07,08,09,10}*.kubeconfig; do
    [ -f "$kc" ] && OPERATOR_KUBECONFIGS+=("$kc")
  done
  OPERATOR_KUBECONFIGS+=("$HOME/secrets/ocpvdev01.dal13.infra.demo.redhat.com.kubeconfig")

  RESTARTED_CLUSTERS=()
  for kc in "${OPERATOR_KUBECONFIGS[@]}"; do
    cluster=$(basename "$kc" .kubeconfig | cut -d. -f1)
    printf "  %s: " "$cluster"

    # Get the running operator's image reference (includes @sha256: if pulled by digest)
    RUNNING_IMAGE=$(oc get deploy troshka-operator -n troshka-operator --kubeconfig="$kc" \
      -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo "unknown")

    if [ -n "$EXPECTED_DIGEST" ] && echo "$RUNNING_IMAGE" | grep -qF "$EXPECTED_DIGEST"; then
      echo "current"
      continue
    fi

    oc rollout restart deployment/troshka-operator -n troshka-operator --kubeconfig="$kc" 2>&1 | grep -o "restarted"
    RESTARTED_CLUSTERS+=("$kc|$cluster")
  done

  # Verify restarted operators picked up the new image
  if [ ${#RESTARTED_CLUSTERS[@]} -gt 0 ] && [ -n "$EXPECTED_DIGEST" ]; then
    echo ""
    echo "  Verifying ${#RESTARTED_CLUSTERS[@]} restarted operator(s)..."
    sleep 10
    for entry in "${RESTARTED_CLUSTERS[@]}"; do
      kc="${entry%%|*}"
      cluster="${entry##*|}"
      printf "    %s: " "$cluster"
      VERIFIED=false
      for attempt in 1 2 3 4 5 6; do
        NEW_IMAGE=$(oc get pods -n troshka-operator --kubeconfig="$kc" \
          -l app=troshka-operator -o jsonpath='{.items[0].status.containerStatuses[0].imageID}' 2>/dev/null || echo "")
        if echo "$NEW_IMAGE" | grep -qF "$EXPECTED_DIGEST"; then
          echo "verified"
          VERIFIED=true
          break
        fi
        sleep 5
      done
      if [ "$VERIFIED" = false ]; then
        echo "NOT verified (may still be rolling out)"
      fi
    done
  fi
fi

echo ""
echo "=== Step 5: Wait for ArgoCD (infra01) ==="
EXPECTED_BACKEND=$(skopeo inspect --format '{{.Digest}}' \
  "docker://quay.io/redhat-gpte/troshka-backend:production" 2>/dev/null || echo "")
if [ -z "$EXPECTED_BACKEND" ]; then
  echo "  WARNING: Could not fetch backend production digest, skipping wait"
else
  echo "  Expected digest: ${EXPECTED_BACKEND:0:19}..."
  for i in $(seq 1 40); do
    POD_IMAGE=$(oc get pods -n troshka --kubeconfig="$KC" \
      -l app=troshka-backend -o jsonpath='{.items[0].status.containerStatuses[0].imageID}' 2>/dev/null || echo "")
    if echo "$POD_IMAGE" | grep -qF "$EXPECTED_BACKEND"; then
      echo "  ArgoCD updated backend image"
      oc rollout status deploy/troshka-backend -n troshka --kubeconfig="$KC" --timeout=120s 2>/dev/null || true
      break
    fi
    if [ "$i" -eq 40 ]; then
      echo "  Timed out (10 min) — check ArgoCD manually"
    fi
    echo "  Waiting for ArgoCD... ($((i * 15))s)"
    sleep 15
  done
fi

echo ""
echo "=== Done ==="
oc get pods -n troshka --kubeconfig="$KC" | grep -E "backend|frontend|worker" | head -5
