# Troshka Tunnel Service Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or implement tasks sequentially with TDD).

**Goal:** Move nested OCP API tunnels off `troshka-backend` onto a scalable `troshka-tunnel` Deployment; multiplex streams; keep troshkad dial semantics.

**Architecture:** `docs/superpowers/specs/2026-10-08-troshka-tunnel-service-design.md`

## File map

| Path | Responsibility |
|------|----------------|
| `src/backend/app/tunnel_main.py` | ASGI entry + lifespan |
| `src/backend/app/tunnel/session.py` | Multiplex session, caps, PF reuse |
| `src/backend/app/tunnel/auth.py` | API key + project ACL (short DB) |
| `src/backend/app/tunnel/ws.py` | WebSocket route |
| `src/backend/app/services/ocp/api_tunnel.py` | Shared dial helpers (unchanged core) |
| `src/backend/app/api/ws.py` | Deprecate old tunnel → 410 close |
| `scripts/troshka_oc.py` | Tunnel URL, header auth, multiplex |
| `deploy/helm/templates/tunnel-*.yaml` | Deploy/Svc/Route/HPA |
| `deploy/helm/values.yaml` | `tunnel.*` knobs |
| `docs/dev/troshka-oc.md` | Ops docs |
| `src/frontend/.../troshkaApiOrigin.ts` | Derive tunnel URL for canvas |

## Tasks

### Task 1: Multiplex framing + unit tests
### Task 2: Tunnel FastAPI app (health + auth + session bridge)
### Task 3: Caps / timeouts tests
### Task 4: `troshka-oc` client multiplex + URL derivation
### Task 5: Helm tunnel Deployment/Service/Route/HPA + DB pool in postgres formula
### Task 6: Backend deprecate old WS tunnel
### Task 7: Docs + canvas copy for tunnel URL
### Task 8: Integration smoke (local) + verify backend health under retry storm
