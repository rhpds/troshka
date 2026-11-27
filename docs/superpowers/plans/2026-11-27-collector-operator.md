# Collector Operator Implementation Plan

> **For agentic workers:** Use subagent-driven-development for the local operator task and review; execute central integration in this session.

**Goal:** Automatically maintain collector credentials and toolbox pins in pods.

**Architecture:** Local operators publish verified transport envelopes. A central
publisher installs immutable application configuration and verifies consumers.

**Tech Stack:** Python3, Kopf, Kubernetes clients, Helm, existing Argo publication.

**Spec:** `docs/superpowers/specs/2026-11-27-collector-operator-design.md`

## Global Constraints

- No broad collector permissions, new enrollment, or custody bypass.
- Token renewal margin 48 hours; request 168 hours; reject under 96 hours.
- Deployment UID, cluster UID, Ceph FSID and pool IDs remain approved anchors.
- Credentials never appear in public output.
- Main before production; named immutable release tag.

## Tasks

- [x] Local operator: implement `helpers/collector_transport.py`, startup handler,
  scoped RBAC and unit tests for identity, expiry, idempotency and failures.
  Use `src/backend/venv/bin/python3 -m pytest src/operator/tests/test_collector_transport.py`.
- [x] Central publisher: add operator-envelope mode to maintenance preparation,
  validate it against existing pins, fetch through provider credentials using
  protected TLS, retain current immutable publication and persistent journal.
  Unit-test changed anchors, invalid expiry, unchanged envelope and retry fence.
- [x] Runtime: package publisher dependencies, add opt-in Helm pod/PVC/SA/RBAC,
  report safe status, replace systemd instructions with operator enablement.
  Render chart with maintenance disabled and enabled; test scopes and mounts.
- [ ] Review full diff and run script/operator tests; correct the known release
  test stdout assertion. Commit complete intended changes, release named tag.
- [ ] Install exact approved operator configuration, enable central controller,
  verify renewal/publication and a subsequent healthy reconciliation. Preserve
  live jobs and report any independent incomplete fleet enrollment.

## Execution ledger

Ruling: reuse existing provider identities for authenticated reads of the named
operator output Secret, with protected TLS configuration. This avoids creating
another externally managed expiring maintenance credential.

Review: no-change cycles now verify current deployments and every replica's
protected files; configured Argo namespace reaches the API adapter. Status uses
explicit merge-patch, with response handling compatible with Kubernetes clients
31 and 36. Python websocket exec ignores its requested HTTP method; pinned,
checksum-verified oc 4.20.10 with SPDY preserves create-only exec permissions.

Validation: script suite 278 passed; live read-only ocpv08 API/SPDY identity
probe passed. Local operator unit tests cover real client request boundaries,
with no cluster access. Full operator suite is run from src/operator so
subprocess import tests use the correct module root.
