#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/src/backend"
FRONTEND_DIR="$SCRIPT_DIR/src/frontend"
DB_CONTAINER="troshka-postgres"
DB_PORT=5433
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
        podman run -d --name "$DB_CONTAINER" \
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
    uvicorn app.main:app --host 0.0.0.0 --port "$BACKEND_PORT" &>/tmp/troshka-backend.log &
    echo $! > "$PID_DIR/backend.pid"
    echo "  Backend:    started (port $BACKEND_PORT, PID $(cat "$PID_DIR/backend.pid"))"
}

stop_backend() {
    if [ -f "$PID_DIR/backend.pid" ]; then
        kill "$(cat "$PID_DIR/backend.pid")" 2>/dev/null || true
        rm -f "$PID_DIR/backend.pid"
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
    if [ -f "$PID_DIR/backend.pid" ] && kill -0 "$(cat "$PID_DIR/backend.pid")" 2>/dev/null; then
        echo "  Backend:    RUNNING (port $BACKEND_PORT)"
    else
        echo "  Backend:    STOPPED"
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
}

case "${1:-status}" in
    start)
        echo "=== Starting Troshka ==="
        start_db
        start_backend
        start_frontend
        echo ""
        echo "  Frontend:   http://localhost:$FRONTEND_PORT"
        echo "  Backend:    http://localhost:$BACKEND_PORT"
        echo "  API Docs:   http://localhost:$BACKEND_PORT/docs"
        ;;
    stop)
        echo "=== Stopping Troshka ==="
        stop_frontend
        stop_backend
        stop_db
        ;;
    restart)
        echo "=== Restarting Troshka ==="
        stop_frontend
        stop_backend
        stop_db
        start_db
        start_backend
        start_frontend
        echo ""
        echo "  Frontend:   http://localhost:$FRONTEND_PORT"
        echo "  Backend:    http://localhost:$BACKEND_PORT"
        ;;
    db)
        case "${2:-start}" in
            start) start_db ;;
            stop) stop_db ;;
        esac
        ;;
    backend)
        case "${2:-start}" in
            start) start_backend ;;
            stop) stop_backend ;;
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
        echo "Usage: $0 {start|stop|restart|status}"
        echo "       $0 {db|backend|frontend} {start|stop}"
        exit 1
        ;;
esac
