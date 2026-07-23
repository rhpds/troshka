#!/usr/bin/env bash
set -euo pipefail

REGISTRY="quay.io/redhat-gpte"
IMAGES=(
  troshka-backend
  troshka-frontend
  troshka-operator
  troshka-dnsmasq
  troshka-gateway
  troshka-tools
  troshka-sushy
  troshka-vnc-proxy
)

SOURCE_TAG="${1:-latest}"
TARGET_TAG="production"

echo "Promoting ${SOURCE_TAG} → ${TARGET_TAG} for ${#IMAGES[@]} images"
echo ""

for img in "${IMAGES[@]}"; do
  echo "  ${img}:${SOURCE_TAG} → :${TARGET_TAG}"
  skopeo copy --all \
    "docker://${REGISTRY}/${img}:${SOURCE_TAG}" \
    "docker://${REGISTRY}/${img}:${TARGET_TAG}"
done

echo ""
echo "Done. ArgoCD Image Updater handles infra01. Use the admin UI to update operators."
