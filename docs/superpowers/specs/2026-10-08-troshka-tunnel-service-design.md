# Troshka Tunnel Service Design

**Date:** 2026-10-08  
**Status:** Approved (approach 1 — client → tunnel Route directly)

## Problem

`troshka-oc` opens a WebSocket API tunnel on `troshka-backend`. Each local `oc` TCP connection becomes a new in-process `kubevirt-pf` (or troshkad `/tcp-tunnel`) on the single uvicorn worker. When the nested API is slow/dead, discovery retries storm the backend; `/api/v1/health` misses the 1s liveness timeout and kubelet kills the API pod (exit 137).

## Goals

- Tunnel traffic never runs in the API process
- Scale to hundreds of concurrent users via a dedicated Deployment + HPA
- One multiplexed session per (user, project, cluster) from the laptop daemon
- Fast fail on dead nested APIs; hard caps per pod / user / project
- troshkad protocol unchanged; dial caller moves to tunnel pods

## Non-goals (v1)

- Showroom / oc-exec migration
- Agent-side concurrency limits in troshkad
- Changing nested cluster networking

## Architecture

```
laptop troshka-oc
  REST (list/kubeconfig) ──► troshka-api (backend)
  WSS (multiplexed streams) ──► troshka-tunnel Route
                                    │
                                    ▼
                              troshka-tunnel Deployment (N replicas, HPA)
                                    │
                    ┌───────────────┼────────────────┐
                    ▼               ▼                ▼
              kubevirt-pf     troshkad /tcp-tunnel   public Route
              (provider API)  (host agent)           (passthrough)
```

### Components

| Piece | Role |
|-------|------|
| `app/tunnel_main.py` (+ `app/tunnel/`) | Lean FastAPI: `/health`, multiplex WS tunnel |
| Same container image as backend | Different command / args |
| Helm `troshka-tunnel` Deployment/Service/Route/HPA | Isolated fleet |
| `scripts/troshka_oc.py` | Derive tunnel URL; header auth; multiplex |

### Auth

- Client sends `Authorization: Bearer trk_…` (not `?token=` — avoid access-log leakage)
- Tunnel pods use a **small** DB pool (e.g. 2+2) against the same Postgres
- On connect: validate unscoped API key + project owner/admin; **close DB session before dial/bridge**
- Dev mode: same auto-admin as backend when oauth disabled

### Multiplex protocol

One WebSocket session = one nested-cluster dial session.

1. Client opens `wss://…/api/v1/projects/{id}/clusters/{cid}/api-tunnel`
2. Server authenticates, resolves dial targets, opens **one** transport (e.g. one k8s `PortForward` object, or first successful dial path)
3. Server sends JSON text: `{"type":"ready","via":"…","tls_server_name":"…","force_insecure":bool,"multiplex":true}`
4. Client opens streams for each local TCP accept:
   - Text: `{"type":"open","stream_id":N}`
   - Server: `{"type":"opened","stream_id":N}` or `{"type":"error","stream_id":N,"error":"…"}`
   - Binary: `uint32_be stream_id || payload`
   - Text: `{"type":"close","stream_id":N}`

For kubevirt-pf: reuse the same `PortForward` and call `socket(port)` per stream.  
For troshkad: one `/tcp-tunnel` per stream (caps apply); future agent multiplex optional.

Legacy single-stream mode (no multiplex): still supported for one TCP bridge after `ready` without `open` messages (binary frames are raw TCP) — optional; v1 client always multiplexes.

### Caps & timeouts

| Limit | Default (configurable) |
|-------|-------------------------|
| Max concurrent WS sessions per pod | 100 |
| Max streams per session | 32 |
| Max sessions per user | 20 |
| Max sessions per project | 10 |
| Dial timeout | 15s |
| Session receive / ping interval | 60s (ping; session stays open with zero streams) |
| Nested connect timeout | 10s |

Exceeding caps → close with 1013 + clear reason.

### Helm / ops

- Route host: `route.host \| replace "troshka." "troshka-tunnel."`
- HPA: CPU 70%, min 2, max 20 (values-tunable)
- Probes: `/api/v1/health` only (no k8s dials)
- RBAC: same SA as backend (needs provider kubeconfig usage / portforward) **or** dedicated SA with equivalent rules
- Postgres `max_connections` formula includes `tunnel.replicas * tunnel.dbPoolSize`

### Backend

- Remove live bridge from `cluster_api_tunnel` (or return JSON/close `410` instructing clients to use tunnel URL)
- Keeps REST kubeconfig harvest unchanged

### Client (`troshka-oc`)

- `TROSHKA_TUNNEL_URL` optional; default derive:
  - `troshka-api.` → `troshka-tunnel.`
  - `localhost:8200` → `localhost:8201` (dev) or same process flag in local only
- REST stays on `TROSHKA_API_URL`
- Daemon: one multiplex WS per cluster; local listener fans streams
- Teardown closes the session

### troshkad impact

- No protocol change
- Caller becomes tunnel pods (mTLS client certs / agent token from DB as today)
- Load shape improves if clients multiplex (fewer sessions); still N netns relays per stream for troshkad path

## Success criteria

- API pod liveness unaffected by `oc` retry storms
- 100+ concurrent tunnel users sustainable via HPA (capacity test / load estimate)
- Dead nested API fails streams quickly without killing tunnel pods
- Docs + canvas Local oc panel point at tunnel URL derivation
