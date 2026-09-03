#!/usr/bin/env bash
#
# dev-tunnel.sh — expose the local Troshka backend to remote hosts.
#
# In dev, the backend runs on your laptop (localhost:8200) but ops pods and
# agnosticd callbacks run on a REMOTE host and need to reach the Troshka API
# via TROSHKA_API_URL (app.external_url). A laptop behind NAT isn't reachable,
# so this opens a public tunnel to :8200 and wires app.external_url to it.
#
# Usage:
#   ./scripts/dev-tunnel.sh start     # start tunnel + set external_url + restart
#   ./scripts/dev-tunnel.sh stop      # stop tunnel + restore external_url
#   ./scripts/dev-tunnel.sh status    # show tunnel + current external_url
#
# Requires cloudflared (recommended, zero-config) or ngrok:
#   brew install cloudflared        # macOS
#
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$REPO/src/backend/config/config.local.yaml"
PY="$REPO/src/backend/venv/bin/python3"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/troshka"
PIDFILE="$CACHE/dev-tunnel.pid"
LOG="/tmp/troshka-dev-tunnel.log"
PORT=8200
# Value restored on `stop` (the local frontend API-proxy — fine for local-only work).
LOCAL_URL="http://localhost:3100"

mkdir -p "$CACHE"

# --- config helpers (edit app.external_url in config.local.yaml, YAML-safe) ---
set_external_url() {
  "$PY" - "$CONFIG" "$1" <<'PY'
import sys, yaml
path, url = sys.argv[1], sys.argv[2]
try:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
except FileNotFoundError:
    data = {}
data.setdefault("app", {})["external_url"] = url
with open(path, "w") as f:
    yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
print(url)
PY
}

get_external_url() {
  "$PY" - "$CONFIG" <<'PY'
import sys, yaml
try:
    with open(sys.argv[1]) as f:
        print(((yaml.safe_load(f) or {}).get("app") or {}).get("external_url", ""))
except FileNotFoundError:
    print("")
PY
}

tunnel_running() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

restart_services() {
  echo "→ Restarting backend + worker so external_url takes effect…"
  "$REPO/dev-services.sh" restart backend
  "$REPO/dev-services.sh" restart worker
}

start() {
  if tunnel_running; then
    echo "Tunnel already running (pid $(cat "$PIDFILE")). Current external_url: $(get_external_url)"
    echo "Run '$0 stop' first to restart it."
    exit 0
  fi

  if command -v cloudflared >/dev/null 2>&1; then
    tool=cloudflared
  elif command -v ngrok >/dev/null 2>&1; then
    tool=ngrok
  else
    echo "ERROR: need cloudflared or ngrok."
    echo "  macOS:  brew install cloudflared"
    echo "  (or)    brew install ngrok  &&  ngrok config add-authtoken <token>"
    exit 1
  fi

  echo "→ Starting $tool tunnel to http://localhost:$PORT …"
  : > "$LOG"
  if [ "$tool" = cloudflared ]; then
    cloudflared tunnel --url "http://localhost:$PORT" >"$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    url=""
    for _ in $(seq 1 30); do
      url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)"
      [ -n "$url" ] && break
      sleep 1
    done
  else
    ngrok http "$PORT" --log stdout >"$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    url=""
    for _ in $(seq 1 30); do
      url="$(grep -oE 'https://[a-z0-9.-]+\.ngrok[a-z.-]*\.app|https://[a-z0-9.-]+\.ngrok\.io' "$LOG" | head -1 || true)"
      [ -n "$url" ] && break
      sleep 1
    done
  fi

  if [ -z "$url" ]; then
    echo "ERROR: could not determine tunnel URL. See $LOG"
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE"
    exit 1
  fi

  echo "→ Tunnel URL: $url"
  set_external_url "$url" >/dev/null
  echo "→ Set app.external_url = $url in config.local.yaml"
  restart_services
  echo ""
  echo "✅ Ops pods / agnosticd on remote hosts can now reach your API at:"
  echo "     $url/api/v1"
  echo "   Stop with: $0 stop   (logs: $LOG)"
}

stop() {
  if tunnel_running; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    echo "→ Stopped tunnel (pid $(cat "$PIDFILE"))."
  else
    echo "No tunnel running."
  fi
  rm -f "$PIDFILE"
  set_external_url "$LOCAL_URL" >/dev/null
  echo "→ Restored app.external_url = $LOCAL_URL"
  restart_services
}

status() {
  if tunnel_running; then
    echo "Tunnel:       RUNNING (pid $(cat "$PIDFILE"))"
  else
    echo "Tunnel:       stopped"
  fi
  echo "external_url: $(get_external_url)"
  echo "Log:          $LOG"
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "Usage: $0 {start|stop|status}"; exit 1 ;;
esac
