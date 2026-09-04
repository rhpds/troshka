#!/usr/bin/env bash
#
# Build a container image NATIVELY on a Troshka host (amd64), avoiding slow and
# OOM-prone cross-arch emulation on a local (Apple-silicon / arm64) podman
# machine. The build context is streamed to the host over SSH, built with
# `sudo podman build`, and optionally pushed to a registry using the caller's
# LOCAL registry auth (streamed to the host and removed afterwards).
#
# Why: images like troshka-terminal bake native node modules (wetty/node-pty)
# that must be compiled for glibc/amd64. Emulating that on a 2 GiB arm64 podman
# machine OOM-kills the build; the amd64 hosts have ample CPU/RAM and build it
# in seconds. This is the local/manual counterpart to the CI build-images
# workflow (.github/workflows/build-images.yml).
#
# Usage:
#   scripts/build-image-on-host.sh --host <id> --context <dir> --tag <image:tag> \
#       [--push] [--build-arg KEY=VALUE]...
#
# Examples:
#   # build + push the cluster terminal image on host c73f2e79
#   scripts/build-image-on-host.sh \
#       --host c73f2e79 \
#       --context src/operator/images/terminal \
#       --tag quay.io/redhat-gpte/troshka-terminal:latest \
#       --build-arg OCP_VERSION=stable --push
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOST_SSH="$SCRIPT_DIR/host-ssh.sh"
AUTHFILE="${REGISTRY_AUTH_FILE:-$HOME/.config/containers/auth.json}"

HOST=""
CONTEXT=""
TAG=""
PUSH=false
BUILD_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --context) CONTEXT="$2"; shift 2 ;;
        --tag) TAG="$2"; shift 2 ;;
        --push) PUSH=true; shift ;;
        --build-arg) BUILD_ARGS+=(--build-arg "$2"); shift 2 ;;
        -h|--help)
            sed -n '2,32p' "$0"; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$HOST" || -z "$CONTEXT" || -z "$TAG" ]]; then
    echo "Usage: build-image-on-host.sh --host <id> --context <dir> --tag <image:tag> [--push] [--build-arg K=V]..." >&2
    exit 1
fi
if [[ ! -d "$CONTEXT" ]]; then
    echo "Error: context dir not found: $CONTEXT" >&2
    exit 1
fi
if [[ ! -f "$CONTEXT/Dockerfile" && ! -f "$CONTEXT/Containerfile" ]]; then
    echo "Error: no Dockerfile/Containerfile in $CONTEXT" >&2
    exit 1
fi

RDIR="/tmp/troshka-build-$(date +%s)-$$"
echo ">> Streaming build context ($CONTEXT) to host $HOST:$RDIR"
tar czf - -C "$CONTEXT" . | "$HOST_SSH" "$HOST" "mkdir -p '$RDIR' && tar xzf - -C '$RDIR'"

# shellcheck disable=SC2016 - $RDIR/$TAG are expanded locally into the remote cmd on purpose
BUILD_ARG_STR=""
for a in "${BUILD_ARGS[@]}"; do BUILD_ARG_STR+=" $a"; done

echo ">> Building $TAG natively on $HOST"
"$HOST_SSH" "$HOST" "cd '$RDIR' && sudo podman build${BUILD_ARG_STR} -t '$TAG' . && sudo podman image inspect '$TAG' --format 'built {{.Id}} ({{.Architecture}})'"

if [[ "$PUSH" == "true" ]]; then
    if [[ ! -f "$AUTHFILE" ]]; then
        echo "Error: registry auth file not found: $AUTHFILE (run 'podman login' or set REGISTRY_AUTH_FILE)" >&2
        exit 1
    fi
    echo ">> Pushing $TAG (auth streamed from $AUTHFILE, removed after)"
    # Stream auth over stdin so the token never appears in argv/process list.
    base64 < "$AUTHFILE" | "$HOST_SSH" "$HOST" \
        "cat > '$RDIR/.auth.b64' && base64 -d '$RDIR/.auth.b64' > '$RDIR/.auth.json' && rm -f '$RDIR/.auth.b64' && sudo podman push --authfile '$RDIR/.auth.json' '$TAG'; rm -f '$RDIR/.auth.json'"
fi

echo ">> Cleaning up $HOST:$RDIR"
"$HOST_SSH" "$HOST" "rm -rf '$RDIR'"
echo ">> Done: $TAG"
