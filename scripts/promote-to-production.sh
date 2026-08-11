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
  troshka-bmc
  troshka-vnc-proxy
)

SOURCE_TAG="${1:-latest}"
TARGET_TAG="production"

echo "Promoting ${SOURCE_TAG} → ${TARGET_TAG} for ${#IMAGES[@]} images"
echo ""

PROMOTED=0
for img in "${IMAGES[@]}"; do
  SRC_DIGEST=$(skopeo inspect --no-tags "docker://${REGISTRY}/${img}:${SOURCE_TAG}" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('Digest',''))" 2>/dev/null || echo "")
  TGT_DIGEST=$(skopeo inspect --no-tags "docker://${REGISTRY}/${img}:${TARGET_TAG}" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('Digest',''))" 2>/dev/null || echo "")
  if [ "$SRC_DIGEST" = "$TGT_DIGEST" ] && [ -n "$SRC_DIGEST" ]; then
    echo "  ${img}: up to date"
    continue
  fi
  echo "  ${img}:${SOURCE_TAG} → :${TARGET_TAG}"
  skopeo copy --all \
    "docker://${REGISTRY}/${img}:${SOURCE_TAG}" \
    "docker://${REGISTRY}/${img}:${TARGET_TAG}"
  PROMOTED=$((PROMOTED + 1))
done
echo "  ${PROMOTED} image(s) promoted, $((${#IMAGES[@]} - PROMOTED)) already current"

echo ""
echo "Done. ArgoCD Image Updater handles infra01. Use the admin UI to update operators."
