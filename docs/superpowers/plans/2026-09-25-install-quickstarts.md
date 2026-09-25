# Install Quickstarts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline) or superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship three CLI one-button Troshka install rails (OCP, EKS, Local) with shared wipe/teardown and white-paper docs.

**Architecture:** Control-plane install scripts per platform share `quickstarts/lib` for API wipe/verify. Local uses Compose; OCP/EKS use Helm (EKS adds CFN + Ingress). Docs live under `docs/quickstarts/`.

**Tech Stack:** bash, Docker/Podman Compose, Helm 3, AWS CloudFormation, kubectl/oc, curl/jq

**Spec:** `docs/superpowers/specs/2026-09-25-install-quickstarts-design.md`

## Global Constraints

- Quickstart auth: oauth off / dev auto-admin
- No GUI installer — CLI scripts only
- Teardown must refuse platform delete while projects exist
- EKS CFN is control-plane capacity only (no troshkad hosts)
- macOS/Windows host agent runs on Linux guest/WSL, not Darwin/native Windows
- Image registry: `quay.io/redhat-gpte/troshka-*`

## File map

| Path | Responsibility |
|---|---|
| `quickstarts/lib/*.sh` | wait-for-api, wipe-workloads, verify-clean, common |
| `deploy/compose/*` | Local control-plane stack |
| `quickstarts/local/*` | local install/teardown/bootstrap-host |
| `quickstarts/ocp/*` | Helm wrap install/teardown |
| `deploy/helm/values-eks.yaml` + ingress template | EKS-friendly Helm |
| `deploy/eks/**` | CFN + IAM deployer policy |
| `quickstarts/eks/*` | CFN+helm install/teardown |
| `docs/quickstarts/*` | Hub + per-rail + teardown docs |

---

### Task 1: Shared lib + Compose + local rail

**Files:**
- Create: `quickstarts/lib/common.sh`, `wait-for-api.sh`, `wipe-workloads.sh`, `verify-clean.sh`
- Create: `deploy/compose/compose.yaml`, `deploy/compose/.env.example`
- Create: `quickstarts/local/install.sh`, `teardown.sh`, `bootstrap-host.sh`
- Create: `docs/quickstarts/local.md`

**Interfaces:**
- Produces: `TROSHKA_API_URL` (default `http://localhost:8200`); wipe uses `DELETE /api/v1/projects/{id}` then polls `GET /api/v1/projects/` until empty or timeout
- Consumes: published quay images; Podman/Docker Compose v2

- [ ] **Step 1:** Add `quickstarts/lib/common.sh` with `repo_root`, `confirm_or_yes`, `require_cmd`
- [ ] **Step 2:** Add `wait-for-api.sh` — curl health/projects until 200
- [ ] **Step 3:** Add `wipe-workloads.sh` — list projects, DELETE each, poll until none (timeout 30m default)
- [ ] **Step 4:** Add `verify-clean.sh` — exit 1 if any projects remain
- [ ] **Step 5:** Add Compose stack (postgres, redis, minio, backend, worker, frontend) with generated secrets in `.env`
- [ ] **Step 6:** `local/install.sh` / `teardown.sh` / `bootstrap-host.sh` (dnf/apt libvirt + pointer to UI host add / install-agent)
- [ ] **Step 7:** Write `docs/quickstarts/local.md` (Linux / brew+guest / WSL2 + compute menu)
- [ ] **Step 8:** Commit

---

### Task 2: OCP quickstart wrappers + docs

**Files:**
- Create: `quickstarts/ocp/install.sh`, `teardown.sh`
- Create: `docs/quickstarts/ocp.md`
- Modify: `deploy/helm/values.yaml` — add `route.enabled: true` (default)

- [ ] **Step 1:** Gate Route templates on `route.enabled`
- [ ] **Step 2:** `install.sh` — `helm upgrade --install` with postgres+s4, oauth off, wait Route, print URL
- [ ] **Step 3:** `teardown.sh` — wipe → verify → helm uninstall + delete ns
- [ ] **Step 4:** Write `docs/quickstarts/ocp.md`
- [ ] **Step 5:** Commit

---

### Task 3: Helm Ingress for EKS + values-eks

**Files:**
- Create: `deploy/helm/templates/frontend-ingress.yaml`
- Create: `deploy/helm/values-eks.yaml`
- Modify: `deploy/helm/templates/backend-config.yaml` — external_url from ingress or route host
- Modify: Route templates — `route.enabled`
- Modify: S4 Route — skip when `route.enabled` false

- [ ] **Step 1:** Add `ingress.enabled`, `ingress.className`, `ingress.host`, `ingress.annotations` to values; values-eks sets ingress on, route off, worker.replicas=2, postgres+s4 on, oauth off
- [ ] **Step 2:** Frontend Ingress template (ALB annotations for AWS)
- [ ] **Step 3:** external_url uses ingress.host when route disabled
- [ ] **Step 4:** Commit

---

### Task 4: EKS CloudFormation + IAM + scripts + docs

**Files:**
- Create: `deploy/eks/cloudformation/troshka-eks.yaml`
- Create: `deploy/eks/iam-deployer-policy.json`
- Create: `quickstarts/eks/install.sh`, `teardown.sh`
- Create: `docs/quickstarts/eks.md`

- [ ] **Step 1:** CFN — VPC 2AZ, EKS cluster, managed node group, OIDC outputs
- [ ] **Step 2:** IAM deployer policy JSON covering CFN/EC2/VPC/EKS/IAM/ELB/STS
- [ ] **Step 3:** `install.sh` — deploy stack, update kubeconfig, install aws-load-balancer-controller (helm), helm install troshka with values-eks, print URL
- [ ] **Step 4:** `teardown.sh` — wipe → verify → helm uninstall → cfn delete
- [ ] **Step 5:** `eks.md` with permissions matrix + Launch Stack narrative
- [ ] **Step 6:** Commit

---

### Task 5: Hub docs + teardown narrative + README link

**Files:**
- Create: `docs/quickstarts/README.md`, `teardown.md`
- Modify: `README.md` Installation section → point at quickstarts hub

- [ ] **Step 1:** Hub README with three rails
- [ ] **Step 2:** Shared teardown.md full-wipe story
- [ ] **Step 3:** Update root README Installation
- [ ] **Step 4:** Commit

---

## Spec coverage check

| Spec requirement | Task |
|---|---|
| Shared lib wipe/verify | 1 |
| Compose + local + bootstrap + local.md | 1 |
| OCP install/teardown + ocp.md | 2 |
| Helm Ingress + values-eks | 3 |
| CFN + IAM + eks scripts/docs | 4 |
| Hub + teardown.md + README | 5 |
| Universal compute menu | 1,2,4,5 docs |
| Dev auth defaults | all install scripts |
