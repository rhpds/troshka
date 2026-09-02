# Plan 4b — [LIVE-ENV] Verification Checklist (pod-default OCP install)

**Purpose:** Plan 4b (per-project `install_via`, default `pod`) is verified at unit/wiring level in the
authoring session. The real pod / Redfish / agent-install run is **not** exercisable there. This checklist
is the gate before trusting the pod path in production. Run it on a real troshkad host and a real
KubeVirt (`kubevirt-cluster`) host, with the published EE image
(`quay.io/redhat-gpte/troshka-ops-pod:latest`, CI-built) available.

**Scope reminder:** the bastion path is RETAINED, not removed. `install_via: bastion` must remain
byte-identical to pre-Plan-4b behavior; `install_via: pod` is the new default and applies to ALL host
types (no KubeVirt→bastion fallback — parity landed in Tasks 8b/8c).

---

## Pre-flight
- [ ] EE image published and pullable from the target host(s): `podman pull quay.io/redhat-gpte/troshka-ops-pod:latest` (troshkad host) / image resolvable in-cluster (KubeVirt).
- [ ] OCP pull secret configured (Settings → OCP pull secret) — required for both pod and bastion.
- [ ] A troshkad host connected AND a `kubevirt-cluster` host available.
- [ ] Backend + worker restarted on the code under test (`dev-services.sh restart backend` / `restart worker`) — deploy/destroy run on the RQ worker.

## A. Default (pod) OCP project — troshkad host
- [ ] Create an OCP project from a template WITHOUT choosing an install method → dialog defaults to **Pod**; no RHEL image/ISO/BMC-IP fields required to click Create.
- [ ] Confirm persisted `topology["ocpInstallVia"] == "pod"` (`host-db.sh`).
- [ ] Deploy. An ops pod `troshka-<pid8>-ops` is created + started (podman), attached to the infra-transit network (ops `.4`), privileged.
- [ ] **Per-cluster install runs in parallel** from the pod: for each cluster, `openshift-install agent create image` → ISO served → Redfish InsertMedia+Reset loop → `wait-for install-complete`.
- [ ] **Monitor reports phases:** `project.ocp_status` / `ocp_install_elapsed` advance (creating-image → installing → ready) and are visible in the UI — even though there is no bastion `ocpMonitor` VM.
- [ ] **Success → ready (not timeout):** a cluster that writes `install complete` flips to `ready` promptly; the deploy does NOT sit until the 2h timeout. (Task 8c gap.)
- [ ] **Secrets not exposed:** `podman inspect troshka-<pid8>-ops` shows NO install-config / agent-config / pull-secret content in argv/env command; those live as 0600 host files bind-mounted read-only (Task 8). `TROSHKA_API_KEY` is in env only (acceptable per spec §7).
- [ ] **Idempotent restart:** kill/restart the ops pod (it is `restart_policy=always`) mid-install → completed clusters (kubeconfig present) are SKIPPED on restart; only unfinished clusters resume. No duplicate installs.
- [ ] **Dead-pod → failed (not spin):** force the pod to crash-loop (e.g. bad image) → after 3 consecutive confirmed not-running polls the non-terminal clusters report `failed`, not a 2h hang. A transient troshkad status error must NOT trip this (counter resets).
- [ ] **Cancel stops the pod:** cancel the install → `/pods/destroy` removes `troshka-<pid8>-ops`; status shows `cancelled`.
- [ ] **Scoped key least-privilege:** while running, the pod's API key can read topology + exec on VMs ONLY (`topology:read`, `vm:exec`); every other route returns 403 (global default-deny). A websocket with the scoped key closes 4003 on non-allowlisted use.
- [ ] **Destroy revokes the key:** destroying the project revokes/deletes the scoped ApiKey (row gone / `is_scoped` key no longer authenticates).

## B. Default (pod) OCP project — KubeVirt host
- [ ] Same create flow → `install_via: pod` (no bastion fallback on kubevirt).
- [ ] Deploy → a namespace Pod `troshka-<pid8>-ops` in the project namespace (`_project_ns`, `troshka-<pid8>`), on `OPS_POD_IMAGE`, attached via `k8s.v1.cni.cncf.io/networks` to BOTH the cluster NAD(s) and the BMC NAD, privileged + NET_ADMIN/NET_RAW (mirrors `build_bmc_deployment`).
- [ ] Per-cluster configs + pull secret ride in a **k8s Secret** mounted at the workdir paths — NOT in argv.
- [ ] **Log-read via k8s exec:** monitor tails each cluster's `install.log` via `connect_get_namespaced_pod_exec` (container `ops`); phases advance; success → ready (not timeout).
- [ ] **Running-check conservative:** a transient k8s API error does not false-fail the deploy (assume-running); `Succeeded`/`Failed`/404 → not running.
- [ ] **Cancel** deletes the k8s Pod (`delete_namespaced_pod`, grace 0); status `cancelled`.
- [ ] NAD reachability: pod can serve the agent ISO to nested VMs and reach sushy `:8000`.

## C. Bastion regression (install_via: bastion) — MUST be unchanged
- [ ] Create an OCP project and select **Bastion** → image/ISO/BMC-IP fields reappear and are required; the create-gate enforces them.
- [ ] Persisted `topology["ocpInstallVia"] == "bastion"`.
- [ ] Deploy (single-cluster) → the bastion VM is baked (cloud-init) exactly as before; NO ops pod is created.
- [ ] The bastion runs the install script (`_build_install_script`, golden-tested byte-identical) and installs the cluster; DNS/VNC/console/monitor behave as pre-Plan-4b.
- [ ] `install_via: bastion` + multi-cluster is rejected with a clear validation error (a bastion cannot run multi-cluster).

## D. Known deferred items (track, not blockers for this checklist)
- `pull_secret_json=""` is passed at troshkad create time; the pull secret currently rides inside the per-cluster install-config files rather than as a separate top-level file. Confirm the install still authenticates to registries; wire an explicit top-level pull-secret file if a live run shows it's needed.
- Backend/worker restart durability: the install monitor runs as a daemon thread; a worker restart mid-install relies on the pod's own idempotency (Task 4) + `restart_policy=always` to resume — verify a worker bounce mid-install recovers.
- Undeploy does not clear `ocp_status` (pre-existing; affects the bastion path too). A re-deploy of the same project starts from the prior status string until the monitor overwrites it.

---

**Sign-off:** Plan 4b is unit/wiring-green (full backend suite + frontend suite green; pyright clean on all
Plan 4b production files; bastion golden unchanged; end-to-end `install_via` resolution = pod default on all
host types). Production trust requires sections A–C above to pass on real hosts.
