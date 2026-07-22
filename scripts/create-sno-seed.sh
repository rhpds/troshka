#!/bin/bash
set -euo pipefail

SEED_IMAGE="${1:-quay.io/redhat-gpte/sno-seed:4.22}"
SEED_AUTH_FILE="${2:-/home/cloud-user/.docker/config.json}"
KUBECONFIG="/home/cloud-user/ocp-install/auth/kubeconfig"

echo "=== SNO Seed Image Creation ==="
echo "Target: $SEED_IMAGE"
echo "Kubeconfig: $KUBECONFIG"
echo ""

export KUBECONFIG

# Check cluster is healthy
echo "Checking cluster health..."
oc get clusterversion version -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' | grep -q True || { echo "ERROR: Cluster not available"; exit 1; }
echo "Cluster is healthy"

# Ensure ImageTagMirrorSet exists if cluster uses imageDigestSources (pull-through registry)
if oc get imagedigestmirrorset -o name 2>/dev/null | grep -q .; then
    if ! oc get imagetagmirrorset pull-through-registry-tags 2>/dev/null | grep -q .; then
        echo "Creating ImageTagMirrorSet for catalog source pulls..."
        MIRRORS=$(oc get imagedigestmirrorset -o jsonpath='{range .items[0].spec.imageDigestMirrors[*]}{.source}={.mirrors[0]}{"\n"}{end}' 2>/dev/null)
        cat <<ITMSEOF | oc apply -f -
apiVersion: config.openshift.io/v1
kind: ImageTagMirrorSet
metadata:
  name: pull-through-registry-tags
spec:
  imageTagMirrors:
$(echo "$MIRRORS" | while IFS='=' read -r src mirror; do
    [ -n "$src" ] && echo "    - source: $src
      mirrors:
        - $mirror"
done)
ITMSEOF
        echo "Waiting for MCP update after ITMS creation..."
        sleep 30
        for i in $(seq 1 30); do
            if oc get mcp master -o jsonpath='{.status.conditions[?(@.type=="Updated")].status}' 2>/dev/null | grep -q True; then
                echo "MCP updated"
                break
            fi
            echo "  MCP updating (attempt ${i}/30)..."
            sleep 20
        done
    fi
fi

# Install LCA operator if not present
if ! oc get csv -n openshift-lifecycle-agent 2>/dev/null | grep -q lifecycle-agent; then
    echo "Installing Lifecycle Agent operator..."
    cat <<LCAEOF | oc apply -f -
apiVersion: v1
kind: Namespace
metadata:
  name: openshift-lifecycle-agent
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: lifecycle-agent
  namespace: openshift-lifecycle-agent
spec:
  targetNamespaces:
    - openshift-lifecycle-agent
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: lifecycle-agent
  namespace: openshift-lifecycle-agent
spec:
  channel: stable
  name: lifecycle-agent
  source: redhat-operators
  sourceNamespace: openshift-marketplace
  installPlanApproval: Automatic
LCAEOF
    echo "Waiting for LCA operator to be ready..."
    for i in $(seq 1 60); do
        if oc get csv -n openshift-lifecycle-agent 2>/dev/null | grep -q Succeeded; then
            echo "LCA operator ready"
            break
        fi
        sleep 10
    done
fi

# Fix lca-cli bug: create authorized_keys.d directory that lca-cli expects but doesn't mkdir
echo "Creating authorized_keys.d directory on SNO (lca-cli bug workaround)..."
NODE=$(oc get nodes -o jsonpath='{.items[0].metadata.name}')
oc debug node/"$NODE" -- chroot /host mkdir -p /home/core/.ssh/authorized_keys.d 2>/dev/null || \
  oc debug node/"$NODE" -- chroot /host bash -c "mkdir -p /home/core/.ssh/authorized_keys.d && chown -R core:core /home/core/.ssh" 2>/dev/null || \
  echo "WARNING: Could not create authorized_keys.d — seed restore may fail"

# Create registry auth secret for pushing the seed image (LCA expects seedAuth key)
echo "Creating registry auth secret..."
oc delete secret seedgen -n openshift-lifecycle-agent 2>/dev/null || true
oc create secret generic seedgen \
    -n openshift-lifecycle-agent \
    --from-file=seedAuth="$SEED_AUTH_FILE"

# Create SeedGenerator CR
echo "Creating SeedGenerator CR..."
cat <<SGEOF | oc apply -f -
apiVersion: lca.openshift.io/v1
kind: SeedGenerator
metadata:
  name: seedimage
spec:
  seedImage: $SEED_IMAGE
SGEOF

echo ""
echo "Seed generation started. Monitor with:"
echo "  oc get seedgenerator seedimage -o yaml"
echo "  oc logs -n openshift-lifecycle-agent -l app.kubernetes.io/name=lifecycle-agent -f"
echo ""
echo "The node will reboot during seed capture. This takes ~15-20 minutes."
echo "When complete, the seed image will be pushed to: $SEED_IMAGE"

# Clean up registry push credentials
echo ""
echo "Cleaning up registry auth..."
rm -f "$SEED_AUTH_FILE"
rmdir "$(dirname "$SEED_AUTH_FILE")" 2>/dev/null || true
echo "Registry credentials removed from bastion"
