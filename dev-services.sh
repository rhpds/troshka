#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/src/backend"
FRONTEND_DIR="$SCRIPT_DIR/src/frontend"
DB_CONTAINER="troshka-postgres"
REDIS_CONTAINER="troshka-redis"
DB_PORT=5433
REDIS_PORT=6379
DB_USER="troshka"
DB_PASS="troshka"
DB_NAME="troshka"
BACKEND_PORT=8200
FRONTEND_PORT=3100
PID_DIR="/tmp/troshka"

mkdir -p "$PID_DIR"

start_db() {
    if podman ps --format '{{.Names}}' 2>/dev/null | grep -q "^${DB_CONTAINER}$"; then
        echo "  PostgreSQL: already running (port $DB_PORT)"
        return
    fi
    if podman ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${DB_CONTAINER}$"; then
        podman start "$DB_CONTAINER"
    else
        podman volume create troshka-pgdata 2>/dev/null || true
        podman run -d --name "$DB_CONTAINER" \
            --restart=always \
            -v troshka-pgdata:/var/lib/postgresql/data \
            -e POSTGRES_USER="$DB_USER" \
            -e POSTGRES_PASSWORD="$DB_PASS" \
            -e POSTGRES_DB="$DB_NAME" \
            -p "${DB_PORT}:5432" \
            docker.io/library/postgres:16
    fi
    echo -n "  PostgreSQL: starting..."
    for i in $(seq 1 30); do
        if podman exec "$DB_CONTAINER" pg_isready -U "$DB_USER" &>/dev/null; then
            echo " ready (port $DB_PORT)"
            return
        fi
        sleep 1
    done
    echo " FAILED"
    exit 1
}

stop_db() {
    podman stop "$DB_CONTAINER" 2>/dev/null || true
    echo "  PostgreSQL: stopped"
}

start_redis() {
    if podman ps --format '{{.Names}}' 2>/dev/null | grep -q "^${REDIS_CONTAINER}$"; then
        echo "  Redis:      already running (port $REDIS_PORT)"
        return
    fi
    if podman ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${REDIS_CONTAINER}$"; then
        podman start "$REDIS_CONTAINER"
    else
        podman run -d --name "$REDIS_CONTAINER" \
            --restart=always \
            -p "${REDIS_PORT}:6379" \
            docker.io/library/redis:7
    fi
    echo -n "  Redis:      starting..."
    for i in $(seq 1 10); do
        if podman exec "$REDIS_CONTAINER" redis-cli ping &>/dev/null; then
            echo " ready (port $REDIS_PORT)"
            return
        fi
        sleep 1
    done
    echo " FAILED (backend will use in-memory fallback)"
}

stop_redis() {
    podman stop "$REDIS_CONTAINER" 2>/dev/null || true
    echo "  Redis:      stopped"
}

start_worker() {
    if [ -f "$PID_DIR/worker.pid" ] && kill -0 "$(cat "$PID_DIR/worker.pid")" 2>/dev/null; then
        echo "  Worker:     already running"
        return
    fi
    cd "$BACKEND_DIR"
    if [ ! -d "venv" ]; then
        echo "  Worker:     skipped (no venv — start backend first)"
        return
    fi
    source venv/bin/activate
    python3 -m app.workers.deploy_worker >>/tmp/troshka-worker.log 2>&1 &
    echo $! > "$PID_DIR/worker.pid"
    echo "  Worker:     started (PID $(cat "$PID_DIR/worker.pid"))"
}

stop_worker() {
    if [ -f "$PID_DIR/worker.pid" ]; then
        kill "$(cat "$PID_DIR/worker.pid")" 2>/dev/null || true
        rm -f "$PID_DIR/worker.pid"
    fi
    echo "  Worker:     stopped"
}

start_backend() {
    if [ -f "$PID_DIR/backend.pid" ] && kill -0 "$(cat "$PID_DIR/backend.pid")" 2>/dev/null; then
        echo "  Backend:    already running (port $BACKEND_PORT)"
        return
    fi
    cd "$BACKEND_DIR"
    if [ ! -d "venv" ]; then
        echo "  Backend:    creating venv..."
        python3 -m venv venv
        venv/bin/pip install -q -e ".[dev]"
    fi
    source venv/bin/activate
    alembic upgrade head 2>/dev/null || true
    uvicorn app.main:app --host 0.0.0.0 --port "$BACKEND_PORT" >>/tmp/troshka-backend.log 2>&1 &
    echo $! > "$PID_DIR/backend.pid"
    echo "  Backend:    started (port $BACKEND_PORT, PID $(cat "$PID_DIR/backend.pid"))"
}

