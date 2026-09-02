# Live-Environment Verification Harness

## Purpose

The live-environment harness (`tests/live/`) automates the **Plan 4b — [LIVE-ENV] Verification Checklist** for testing real OCP install flows (pod-default and bastion methods) against deployed Troshka instances. It provides:

- **Tier 1 (fast, ~15–20 min):** API wiring, ops-pod lifecycle, scoped key least-privilege, bastion regression
- **Tier 2 (slow, ~30–60 min):** Real OCP cluster install via both pod and bastion methods, monitor phase progression, idempotency/cancel/destroy flows
- **Two provider tracks:** troshkad (libvirt host) and KubeVirt (Kubernetes-native)

All tests are **skipped** when not configured (see below). Projects are always deleted on teardown (no manual cleanup required).

## Environment Configuration

The harness is driven by environment variables. At minimum, set `TROSHKA_LIVE_URL` to activate tests; other vars enable specific tracks or change timeouts.

| Variable | Purpose | Example |
|----------|---------|---------|
| `TROSHKA_LIVE_URL` | Backend base URL (activates harness) | `http://localhost:8200` |
| `TROSHKA_LIVE_TOKEN` | API token (omit for dev auth) | `trk_...` |
| `TROSHKA_LIVE_TROSHKAD_HOST` | SSH/exec target for troshkad ops-pod tests | `192.168.1.10` |
| `TROSHKA_LIVE_KUBECONFIG` | Path to KubeVirt cluster kubeconfig | `/home/user/.kube/config-kubevirt` |
| `TROSHKA_LIVE_KUBEVIRT_HOST` | Hostname for KubeVirt pod readiness checks | `kubevirt-cluster` |
| `TROSHKA_LIVE_TIER2` | Set to `1` to enable slow real-install tests | `1` |
| `TROSHKA_LIVE_TIMEOUT_S` | Overall test timeout (default: 4200s = 70 min) | `7200` |

### Reaching a Sandbox Instance

For a Troshka deployed in a Kubernetes cluster (e.g., OCP):

```bash
# Start a local port-forward to the backend service
oc port-forward -n troshka svc/troshka-backend 8200:8200 --kubeconfig=/path/to/kubeconfig &

# Set the harness to use the local tunnel
export TROSHKA_LIVE_URL=http://localhost:8200

# For dev instances (OpenShift with pre-authenticated operator), no token needed.
# For production/external instances, set the token:
export TROSHKA_LIVE_TOKEN=trk_abc123...
```

## Authentication

- **Dev instances:** Omit `TROSHKA_LIVE_TOKEN` — the harness auto-authenticates as admin (same as the backend's dev mode).
- **Production/external:** Set `TROSHKA_LIVE_TOKEN` to a valid API token (obtain from Settings → API Tokens or via the backend).

## Invocation

### Tier-1 Tests (Both Providers)

Fast smoke tests (~15–20 min total) covering API wiring, ops-pod creation, scoped key validation, and bastion regression:

```bash
cd /Users/prutledg/troshka/src/backend
TROSHKA_LIVE_URL=http://localhost:8200 \
  TROSHKA_LIVE_TROSHKAD_HOST=192.168.1.10 \
  TROSHKA_LIVE_KUBECONFIG=/home/user/.kube/config-kubevirt \
  TROSHKA_LIVE_KUBEVIRT_HOST=kubevirt-cluster \
  ./venv/bin/python3 -m pytest tests/live -m "live_env and not tier2" -v
```

### Tier-2 Tests (Real Install, Both Providers)

Slow real-world tests (~30–60 min) for pod and bastion OCP installs, monitor phase progression, idempotency, cancel, and destroy:

```bash
cd /Users/prutledg/troshka/src/backend
TROSHKA_LIVE_TIER2=1 \
  TROSHKA_LIVE_URL=http://localhost:8200 \
  TROSHKA_LIVE_TROSHKAD_HOST=192.168.1.10 \
  TROSHKA_LIVE_KUBECONFIG=/home/user/.kube/config-kubevirt \
  TROSHKA_LIVE_KUBEVIRT_HOST=kubevirt-cluster \
  ./venv/bin/python3 -m pytest tests/live -m "live_env and tier2" -v
```

### Single-Track Runs

To run only the troshkad provider (libvirt host):

```bash
./venv/bin/python3 -m pytest tests/live -m "live_env and live_troshkad and not tier2" -v
```

To run only the KubeVirt provider:

```bash
./venv/bin/python3 -m pytest tests/live -m "live_env and live_kubevirt and not tier2" -v
```

## Tier-2 Preconditions

Tier-2 tests deploy real OCP clusters and require:

1. **OCP pull secret configured:** In the Troshka UI, navigate to **Settings → OCP Pull Secret** and paste your Red Hat pull secret (same as used for production clusters). This is used for both pod and bastion install methods.

2. **For troshkad host:** The host must have network connectivity to the internet (or a configured pull-through registry) and must be able to reach the agent ISO served by the ops pod and the Redfish BMC virtual media endpoint.

3. **For KubeVirt host:** The cluster must have container-image pull permissions for the EE image (`quay.io/redhat-gpte/troshka-ops-pod:latest`) and network connectivity for the pod to reach the agent ISO and BMC endpoints.

## Runtimes

- **Tier-1 (ops-pod lifecycle, security, bastion):** ~5–10 min per provider (parallel per track, serialized across tracks)
- **Tier-2 single-cluster install:** ~20–30 min per cluster (pod and bastion run in separate tests, both ~25–30 min)
- **Full suite (Tier-1 + Tier-2, both providers):** ~60–120 min depending on cluster readiness and network speed

## Teardown

All tests include automatic project deletion (destroy-on-teardown). **No manual cleanup is required.** If a test is interrupted:

1. Check the Troshka UI for orphaned projects (recent `created_at`).
2. Manually delete them via the UI (Delete button), or use the API: `curl -X DELETE http://localhost:8200/api/v1/projects/{project_id}`.

For troshkad hosts, orphaned ops pods are reaped by the garbage-collection worker (runs every 5 min); no manual pod deletion needed.

## File Structure

- `live_config.py`: Env-var-driven `LiveConfig` dataclass
- `live_api.py`: HTTP client + assertion helpers (`LiveClient`, `poll_ocp`, etc.)
- `live_hostcmd.py`: SSH/exec utilities for host and pod inspection (`host_ssh`, `oc`, `host_db`, `ops_pod_apikey_row`)
- `live_scopedkey.py`: Scoped API key helpers (`scoped_key_from_pod`, `client_for_key`)
- `conftest.py`: Collection guard, skip decisions, pytest fixtures
- `test_tier1_ops_pod.py`: Ops-pod creation, running state, container secrets
- `test_tier1_security.py`: Scoped key least-privilege, revocation
- `test_tier1_bastion.py`: Bastion regression (unchanged from pre-Plan-4b)
- `test_tier2_install.py`: Pod and bastion real OCP install, phase progression
- `test_tier2_lifecycle.py`: Cancel, destroy, idempotent restart
- Unit tests (`test_live_*.py`): Pure-unit coverage of config, client, hostcmd, scopedkey (always run)

---

For detailed verification steps and rationale, see **`docs/superpowers/plans/2026-09-02-multi-cluster-ocp-plan4b-live-env-checklist.md`**.
