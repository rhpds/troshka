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
PID_DIR="${HOME}/.cache/troshka"
LIFECYCLE_LOG="${PID_DIR}/lifecycle.log"
BACKEND_EXIT_LOG="${PID_DIR}/backend-exit.log"
BACKEND_LOG="${SCRIPT_DIR}/.dev/backend.log"

mkdir -p "$PID_DIR"

lifecycle_log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') dev-services pid=$$ $*" >> "$LIFECYCLE_LOG"
}

needs_podman_machine() {
    [[ "$(uname -s)" == "Darwin" ]] || return 1
    podman machine list &>/dev/null
}

ensure_podman() {
    if ! command -v podman &>/dev/null; then
        echo "  Podman:     not found in PATH"
        exit 1
    fi

    if podman ps &>/dev/null; then
        return 0
    fi

    local started=false
    if needs_podman_machine; then
        local running=""
        running="$(podman machine list --format '{{.Running}}' 2>/dev/null | head -1 || true)"
        if [[ "$running" != "true" ]]; then
            echo -n "  Podman:     starting machine..."
            if ! podman machine start &>/dev/null; then
                echo " FAILED"
                echo "  Hint: try 'podman machine init' then 'podman machine start'"
                exit 1
            fi
            started=true
        fi
    fi

    if ! podman ps &>/dev/null; then
        [[ "$started" == false ]] && echo -n "  Podman:     waiting..."
        for _ in $(seq 1 60); do
            if podman ps &>/dev/null; then
                echo " ready"
                return 0
            fi
            sleep 1
        done
        echo " FAILED"
        if needs_podman_machine; then
            echo "  Hint: run 'podman machine start' or recreate with 'podman machine init'"
        fi
        exit 1
    fi

    if [[ "$started" == true ]]; then
        echo " ready"
    fi
}

podman_container_running() {
    [ -n "$(podman ps -q --filter "name=^${1}$" 2>/dev/null)" ]
}

podman_container_exists() {
    podman container exists "$1" &>/dev/null
}