check_backend_idle() {
    local pid
    if [ -f "$PID_DIR/backend.pid" ]; then
        pid="$(cat "$PID_DIR/backend.pid")"
        kill -0 "$pid" 2>/dev/null || return 0
    else
        return 0
    fi

    # Check for named background work threads via the debug endpoint
    local work_threads
    work_threads=$(curl -s "http://localhost:$BACKEND_PORT/api/v1/debug/threads" 2>/dev/null | \
        python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    idle = {'MainThread', 'health-poller', 'ws-state-poller', 'AnyIO worker thread'}
    # Only these thread prefixes should block a restart
    blocking = {'deploy-', 'reconfig-', 'redeploy-', 'start-', 'stop-', 'install-', 'reinstall-', 'pattern-', 'capture-'}
    work = [t['name'] for t in data.get('threads', []) if any(t['name'].startswith(p) for p in blocking)]
    if work:
        print(' '.join(work))
except:
    pass
" 2>/dev/null)

    if [ -n "$work_threads" ]; then
        echo "  Backend:    WARNING — active: $work_threads"
        return 1
    fi
    return 0
}

stop_backend() {
    local force="${1:-}"
    if [ -f "$PID_DIR/backend.pid" ] && kill -0 "$(cat "$PID_DIR/backend.pid")" 2>/dev/null; then
        if ! check_backend_idle 2>/dev/null; then
            echo "  Backend:    in-flight work detected — will resume after restart"
        fi
        kill "$(cat "$PID_DIR/backend.pid")" 2>/dev/null || true
    fi
    rm -f "$PID_DIR/backend.pid"
    # Kill any orphaned uvicorn processes listening on the backend port
    local orphans
    orphans=$(lsof -ti :"$BACKEND_PORT" -sTCP:LISTEN 2>/dev/null || true)
    if [ -n "$orphans" ]; then
        echo "$orphans" | xargs kill 2>/dev/null || true
        sleep 1
    fi
    echo "  Backend:    stopped"
}

start_frontend() {
    if [ -f "$PID_DIR/frontend.pid" ] && kill -0 "$(cat "$PID_DIR/frontend.pid")" 2>/dev/null; then
        echo "  Frontend:   already running (port $FRONTEND_PORT)"
        return
    fi
    cd "$FRONTEND_DIR"
    if [ ! -d "node_modules" ]; then
        echo "  Frontend:   installing dependencies..."
        npm install --silent
    fi
    npm run dev &>/tmp/troshka-frontend.log &
    echo $! > "$PID_DIR/frontend.pid"
    echo "  Frontend:   started (port $FRONTEND_PORT, PID $(cat "$PID_DIR/frontend.pid"))"
}

stop_frontend() {
    if [ -f "$PID_DIR/frontend.pid" ]; then
        kill "$(cat "$PID_DIR/frontend.pid")" 2>/dev/null || true
        rm -f "$PID_DIR/frontend.pid"
    fi
    pkill -f "next dev" 2>/dev/null || true
    echo "  Frontend:   stopped"
}

status() {
    echo "=== Troshka Dev Services ==="
    if podman ps --format '{{.Names}}' 2>/dev/null | grep -q "^${DB_CONTAINER}$"; then
        echo "  PostgreSQL: RUNNING (port $DB_PORT)"
    else
        echo "  PostgreSQL: STOPPED"
    fi
    if podman ps --format '{{.Names}}' 2>/dev/null | grep -q "^${REDIS_CONTAINER}$"; then
        echo "  Redis:      RUNNING (port $REDIS_PORT)"
    else
        echo "  Redis:      STOPPED (backend uses in-memory fallback)"
    fi
    if [ -f "$PID_DIR/backend.pid" ] && kill -0 "$(cat "$PID_DIR/backend.pid")" 2>/dev/null; then
        echo "  Backend:    RUNNING (port $BACKEND_PORT)"
    else
        echo "  Backend:    STOPPED"
    fi
    if [ -f "$PID_DIR/worker.pid" ] && kill -0 "$(cat "$PID_DIR/worker.pid")" 2>/dev/null; then
        echo "  Worker:     RUNNING (PID $(cat "$PID_DIR/worker.pid"))"
    else
        echo "  Worker:     STOPPED (backend runs jobs in-process)"
    fi
    if [ -f "$PID_DIR/frontend.pid" ] && kill -0 "$(cat "$PID_DIR/frontend.pid")" 2>/dev/null; then
        echo "  Frontend:   RUNNING (port $FRONTEND_PORT)"
    else
        echo "  Frontend:   STOPPED"
    fi
    echo ""
    echo "  Frontend:   http://localhost:$FRONTEND_PORT"
    echo "  Backend:    http://localhost:$BACKEND_PORT"
    echo "  API Docs:   http://localhost:$BACKEND_PORT/docs"
    echo "  Queue:      http://localhost:$BACKEND_PORT/api/v1/admin/queue-status"
}

case "${1:-status}" in
    start)
        echo "=== Starting Troshka ==="
        start_db
        start_redis
        start_backend
        start_worker
        start_frontend
        echo ""
        echo "  Frontend:   http://localhost:$FRONTEND_PORT"
        echo "  Backend:    http://localhost:$BACKEND_PORT"
        echo "  API Docs:   http://localhost:$BACKEND_PORT/docs"
        ;;
    stop)
        case "${2:-all}" in
            backend) echo "=== Stopping Backend ==="; stop_backend "${3:-}" ;;
            frontend) echo "=== Stopping Frontend ==="; stop_frontend ;;
            worker) echo "=== Stopping Worker ==="; stop_worker ;;
            redis) echo "=== Stopping Redis ==="; stop_redis ;;
            db) echo "=== Stopping PostgreSQL ==="; stop_db ;;
            all)
                echo "=== Stopping Troshka ==="
                stop_frontend
                stop_worker
                stop_backend "${3:-}"
                stop_redis
                stop_db
                ;;
            *) echo "Usage: $0 stop [backend|frontend|worker|redis|db]"; exit 1 ;;
        esac
        ;;
    restart)
        case "${2:-all}" in
            backend)
                FORCE="${3:-}"
                echo "=== Restarting Backend ==="
                stop_backend "$FORCE"
                start_backend
                echo ""
                echo "  Backend:    http://localhost:$BACKEND_PORT"
                ;;
            frontend)
                echo "=== Restarting Frontend ==="
                stop_frontend
                start_frontend
                echo ""
                echo "  Frontend:   http://localhost:$FRONTEND_PORT"
                ;;
            worker)
                echo "=== Restarting Worker ==="
                stop_worker
                start_worker
                ;;
            all|--force)
                FORCE=""
                [ "${2:-}" = "--force" ] && FORCE="--force"
                [ "${3:-}" = "--force" ] && FORCE="--force"
                echo "=== Restarting Troshka ==="
                stop_frontend
                stop_worker
                stop_backend "$FORCE"
                stop_redis
                stop_db
                start_db
                start_redis
                start_backend
                start_worker
                start_frontend
                echo ""
                echo "  Frontend:   http://localhost:$FRONTEND_PORT"
                echo "  Backend:    http://localhost:$BACKEND_PORT"
                ;;
        esac
        ;;
    db)
        case "${2:-start}" in
            start) start_db ;;
            stop) stop_db ;;
        esac
        ;;
    backend)
        FORCE="${3:-}"
        case "${2:-start}" in
            start) start_backend ;;
            stop) stop_backend "$FORCE" ;;
            restart)
                stop_backend "$FORCE"
                start_backend
                ;;
        esac
        ;;
    frontend)
        case "${2:-start}" in
            start) start_frontend ;;
            stop) stop_frontend ;;
        esac
        ;;
    status) status ;;
    *)
        echo "Usage: $0 {start|stop|restart [backend|frontend|worker|redis] [--force]|status}"
        echo "       $0 backend {start|stop|restart} [--force]"
        echo "       $0 {db|redis|frontend|worker} {start|stop}"
        exit 1
        ;;
esac
