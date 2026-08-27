#!/usr/bin/env bash
# Build day-0 bootstrap ISOs for net-automation workshop routers (vrnetlab-compatible).
#
#   rtr1: iosxe_config.txt on ISO root (Cisco C8000v CVAC)
#   rtr3: config/juniper.conf on ISO (Juniper vSRX KVM bootstrap)
#
# Usage:
#   ./scripts/build-router-bootstrap-isos.sh [output-dir]
#   ./scripts/build-router-bootstrap-isos.sh --upload          # local Troshka library (dev)
#   ./scripts/build-router-bootstrap-isos.sh --upload-central  # central S4 + manifest.json
#   ./scripts/build-router-bootstrap-isos.sh --upload-central --sync-central
#
# Central S4 upload requires write credentials (not the s3_readonly provider):
#   export CENTRAL_S4_BUCKET=troshka-gold-images
#   export CENTRAL_S4_ENDPOINT=https://s4.example.com   # optional, for RGW/MinIO
#   export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BOOTSTRAP_DIR="$ROOT/example_templates/net-automation-workshop/bootstrap"
OUT_DIR="$ROOT/.generated/bootstrap-isos"
API_URL="${TROSHKA_API_URL:-http://localhost:8200}"
UPLOAD=false
UPLOAD_CENTRAL=false
SYNC_CENTRAL=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --upload) UPLOAD=true; shift ;;
        --upload-central) UPLOAD_CENTRAL=true; shift ;;
        --sync-central) SYNC_CENTRAL=true; shift ;;
        -h|--help)
            sed -n '1,20p' "$0"
            exit 0
            ;;
        *)
            OUT_DIR="$1"
            shift
            ;;
    esac
done

if ! command -v mkisofs >/dev/null 2>&1 && ! command -v genisoimage >/dev/null 2>&1 && ! command -v hdiutil >/dev/null 2>&1; then
    echo "Error: install mkisofs/genisoimage (brew install cdrtools) or use macOS hdiutil" >&2
    exit 1
fi

MKISO() {
    local out="$1"
    local src="$2"
    if command -v mkisofs >/dev/null 2>&1; then
        mkisofs -l -o "$out" "$src"
    elif command -v genisoimage >/dev/null 2>&1; then
        genisoimage -l -o "$out" "$src"
    else
        hdiutil makehybrid -o "$out" -hfs -joliet -iso -default-volume-name CONFIG "$src" >/dev/null
    fi
}

mkdir -p "$OUT_DIR"

build_rtr1_iso() {
    local staging="$OUT_DIR/.staging-rtr1"
    rm -rf "$staging"
    mkdir -p "$staging"
    cp "$BOOTSTRAP_DIR/rtr1-iosxe-config.txt" "$staging/iosxe_config.txt"
    MKISO "$OUT_DIR/net-automation-rtr1-bootstrap.iso" "$staging"
    rm -rf "$staging"
    echo "Built $OUT_DIR/net-automation-rtr1-bootstrap.iso"
}

build_rtr3_iso() {
    local staging
    staging="$(mktemp -d)"
    mkdir -p "$staging/config"
    cp "$BOOTSTRAP_DIR/rtr3-juniper.conf" "$staging/config/juniper.conf"
    MKISO "$OUT_DIR/net-automation-rtr3-bootstrap.iso" "$staging"
    rm -rf "$staging"
    echo "Built $OUT_DIR/net-automation-rtr3-bootstrap.iso"
}