start_db() {
    ensure_podman
    if podman_container_running "$DB_CONTAINER"; then
        echo "  PostgreSQL: already running (port $DB_PORT)"
        return
    fi
    if podman_container_exists "$DB_CONTAINER"; then
        podman start "$DB_CONTAINER"
    else
        podman volume create troshka-pgdata 2>/dev/null || true
        if ! podman run -d --name "$DB_CONTAINER" \
            --restart=always \
            -v troshka-pgdata:/var/lib/postgresql/data \
            -e POSTGRES_USER="$DB_USER" \
            -e POSTGRES_PASSWORD="$DB_PASS" \
            -e POSTGRES_DB="$DB_NAME" \
            -p "${DB_PORT}:5432" \
            docker.io/library/postgres:16; then
            if podman_container_exists "$DB_CONTAINER"; then
                podman start "$DB_CONTAINER"
            else
                echo "  PostgreSQL: FAILED to create container"
                exit 1
            fi
        fi
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
    ensure_podman
    if podman_container_running "$REDIS_CONTAINER"; then
        echo "  Redis:      already running (port $REDIS_PORT)"
        return
    fi
    if podman_container_exists "$REDIS_CONTAINER"; then
        podman start "$REDIS_CONTAINER"
    else
        if ! podman run -d --name "$REDIS_CONTAINER" \
            --restart=always \
            -p "${REDIS_PORT}:6379" \
            docker.io/library/redis:7; then
            if podman_container_exists "$REDIS_CONTAINER"; then
                podman start "$REDIS_CONTAINER"
            else
                echo "  Redis:      FAILED to create container"
                return
            fi
        fi
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

# macOS SimpleWorker runs jobs in-process; multiple workers race on the same
# queues and duplicate deploy jobs. Linux uses forked Worker children safely.
if [[ "$(uname -s)" == "Darwin" ]]; then
    WORKER_COUNT=1
else
    WORKER_COUNT=3
fi
WORKER_LOG="/tmp/troshka-worker.log"

_cleanup_stale_rq_workers() {
    cd "$BACKEND_DIR" || return 0
    [ -d "venv" ] || return 0
    ./venv/bin/python3 - <<'PY' 2>/dev/null || true
from app.core.redis import get_redis_raw
r = get_redis_raw()
dead = []
for w in r.smembers("rq:workers"):
    ws = w.decode() if isinstance(w, bytes) else w
    if not r.exists(f"{ws}:heartbeat"):
        dead.append(w)
for w in dead:
    r.srem("rq:workers", w)
    r.delete(f"{w}:heartbeat")
if dead:
    print(f"  Worker:     pruned {len(dead)} stale RQ worker registration(s)")
PY
}

start_worker() {
    cd "$BACKEND_DIR"
    if [ ! -d "venv" ]; then
        echo "  Worker:     skipped (no venv — start backend first)"
        return
    fi
    _cleanup_stale_rq_workers
    local started=0
    for i in $(seq 1 "$WORKER_COUNT"); do
        local pidfile="$PID_DIR/worker-${i}.pid"
        local supervisor_pidfile="$PID_DIR/worker-${i}-supervisor.pid"
        if [ -f "$supervisor_pidfile" ] && kill -0 "$(cat "$supervisor_pidfile")" 2>/dev/null; then
            continue
        fi
        rm -f "$pidfile" "$supervisor_pidfile"
        python3 "$SCRIPT_DIR/scripts/supervise-worker.py" \
            --backend-dir "$BACKEND_DIR" \
            --worker-pidfile "$pidfile" \
            --supervisor-pidfile "$supervisor_pidfile" \
            --log "$WORKER_LOG"
        started=$((started + 1))
    done
    local running=0
    sleep 0.5
    for i in $(seq 1 "$WORKER_COUNT"); do
        local supervisor_pidfile="$PID_DIR/worker-${i}-supervisor.pid"
        [ -f "$supervisor_pidfile" ] && kill -0 "$(cat "$supervisor_pidfile")" 2>/dev/null && running=$((running + 1))
    done
    if [ "$started" -gt 0 ]; then
        echo "  Worker:     started $started ($running total)"
    else
        echo "  Worker:     $running already running"
    fi
}

stop_worker() {
    for i in $(seq 1 "$WORKER_COUNT"); do
        local pidfile="$PID_DIR/worker-${i}.pid"
        local supervisor_pidfile="$PID_DIR/worker-${i}-supervisor.pid"
        if [ -f "$supervisor_pidfile" ]; then
            kill "$(cat "$supervisor_pidfile")" 2>/dev/null || true
            rm -f "$supervisor_pidfile"
        fi
        if [ -f "$pidfile" ]; then
            kill "$(cat "$pidfile")" 2>/dev/null || true
            rm -f "$pidfile"
        fi
    done
    echo "  Worker:     stopped"
}

# PIDs of the backend server on this port — the uvicorn process (matched by
# command, which reliably catches stale orphans the PID file no longer tracks)
# and anything LISTENing on the port. Deliberately does NOT do a plain
# `lsof -ti tcp:PORT`: that also returns *clients* connected to the port (e.g.
# the frontend's keep-alive to the backend), which we must never kill.
backend_pids() {
    {
        pgrep -f "uvicorn app.main:app.*--port ${BACKEND_PORT}" 2>/dev/null || true
        lsof -ti "tcp:${BACKEND_PORT}" -sTCP:LISTEN 2>/dev/null || true
    } | sort -u
}

backend_port_in_use() {
    [ -n "$(backend_pids)" ]
}

start_backend() {
    # Never start a duplicate: if anything already holds the port, leave it alone.
    if backend_port_in_use; then
        echo "  Backend:    already running on port $BACKEND_PORT (PID(s): $(backend_pids | tr '\n' ' '))"
        echo "              use '$0 restart backend' to replace it"
        return
    fi
    cd "$BACKEND_DIR"
    if [ ! -d "venv" ]; then
        echo "  Backend:    creating venv..."
        python3 -m venv venv
        venv/bin/pip install -q -e ".[dev]"
    fi
    source venv/bin/activate
    # macOS: avoid fork-safety crashes when background threads + subprocess/ssl coexist
    export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
    alembic upgrade head 2>/dev/null || true
    mkdir -p "$(dirname "$BACKEND_LOG")"
    rm -f "$PID_DIR/backend.pid" "$PID_DIR/backend-supervisor.pid"
    lifecycle_log "start_backend spawning supervised uvicorn on port $BACKEND_PORT log=$BACKEND_LOG"
    # posix_spawn(setsid) inside the supervisor — must NOT background this in the
    # Agent shell PGID (that PGID is SIGKILL'd when the tool command returns).
    python3 "$SCRIPT_DIR/scripts/supervise-backend.py" \
        --port "$BACKEND_PORT" \
        --backend-dir "$BACKEND_DIR" \
        --pidfile "$PID_DIR/backend.pid" \
        --supervisor-pidfile "$PID_DIR/backend-supervisor.pid" \
        --log "$BACKEND_LOG"
    local pid=""
    local i
    for i in $(seq 1 20); do
        if [ -f "$PID_DIR/backend.pid" ]; then
            pid="$(cat "$PID_DIR/backend.pid")"
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                break
            fi
        fi
        sleep 0.25
    done
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        lifecycle_log "start_backend FAILED uvicorn did not spawn"
        echo "  Backend:    FAILED to start — supervisor/uvicorn did not spawn (see $BACKEND_LOG, $BACKEND_EXIT_LOG, ${HOME}/.cache/troshka/backend-crash.log)"
        rm -f "$PID_DIR/backend.pid"
        return 1
    fi
    lifecycle_log "start_backend spawned uvicorn pid=$pid supervisor=$(cat "$PID_DIR/backend-supervisor.pid" 2>/dev/null || echo '?')"
    # Catch an immediate failure (crash / bind conflict). Slow startup is fine —
    # the process stays alive and binds the port once lifespan startup completes.
    sleep 3
    if ! kill -0 "$pid" 2>/dev/null; then
        lifecycle_log "start_backend FAILED pid=$pid exited immediately"
        echo "  Backend:    FAILED to start — process exited (see $BACKEND_LOG, $BACKEND_EXIT_LOG, ${HOME}/.cache/troshka/backend-crash.log)"
        rm -f "$PID_DIR/backend.pid"
        return 1
    fi
    for _ in $(seq 1 30); do
        if curl -sf --max-time 2 "http://localhost:$BACKEND_PORT/api/v1/auth/me" >/dev/null 2>&1; then
            lifecycle_log "start_backend ready pid=$pid"
            echo "  Backend:    started (port $BACKEND_PORT, PID $pid)"
            return
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            lifecycle_log "start_backend FAILED pid=$pid died during warmup"
            rm -f "$PID_DIR/backend.pid"
            echo "  Backend:    FAILED during warmup (see $BACKEND_LOG, $BACKEND_EXIT_LOG, ${HOME}/.cache/troshka/backend-crash.log)"
            return 1
        fi
        sleep 2
    done
    lifecycle_log "start_backend slow pid=$pid still warming"
    echo "  Backend:    started (port $BACKEND_PORT, PID $pid) — still warming up"
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
    lifecycle_log "stop_backend begin force=${force} pids=$(backend_pids | tr '\n' ' ')"
    if [ -n "$(backend_pids)" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') dev-services intentional stop_backend pids=$(backend_pids | tr '\n' ' ')" >> "$BACKEND_EXIT_LOG"
    fi
    if [ -f "$PID_DIR/backend.pid" ] && kill -0 "$(cat "$PID_DIR/backend.pid")" 2>/dev/null; then
        if ! check_backend_idle 2>/dev/null; then
            echo "  Backend:    in-flight work detected — will resume after restart"
        fi
    fi
    rm -f "$PID_DIR/backend.pid"
    # Kill uvicorn first so the supervisor's waitid() records the reason.
    # Then reap the supervisor if it is still around.
    pkill -f "$SCRIPT_DIR/scripts/supervise-backend.py" 2>/dev/null || true
    rm -f "$PID_DIR/backend-supervisor.pid"
    # Kill every process on the backend port — tracked PID, stale orphans, and
    # zombies alike (matched by port AND by uvicorn command). Graceful first,
    # then force, waiting until the port is actually free.
    local pids
    pids="$(backend_pids)"
    if [ -n "$pids" ]; then
        echo "$pids" | xargs kill 2>/dev/null || true
        for _ in $(seq 1 10); do
            backend_port_in_use || break
            sleep 1
        done
        pids="$(backend_pids)"
        if [ -n "$pids" ]; then
            lifecycle_log "stop_backend kill -9 pids=$(echo "$pids" | tr '\n' ' ')"
            echo "$pids" | xargs kill -9 2>/dev/null || true
            sleep 1
        fi
    fi
    lifecycle_log "stop_backend done"
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
    if needs_podman_machine; then
        local running=""
        running="$(podman machine list --format '{{.Running}}' 2>/dev/null | head -1 || true)"
        if [[ "$running" == "true" ]]; then
            echo "  Podman:     machine RUNNING"
        else
            echo "  Podman:     machine STOPPED (containers unavailable until started)"
        fi
    fi
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
    local running_workers=0
    for i in $(seq 1 "$WORKER_COUNT"); do
        if [ -f "$PID_DIR/worker-${i}-supervisor.pid" ] && kill -0 "$(cat "$PID_DIR/worker-${i}-supervisor.pid")" 2>/dev/null; then
            running_workers=$((running_workers + 1))
        fi
    done
    if [ "$running_workers" -gt 0 ]; then
        echo "  Worker:     RUNNING ($running_workers of $WORKER_COUNT)"
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
