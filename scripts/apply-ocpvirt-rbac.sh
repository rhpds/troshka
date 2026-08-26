#!/usr/bin/env bash
# Apply infra/ocpvirt-rbac.yaml to every KubeVirt/OCP Virt provider cluster.
# Run after changing troshka-provider ClusterRole (e.g. troshkavms update verb).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MANIFEST="${ROOT}/infra/ocpvirt-rbac.yaml"

if [ ! -f "$MANIFEST" ]; then
  echo "ERROR: missing $MANIFEST" >&2
  exit 1
fi

KUBECONFIGS=()
for kc in "$HOME"/secrets/ocpv{01,03,06,07,08,09}*.kubeconfig; do
  [ -f "$kc" ] && KUBECONFIGS+=("$kc")
done
KUBECONFIGS+=("$HOME/secrets/ocpvdev01.dal13.infra.demo.redhat.com.kubeconfig")

if [ "${#KUBECONFIGS[@]}" -eq 0 ]; then
  echo "ERROR: no ocpv kubeconfigs found under ~/secrets/" >&2
  exit 1
fi

echo "Applying $MANIFEST to ${#KUBECONFIGS[@]} cluster(s)..."
FAILED=0
for kc in "${KUBECONFIGS[@]}"; do
  cluster=$(basename "$kc" .kubeconfig | cut -d. -f1)
  printf "  %s: " "$cluster"
  if oc apply -f "$MANIFEST" --kubeconfig="$kc" >/dev/null 2>&1; then
    # Verify troshkavms has update (rule may move — grep the live object)
    if oc get clusterrole troshka-provider --kubeconfig="$kc" -o yaml 2>/dev/null \
      | grep -A6 'troshkavms' | grep -q 'update'; then
      echo "ok (troshkavms update present)"
    else
      echo "applied (verify troshkavms update manually)"
    fi
  else
    echo "FAILED"
    FAILED=$((FAILED + 1))
  fi
done

if [ "$FAILED" -gt 0 ]; then
  echo "ERROR: $FAILED cluster(s) failed" >&2
  exit 1
fi
echo "Done."