upload_iso() {
    local name="$1"
    local file="$2"
    local item_id

    item_id="$("$ROOT/src/backend/venv/bin/python3" - <<'PY' "$API_URL" "$name"
import json
import sys
import urllib.request

api, name = sys.argv[1:3]
req = urllib.request.Request(
    f"{api}/api/v1/library/",
    data=json.dumps(
        {
            "name": name,
            "description": "Net automation workshop day-0 bootstrap (vrnetlab-compatible)",
            "type": "image",
            "format": "iso",
            "tags": ["net-automation", "bootstrap"],
        }
    ).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req) as resp:
        print(json.load(resp)["id"])
except urllib.error.HTTPError as e:
    if e.code != 409:
        raise
    with urllib.request.urlopen(f"{api}/api/v1/library/") as resp:
        items = json.load(resp)
    match = next((i["id"] for i in items if i["name"] == name), None)
    if not match:
        raise SystemExit(f"library item {name} exists but could not resolve id") from e
    print(match)
PY
)"

    curl -sf -X POST \
        -F "file=@${file}" \
        "${API_URL}/api/v1/library/${item_id}/upload-proxy" >/dev/null

    echo "Uploaded ${name} → local library item ${item_id}"
}

upload_central_isos() {
    "$ROOT/src/backend/venv/bin/python3" - <<'PY' "$OUT_DIR" "$SYNC_CENTRAL" "$API_URL"
import json
import os
import sys
import urllib.request
from pathlib import Path

import boto3
from botocore.config import Config

out_dir = Path(sys.argv[1])
sync_central = sys.argv[2].lower() == "true"
api_url = sys.argv[3]

bucket = os.environ.get("CENTRAL_S4_BUCKET", "troshka-gold-images")
endpoint = os.environ.get("CENTRAL_S4_ENDPOINT") or os.environ.get("AWS_ENDPOINT_URL")

items = [
    (
        "net-automation-rtr1-bootstrap",
        out_dir / "net-automation-rtr1-bootstrap.iso",
    ),
    (
        "net-automation-rtr3-bootstrap",
        out_dir / "net-automation-rtr3-bootstrap.iso",
    ),
]

for _name, path in items:
    if not path.is_file():
        raise SystemExit(f"missing ISO: {path}")

client_kw = {"region_name": os.environ.get("AWS_DEFAULT_REGION", "us-east-1")}
if endpoint:
    client_kw["endpoint_url"] = endpoint
client = boto3.client("s3", config=Config(signature_version="s3v4"), **client_kw)

manifest = []
try:
    resp = client.get_object(Bucket=bucket, Key="library/manifest.json")
    data = json.loads(resp["Body"].read())
    if isinstance(data, list):
        manifest = data
except Exception:
    pass

manifest = [
    e
    for e in manifest
    if e.get("name") not in {name for name, _ in items}
    and e.get("s3_key") not in {f"library/{name}.iso" for name, _ in items}
]

for name, path in items:
    s3_key = f"library/{name}.iso"
    size_bytes = path.stat().st_size
    print(f"Uploading {path.name} → s3://{bucket}/{s3_key} ({size_bytes} bytes)")
    client.upload_file(str(path), bucket, s3_key)
    manifest.append(
        {
            "s3_key": s3_key,
            "name": name,
            "type": "iso",
            "format": "iso",
            "size_bytes": size_bytes,
            "tags": ["net-automation", "bootstrap"],
        }
    )

manifest_body = json.dumps(manifest, indent=2) + "\n"
client.put_object(
    Bucket=bucket,
    Key="library/manifest.json",
    Body=manifest_body.encode(),
    ContentType="application/json",
)
print(f"Updated s3://{bucket}/library/manifest.json ({len(manifest)} entries)")

if sync_central:
    req = urllib.request.Request(
        f"{api_url}/api/v1/library/sync-central",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.load(resp)
        print("sync-central:", json.dumps(result))
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"sync-central failed ({e.code}): {body}", file=sys.stderr)
        print("Run Admin → Library → Sync Central manually, or POST /api/v1/library/sync-central", file=sys.stderr)
PY
}

build_rtr1_iso
build_rtr3_iso

if $UPLOAD; then
    upload_iso "net-automation-rtr1-bootstrap" "$OUT_DIR/net-automation-rtr1-bootstrap.iso"
    upload_iso "net-automation-rtr3-bootstrap" "$OUT_DIR/net-automation-rtr3-bootstrap.iso"
fi

if $UPLOAD_CENTRAL; then
    upload_central_isos
fi
