# Redis Job Queue & Horizontal Scaling

## Problem

Troshka's backend runs as a single pod with all deploy/destroy operations as
in-process daemon threads. At ~100 concurrent deploys the DB connection pool
exhausts, in-memory state can't be shared across replicas, and there's no
backpressure.

## Solution

Replace daemon threads with Redis Queue (RQ) workers for background operations.
Migrate all in-memory state (progress, cancellation, locks) to Redis. Add a
Redis pub/sub bridge so WebSocket notifications work across multiple backend
replicas.

## Architecture

```
                    ┌─────────────┐
     HTTP/WS ──────►  Backend(s)  │──── Redis pub/sub ───► Backend(s)
                    │  (stateless) │
                    └──────┬──────┘
                           │ enqueue
                    ┌──────▼──────┐
                    │    Redis    │ ◄── state, locks, pub/sub
                    └──────┬──────┘
                           │ dequeue
                    ┌──────▼──────┐
                    │  Worker(s)  │──── same codebase, different entrypoint
                    └─────────────┘
```

### Components

| Component | Image | Replicas | Purpose |
|-----------|-------|----------|---------|
| backend | troshka-backend | N | API + WebSocket (stateless) |
| worker | troshka-backend | M | RQ workers executing background jobs |
| redis | redis:7 | 1 | Job queue + shared state + pub/sub |

Workers use the same container image as the backend with a different entrypoint:
`python3 -m app.workers.deploy_worker`.

### Queues

| Queue | Operations | Timeout |
|-------|-----------|---------|
| `deploy` | deploy, destroy, stop, start, capture, bulk-deploy | 2h |
| `provision` | host provisioning, image builds, FSx polling | 2h |
| `default` | cache cleanup, misc background tasks | 1h |

### Graceful Degradation

When Redis is unavailable (dev, tests), all operations fall back to in-memory
equivalents — same behavior as before this change:
- `enqueue_job()` spawns a daemon thread
- State helpers use module-level dicts with threading.Lock
- Semaphore uses threading.Semaphore
- Distributed locks use threading.Lock
- Pub/sub delivers locally only

## State Migration

| Before (in-memory) | After (Redis) | Fallback |
|---------------------|---------------|----------|
| `_deploy_progress` dict | `progress:deploy:{id}` key | `_mem_progress` dict |
| `_deploy_cancelled` set | `cancelled:{id}` key | `_mem_cancelled` set |
| `_active_health_monitors` set | `deploy:health_monitors` set | `_mem_sets` dict |
| `_network_locks` dict | Redis `SET NX` lock | `threading.Lock` |
| `_deploy_semaphore` threading | Redis sorted set semaphore | `threading.Semaphore` |

## Thread → RQ Migration

### Fully migrated (module-level functions, ready for RQ workers)
- `deploy_project_async` — deploy/start a project
- `destroy_project_sync` — destroy a project
- `stop_project_async` — stop a project
- `start_project_async` — start a project
- `capture_pattern_disks` — capture pattern disk images
- `import_pattern_from_tar` — import pattern from tar
- `job_bulk_deploy_projects` — sequential bulk deploy
- `job_clean_pattern_cache` — clean host caches
- `job_provision_ocpvirt_host` — provision OCP Virt host
- `job_provision_kubevirt` — provision KubeVirt cluster
- `build_host_image` — Red Hat Image Builder
- `_poll_fsx_until_available` — FSx poller
- `_retry_pb_agent_install` — pattern buffer retry

### Remaining as threads (closure-based, deferred to follow-up)
- `_do_reconfigure` — 630-line closure, reconfigure deployed topology
- `_do_redeploy` — redeploy single VM
- `_start_infra_then_vm` — start infra + single VM
- `_cache_and_start` — cache images + start VM
- `_redeploy_bg` — full project redeploy

These closures capture simple values (project_id, host_id, vm_id) from their
enclosing route handlers. Converting them to module-level functions is
mechanical but large. They're also lower-frequency operations that don't need
to scale to hundreds of concurrent instances.

## Cross-Cluster Placement

The placement service already spreads load across hosts/clusters by selecting
the host with the most free RAM. `find_available_host` now accepts an optional
`provider_id` parameter for targeted placement, but defaults to searching all
providers — spreading load across kubevirt clusters naturally.

## Rate Limiting

Per-user rate limiting via Redis sliding window counters:
- Deploy concurrency: max 20 per user (configurable)
- Request rate: 100 requests/minute per user
- IP ban: 10 auth failures in 60s → 5-minute ban

## Files Changed

### New files
- `src/backend/app/core/redis.py` — Redis client + in-memory fallback
- `src/backend/app/workers/__init__.py`
- `src/backend/app/workers/deploy_worker.py` — RQ worker entrypoint
- `src/backend/app/workers/jobs.py` — standalone job functions
- `deploy/helm/templates/redis.yaml` — Redis deployment + service
- `deploy/helm/templates/worker-deployment.yaml` — Worker deployment

### Modified files
- `src/backend/app/services/deploy_service.py` — Redis state, distributed locks
- `src/backend/app/services/ws_pubsub.py` — Redis pub/sub bridge
- `src/backend/app/api/projects.py` — enqueue_job, Redis progress
- `src/backend/app/api/patterns.py` — enqueue_job
- `src/backend/app/api/providers.py` — enqueue_job
- `src/backend/app/core/rate_limit.py` — Redis-backed rate limiting
- `src/backend/app/main.py` — Redis init, enqueue for recovery
- `src/backend/app/services/placement.py` — provider_id filter
- `src/backend/pyproject.toml` — add `rq` dependency
- `src/backend/requirements.txt` — pin `rq==2.3.2`
- `deploy/helm/values.yaml` — redis, worker config
- `deploy/helm/templates/backend-config.yaml` — Redis URL
- `deploy/helm/templates/backend-deployment.yaml` — Redis URL env var

## Verification

1. All 298 existing tests pass (with Redis fallback)
2. Deploy 100+ projects via bulk deploy — jobs queued and processed by workers
3. Scale workers to N — deploys distributed across workers
4. Kill a worker mid-deploy — job re-queued by RQ
5. Scale backend to 3 replicas — WS updates work from any pod
