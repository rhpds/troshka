# Local `oc` via `troshka-oc`

Use stock `oc` on your laptop against nested OpenShift clusters in a Troshka project you own. The helper opens an authenticated tunnel through the dedicated **troshka-tunnel** service so you do not need in-cluster DNS, a bastion, or a manually rewritten kubeconfig — and so tunnel traffic cannot stall the Troshka API worker.

## Prerequisites

- `oc` on your `PATH`
- Repo checkout (`scripts/troshka-oc`)
- A Troshka project **you own** (or admin) with at least one nested cluster that has finished install (harvested kubeconfig)
- API + tunnel reachability + auth (see [API access](#api-access) below)

## Quick start

```bash
# REST API (kubeconfig list/fetch) — NOT the UI host
export TROSHKA_API_URL=https://troshka-api.apps.example.com
# Tunnel WebSocket service (derived automatically if unset)
export TROSHKA_TUNNEL_URL=https://troshka-tunnel.apps.example.com
export TROSHKA_API_KEY=trk_…   # from Settings → API Keys

# Local dev defaults: API :8200, tunnel :8201
# export TROSHKA_API_URL=http://localhost:8200
# export TROSHKA_TUNNEL_URL=http://localhost:8201

# List nested clusters + whether a kubeconfig was harvested
./scripts/troshka-oc list <project>

# Start tunnels for ALL clusters in the project (merged kubeconfig).
# A child process cannot export for you — eval is required.
eval "$(./scripts/troshka-oc use e0f60a08)"
oc config get-contexts
oc get nodes                          # current context
oc config use-context destination     # switch cluster
oc get nodes

# Stop the daemon and clear KUBECONFIG
eval "$(./scripts/troshka-oc teardown e0f60a08)"
# or without eval (still kills the daemon; leaves your shell's KUBECONFIG set):
./scripts/troshka-oc teardown e0f60a08

# Optional: tunnel only one cluster
eval "$(./scripts/troshka-oc use e0f60a08 source)"

# One-shot (no daemon; tunnel lives only for this oc invocation)
./scripts/troshka-oc --project e0f60a08 --cluster source get nodes

# Foreground / debug
./scripts/troshka-oc use --foreground e0f60a08   # Ctrl-C to stop
./scripts/troshka-oc use --shell e0f60a08        # shell inherits KUBECONFIG
./scripts/troshka-oc status e0f60a08
```

`<project>` may be the project UUID, an id prefix (e.g. `e0f60a08`), or the exact project name. Optional `<cluster>` may be the cluster display name or id (omit it to tunnel every cluster).

### Why `eval`?

Unix processes cannot change the parent shell’s environment. `use` **daemonizes**, waits until tunnels are ready, then prints only:

```bash
export KUBECONFIG=/Users/you/.troshka/kube/e0f60a08.yaml
```

on **stdout** (status messages go to stderr). Wrapping with `eval "$(…)"` runs that export in your shell after the daemon is up. Same idea for `teardown`, which prints `unset KUBECONFIG`.

## What it does

1. Resolves the project and cluster(s) via the Troshka **API** (owner or admin).
2. Spawns a background daemon that listens on `127.0.0.1:<ephemeral-port>` per cluster and dials **troshka-tunnel** as needed.
3. Local `oc` TCP connections become multiplex streams; if the WebSocket was dropped (Route idle, server stream-idle), the daemon **re-establishes** it on the next connection.
4. Rewrites a local kubeconfig with the correct `tls-server-name`.
5. Writes `~/.troshka/kube/<project8>.yaml` (mode `0600`) plus `.state.json` / `.pid` / `.log`.
6. Prints `export KUBECONFIG=…` on stdout once ready; `teardown` kills the daemon.

Multi-cluster `use` merges contexts (same naming as the showroom cluster terminal). Switch with `oc config use-context <name>`.

## API access

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `TROSHKA_API_URL` | Against prod / remote | `http://localhost:8200` | REST API (list projects, fetch kubeconfigs) |
| `TROSHKA_TUNNEL_URL` | Optional | derived from API URL | WebSocket tunnel service |
| `TROSHKA_API_KEY` | Against prod / remote | *(empty)* | User API key `trk_…` as `Authorization: Bearer …` |

| Environment | What you need |
|-------------|----------------|
| Local `./dev-services.sh` + tunnel on `:8201` | Usually nothing for API; start tunnel with `uvicorn app.tunnel_main:app --port 8201` |
| Deployed / shared Troshka | `troshka-api` + `troshka-tunnel` Routes + a **user** API key that **owns** the project |

### UI host vs API host vs tunnel host

| Role | Host pattern | Example |
|------|--------------|---------|
| UI (browser / SSO) | `troshka.apps.…` | `https://troshka.apps.ocpv-infra01.…` |
| API (REST + API keys) | `troshka-api.apps.…` | `https://troshka-api.apps.ocpv-infra01.…` |
| Tunnel (`troshka-oc` WS) | `troshka-tunnel.apps.…` | `https://troshka-tunnel.apps.ocpv-infra01.…` |

The UI host sits behind **oauth-proxy**. A `Bearer trk_…` key there returns **403** HTML “Log In”. The canvas **Local oc** panel copies the correct `troshka-api` and `troshka-tunnel` URLs.

If `TROSHKA_TUNNEL_URL` is unset, `troshka-oc` derives it:

- `troshka-api.` → `troshka-tunnel.`
- `localhost:8200` → `localhost:8201`

### Create an API key

1. Sign in to Troshka in the browser (UI host).
2. Open **Settings** → **API Keys**.
3. Create a key, copy it once (`trk_…`).
4. Export:

```bash
export TROSHKA_API_URL=https://troshka-api.apps.example.com
export TROSHKA_TUNNEL_URL=https://troshka-tunnel.apps.example.com
export TROSHKA_API_KEY=trk_…
./scripts/troshka-oc list my-project
```

Notes:

- Use a normal **user** API key (not ops-pod scoped).
- Auth is sent as an HTTP header (not `?token=`), so keys do not appear in access logs.

## How this relates to other access paths

| Path | When to use |
|------|-------------|
| **`troshka-oc` (this doc)** | Laptop `oc` against nested clusters you own |
| Showroom **OpenShift Cluster Terminal** | In-browser `oc` with VIP-rewritten kubeconfigs |
| Host **oc-exec** | Server-side `oc` inside the project network |

## Dial path (troshka-tunnel)

The tunnel service (not the API worker) picks a reachability path:

1. **KubeVirt native**: port-forward into the project gateway pod (one PF per multiplex session, many streams), then exec+`socat`, then Service ClusterIP, then public Route.
2. **troshkad hosts**: agent `POST /tcp-tunnel` dials the API VIP from the project netns (one agent tunnel per stream).

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| `API 403` + HTML “Log In” | `TROSHKA_API_URL` is the **UI** host; use `troshka-api` |
| `API 401` / Not authenticated | `TROSHKA_API_KEY` unset or invalid |
| `API tunnels moved to troshka-tunnel` | Old client hitting backend WS; upgrade `troshka-oc` / set `TROSHKA_TUNNEL_URL` |
| `oc` → `EOF` / timeout | Nested API unreachable from tunnel dial path; check cluster Ready + gateway |
| Dual-context `use`: first cluster EOFs after switching | `troshka-oc` re-dials the tunnel on the next `oc` (Route idle drops parked WS). Update `scripts/troshka-oc` and `teardown`/`use` again |
| Tunnel handshake timeout | Tunnel service down (`:8201` locally / Route in prod) |
| `No project matching …` | Wrong prefix/name, or key user does not own the project |
| `No clusters have a harvested kubeconfig` | Install/recert not finished |

## Canvas UI

On the project canvas, **OPENSHIFT INFO** → **Local oc** shows clone / `TROSHKA_API_URL` / `TROSHKA_TUNNEL_URL` / API key / `use` / `use-context` / teardown / one-shot commands.

## Design

See [`docs/superpowers/specs/2026-10-08-troshka-tunnel-service-design.md`](../superpowers/specs/2026-10-08-troshka-tunnel-service-design.md).
