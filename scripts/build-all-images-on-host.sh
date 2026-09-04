#!/usr/bin/env bash
#
# Build ALL troshka container images natively on a Troshka host (amd64) and push
# them, using build-image-on-host.sh. This is the local/manual counterpart to the
# CI build-images workflow (.github/workflows/build-images.yml) — use it when you
# need images built without waiting on CI, and want to avoid slow/OOM cross-arch
# emulation on a local arm64 podman machine.
#
# Usage:
#   scripts/build-all-images-on-host.sh --host <id> [--registry <r>] [--tag <t>] \
#       [--no-push] [name ...]
#
#   --host <id>       host id prefix (see scripts/host-ssh.sh --list)   [required]
#   --registry <r>    registry/org prefix   [default quay.io/redhat-gpte]
#   --tag <t>         image tag             [default latest]
#   --no-push         build only, do not push
#   name ...          build only these images (short name ok, e.g. "terminal
#                     backend"); default builds all
#
# Examples:
#   scripts/build-all-images-on-host.sh --host c73f2e79            # all, push
#   scripts/build-all-images-on-host.sh --host c73f2e79 terminal   # just terminal
#   scripts/build-all-images-on-host.sh --host c73f2e79 --no-push backend frontend
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD="$SCRIPT_DIR/build-image-on-host.sh"

# name ; context ; containerfile (blank = Dockerfile in context) ; build-arg (blank = none)
IMAGES=(
    "troshka-backend;.;deploy/containerfiles/Containerfile.backend;"
    "troshka-frontend;.;deploy/containerfiles/Containerfile.frontend;"
    "troshka-operator;src/operator;;"
    "troshka-dnsmasq;src/operator/images/dnsmasq;;"
    "troshka-gateway;src/operator/images/gateway;;"
    "troshka-tools;src/operator/images/troshka-tools;;"
    "troshka-bmc;src/operator/images/bmc;;"
    "troshka-vnc-proxy;src/operator/images/vnc-proxy;;"
    "troshka-ops-pod;src/operator/images/ops-pod;;OCP_VERSION=stable"
    "troshka-terminal;src/operator/images/terminal;;OCP_VERSION=stable"
)

HOST=""
REGISTRY="quay.io/redhat-gpte"
TAG="latest"
PUSH=true
ONLY=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --registry) REGISTRY="$2"; shift 2 ;;
        --tag) TAG="$2"; shift 2 ;;
        --no-push) PUSH=false; shift ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        -*) echo "Unknown argument: $1" >&2; exit 1 ;;
        *) ONLY+=("$1"); shift ;;
    esac
done

if [[ -z "$HOST" ]]; then
    echo "Usage: build-all-images-on-host.sh --host <id> [--registry <r>] [--tag <t>] [--no-push] [name ...]" >&2
    exit 1
fi

# Return 0 if this image should be built (no filter, or name matches a filter).
_selected() {
    local name="$1" short="${1#troshka-}" f
    [[ ${#ONLY[@]} -eq 0 ]] && return 0
    for f in "${ONLY[@]}"; do
        [[ "$f" == "$name" || "$f" == "$short" ]] && return 0
    done
    return 1
}

cd "$REPO_ROOT"
built=() ; skipped=()
for entry in "${IMAGES[@]}"; do
    IFS=';' read -r name context file buildarg <<< "$entry"
    if ! _selected "$name"; then
        skipped+=("$name"); continue
    fi
    args=(--host "$HOST" --context "$context" --tag "$REGISTRY/$name:$TAG")
    [[ -n "$file" ]] && args+=(--file "$file")
    [[ -n "$buildarg" ]] && args+=(--build-arg "$buildarg")
    [[ "$PUSH" == "true" ]] && args+=(--push)
    echo ""
    echo "==================================================================="
    echo "  Building $REGISTRY/$name:$TAG   (context: $context)"
    echo "==================================================================="
    "$BUILD" "${args[@]}"
    built+=("$name")
done

echo ""
echo "=== Done. Built: ${built[*]:-none}${skipped:+  |  Skipped: ${skipped[*]}} ==="
