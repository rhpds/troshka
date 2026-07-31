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
for i in $(seq 1 60); do
  status=$(gh run list --limit 2 --json status,workflowName --jq '
    [.[] | select(.workflowName == "Build and Push Container Images" or .workflowName == "Backend CI")]
    | map(.status) | if all(. == "completed") then "done" else "running" end
  ')
  if [ "$status" = "done" ]; then
    conclusions=$(gh run list --limit 2 --json conclusion,workflowName --jq '
      [.[] | select(.workflowName == "Build and Push Container Images" or .workflowName == "Backend CI")]
      | map("\(.workflowName): \(.conclusion)") | join(", ")
    ')
    echo "  CI complete: $conclusions"
    # Check for failures
    if echo "$conclusions" | grep -q "failure"; then
      echo "  CI FAILED — aborting"
      exit 1
    fi
    break
  fi
  printf "\r  Waiting for CI... (%ds)" "$((i * 10))"
  sleep 10
done

echo ""
echo "=== Step 3: Promote images ==="
./scripts/promote-to-production.sh | grep -E "^  |^Done"

if [ "$SKIP_OPERATORS" = false ]; then
  echo ""
  echo "=== Step 4: Restart operators ==="
  for kc in ~/secrets/ocpv{01,03,05,06,07,08,09,10}*.kubeconfig; do
    cluster=$(basename "$kc" .kubeconfig | cut -d. -f1)
    printf "  %s: " "$cluster"
    oc rollout restart deployment/troshka-operator -n troshka-operator --kubeconfig="$kc" 2>&1 | grep -o "restarted"
  done
  printf "  ocpvdev01: "
  oc rollout restart deployment/troshka-operator -n troshka-operator \
    --kubeconfig="$HOME/secrets/ocpvdev01.dal13.infra.demo.redhat.com.kubeconfig" 2>&1 | grep -o "restarted"
fi

echo ""
echo "=== Step 5: Wait for ArgoCD (infra01) ==="
for i in $(seq 1 30); do
  age=$(oc get pods -n troshka --kubeconfig="$KC" 2>/dev/null \
    | grep "backend.*Running" | awk '{print $5}')
  if echo "$age" | grep -qE "^[0-9]+s$|^[01]m"; then
    echo "  Backend updated ($age)"
    break
  fi
  printf "\r  Waiting for ArgoCD... (%ds)" "$((i * 15))"
  sleep 15
done

echo ""
echo "=== Done ==="
oc get pods -n troshka --kubeconfig="$KC" | grep -E "backend|frontend|worker" | head -5
