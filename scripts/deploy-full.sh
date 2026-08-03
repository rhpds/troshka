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
  echo ""
  echo "=== Step 4b: Restart stale per-project pods ==="

  ROLE_NAMES=("vnc-proxy" "dnsmasq" "gateway" "bmc")
  ROLE_IMAGES=("troshka-vnc-proxy" "troshka-dnsmasq" "troshka-gateway" "troshka-bmc")
  ROLE_DIGESTS=()
  for img in "${ROLE_IMAGES[@]}"; do
    ROLE_DIGESTS+=($(skopeo inspect --format '{{.Digest}}' \
      "docker://quay.io/redhat-gpte/${img}:production" 2>/dev/null || echo ""))
  done

  for kc in "${OPERATOR_KUBECONFIGS[@]}"; do
    cluster=$(basename "$kc" .kubeconfig | cut -d. -f1)
    restarted=0

    for idx in "${!ROLE_NAMES[@]}"; do
      role="${ROLE_NAMES[$idx]}"
      expected="${ROLE_DIGESTS[$idx]}"
      [ -z "$expected" ] && continue

      pod_image=$(oc get pods --all-namespaces -l "troshka-role=$role" \
        -o jsonpath='{.items[0].status.containerStatuses[0].imageID}' \
        --kubeconfig="$kc" 2>/dev/null || echo "")

      if [ -z "$pod_image" ] || echo "$pod_image" | grep -qF "$expected"; then
        continue
      fi

      deploys=$(oc get deploy --all-namespaces -l "troshka-role=$role" \
        -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name \
        --no-headers --kubeconfig="$kc" 2>/dev/null || echo "")
      while read -r ns name; do
        [ -z "$ns" ] && continue
        oc rollout restart "deploy/$name" -n "$ns" --kubeconfig="$kc" 2>/dev/null || true
        restarted=$((restarted + 1))
      done <<< "$deploys"
    done

    if [ "$restarted" -eq 0 ]; then
      printf "  %s: all current\n" "$cluster"
    else
      printf "  %s: restarted %d deployments\n" "$cluster" "$restarted"
    fi
  done
fi

echo ""
echo "=== Step 5: Wait for ArgoCD (infra01) ==="
ARGO_IMAGES=("troshka-backend" "troshka-frontend")
ARGO_LABELS=("app.kubernetes.io/name=troshka-backend" "app.kubernetes.io/name=troshka-frontend")
ARGO_DEPLOYS=("troshka-backend" "troshka-frontend")

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
  for i in $(seq 1 40); do
    ALL_MATCH=true
    for idx in "${!ARGO_IMAGES[@]}"; do
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
        oc rollout status "deploy/${dep}" -n troshka --kubeconfig="$KC" --timeout=120s 2>/dev/null || true
      done
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
