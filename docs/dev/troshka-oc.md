# Local `oc` via `troshka-oc`

Use stock `oc` on your laptop against nested OpenShift clusters in a Troshka project you own. The helper opens an authenticated tunnel through the Troshka API so you do not need in-cluster DNS, a bastion, or a manually rewritten kubeconfig.

## Prerequisites

- `oc` on your `PATH`
- Repo checkout (`scripts/troshka-oc`)
- A Troshka project **you own** (or admin) with at least one nested cluster that has finished install (harvested kubeconfig)
- API reachability + auth (see [API access](#api-access) below)

## Quick start

```bash
# Point at the Troshka API (required against prod / remote; see API access)
export TROSHKA_API_URL=https://troshka.example.com
export TROSHKA_API_KEY=trk_…   # from Settings → API Keys

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

1. Resolves the project and cluster(s) via the Troshka API (owner or admin).
2. Spawns a background daemon that opens a WebSocket API tunnel per cluster.
3. Listens on `127.0.0.1:<ephemeral-port>` and rewrites a local kubeconfig with the correct `tls-server-name`.
4. Writes `~/.troshka/kube/<project8>.yaml` (mode `0600`) plus `.state.json` / `.pid` / `.log`.
5. Prints `export KUBECONFIG=…` on stdout once ready; `teardown` kills the daemon.

Multi-cluster `use` merges contexts (same naming as the showroom cluster terminal). Switch with `oc config use-context <name>`.

## API access

`troshka-oc` talks to the Troshka backend over HTTPS (REST + a WebSocket tunnel). Configure with env vars:

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `TROSHKA_API_URL` | Against prod / remote | `http://localhost:8200` | Base URL of the Troshka API (no trailing slash needed) |
| `TROSHKA_API_KEY` | Against prod / remote | *(empty)* | User API key `trk_…` sent as `Authorization: Bearer …` (and as `?token=` on the WebSocket) |

| Environment | What you need |
|-------------|----------------|
| Local `./dev-services.sh` | Usually nothing — backend auto-auths as admin. URL defaults to localhost. |
| Deployed / shared Troshka | `TROSHKA_API_URL` + a **user** API key for an account that **owns** the project |

### Create an API key

1. Sign in to Troshka in the browser (same environment you will call with `TROSHKA_API_URL`).
2. Open **Settings** (user menu) → **API Keys**.
3. Enter a name (e.g. `laptop-oc`), optionally set an expiry, click **Create Key**.
4. **Copy the key immediately** — it is shown once (`trk_…`) and cannot be retrieved later.
5. Export it in your shell (or add to `~/.zshrc` / a private env file):

```bash
export TROSHKA_API_URL=https://troshka.example.com   # your Troshka UI/API origin
export TROSHKA_API_KEY=trk_…                          # paste the key
./scripts/troshka-oc list my-project                  # sanity check
```

Notes:

- Use a normal **user** API key. Scoped ops-pod keys are rejected by the tunnel WebSocket.
- The key authenticates as **you**: you only see projects you own (admins see all).
- Rotate by creating a new key and deleting the old one in Settings if it leaks.

## How this relates to other access paths

| Path | When to use |
|------|-------------|
| **`troshka-oc` (this doc)** | Laptop `oc` against nested clusters you own |
| Showroom **OpenShift Cluster Terminal** | In-browser `oc` with VIP-rewritten kubeconfigs |
| Host **oc-exec** | Server-side `oc` inside the project network |

Showroom terminal kubeconfigs are injected onto the showroom disk separately. `troshka-oc` only writes under `~/.troshka/kube/` and does not change topology / showroom files.

## Dial path (backend)

The API tunnel picks a reachability path by host/provider:

1. **KubeVirt native** (`provider.type=kubevirt` / `host_type=kubevirt-cluster`): port-forward into the project gateway pod (needs `pods/portforward` on the provider SA), then exec+`socat`, then in-cluster Service ClusterIP, then public Route (last resort).
2. **ocpvirt / EC2 / other troshkad hosts** (nested virt on a host VM): agent `POST /tcp-tunnel` dials the API VIP from the project netns (`troshka-<project8>`). Do **not** look for a management-cluster project namespace — that is the kubevirt-native layout.

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| `No project matching …` | Wrong prefix/name, or API key user does not own the project |
| `No clusters have a harvested kubeconfig` | Install/recert not finished; wait until OPENSHIFT INFO shows credentials |
| TLS handshake timeout / tunnel error | Backend cannot reach the nested API (RBAC, gateway down, cluster not Ready) |
| `oc: command not found` | Install the OpenShift CLI and ensure it is on `PATH` |
| Scoped API key rejected on WebSocket | Use a normal user API key, not an ops-pod scoped key |

## Canvas UI

On the project canvas, **OPENSHIFT INFO** has a project-level **Local oc** expandable (above the per-cluster API/console blocks) with clone / `use` (all clusters) / `use-context` / teardown / one-shot commands.
