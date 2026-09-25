# Troshka Install Quickstarts Design

Three blog- and white-paper-ready install rails for Troshka: **OpenShift**, **Amazon EKS**, and **Local (Mac/Windows/Linux)**. Each rail has full documentation plus a CLI one-button install and teardown. No GUI installer.

## Goals

- From zero to Troshka UI with one primary command per platform (EKS may use Launch Stack + a thin wait/helm script).
- Symmetric teardown: destroy Troshka-owned projects/VMs/cloud artifacts first, then remove the platform install.
- After the app is up, a **universal compute menu** on every rail (including Linux): local/near-local host, remote Linux, or EC2 / Azure / GCP / KubeVirt.
- Deep enough docs for a public white paper/blog; slick enough that the happy path is one transcript.

## Non-goals (v1)

- GUI / TUI installer UX.
- Production SSO as the quickstart default (dev auto-admin on happy path; SSO remains in long-form OCP docs).
- Bring-your-own existing EKS as the primary path (appendix only).
- CloudFormation provisioning of Troshka compute hosts or nested-VM capacity.
- Native Darwin troshkad (macOS is not a Troshka host; agent runs on Linux).

## Concepts: control plane vs compute

| Layer | What | Local example | Cloud example |
|---|---|---|---|
| **Control plane** | Troshka app (API, UI, DB, Redis, object store) | Compose on laptop | Helm on OCP or EKS |
| **Compute** | Where lab VMs run | libvirt + troshkad | EC2 / Azure / GCP / KubeVirt / remote Linux |

Rails install the control plane. Compute is chosen after the UI is up and is the same menu on every rail.

## Repository layout

```
docs/quickstarts/
  README.md              # hub — blog entry point (pick a rail)
  ocp.md
  eks.md
  local.md
  teardown.md            # shared full-wipe narrative

quickstarts/
  ocp/install.sh
  ocp/teardown.sh
  eks/install.sh
  eks/teardown.sh
  local/install.sh
  local/teardown.sh
  local/bootstrap-host.sh   # run on the Linux host/guest/WSL
  lib/
    wait-for-api.sh
    wipe-workloads.sh       # destroy all projects via API; wait
    verify-clean.sh         # refuse uninstall if leftovers remain

deploy/
  compose/                  # local control-plane stack
  eks/
    cloudformation/         # greenfield VPC + EKS + IAM + EBS CSI IRSA (+ optional Cognito)
    iam-deployer-policy.json
  helm/
    values-eks.yaml         # Ingress-nginx, sslip.io/LE TLS, basic auth (Cognito on --production)
```

Existing deep guides stay authoritative for advanced topics:

- [`docs/install-ocp.md`](../../install-ocp.md) — OCP SSO, groups, overlays
- [`docs/install-aws.md`](../../install-aws.md) (and gcp/azure/ocpvirt/kubevirt) — provider setup linked from the compute menu

Root [`README.md`](../../../README.md) links to `docs/quickstarts/README.md`.

## Shared UX shape (every rail doc)

1. Prerequisites  
2. One install command  
3. Open the printed URL (dev auto-admin)  
4. Compute menu (local host / remote Linux / cloud providers)  
5. Teardown → one command + link to `teardown.md`

## OpenShift rail

**Install:** `quickstarts/ocp/install.sh` wraps the existing Helm chart (preferred) or Ansible all-in-one defaults from `docs/install-ocp.md`:

- In-cluster Postgres + MinIO/S4  
- OAuth **off** (dev auto-admin)  
- Print Route URL when ready  

**Docs:** short `docs/quickstarts/ocp.md`; keep SSO/groups in `install-ocp.md`.

**Teardown:** shared wipe → Helm uninstall / namespace delete (stronger than today’s namespace-only Ansible undeploy).

## Amazon EKS rail

**Model:** full-stack greenfield. CloudFormation creates the cluster; the install script deploys Troshka with Helm.

**Install script sequence:**

1. Create/update CloudFormation stack (VPC, EKS, node group, EBS CSI + IRSA OIDC)  
2. Wait until stack `CREATE/UPDATE_COMPLETE`; write kubeconfig  
3. Install ingress-nginx (NLB) + cert-manager + `letsencrypt-prod` ClusterIssuer  
4. Ensure default `gp3` StorageClass (EBS CSI); optional EKS access entry for deployer  
5. Hostname: `troshka.<nlb-ip>.sslip.io` (default) or `--domain` via Route53 CNAME  
6. `helm upgrade --install` with `values-eks.yaml` (TLS + basic auth, or Cognito on `--production`)  
7. Print HTTPS URL + credentials  

**Stack contents:**

- VPC (2 AZs), public + private subnets, NAT, IGW  
- EKS control plane + managed node group sized for the **control plane only**  
- Cluster IAM OIDC provider + IRSA role for **EBS CSI** (required so the addon does not hang)  
- Optional `--production`: Cognito User Pool + app client for oauth2-proxy OIDC  
- Outputs: cluster name, VPC, OIDC issuer, Cognito IDs (when enabled)  

Nested-VM capacity is **not** in the CFN stack itself; `install.sh` seeds one EC2 provider + host after Helm (Fedora by default, RHEL on `--production`).

**Helm differences vs OCP:** Kubernetes Ingress (nginx) instead of Routes; public Postgres/Redis images (no `registry.redhat.io` pull secret); edge auth via nginx basic auth (default) or oauth2-proxy→Cognito (`--production`). OpenShift `ose-oauth-proxy` is **not** used (`route.enabled=false`).

### Permissions (required documentation)

`docs/quickstarts/eks.md` includes an explicit matrix for the **deploying principal**, plus `deploy/eks/iam-deployer-policy.json` as a least-privilege starter. Call out that AdministratorAccess is easiest for demos.

| Area | Why |
|---|---|
| CloudFormation | Create/update/delete stack |
| EC2 / VPC / EIP / NAT / SG | Network for the cluster |
| EKS | Cluster, node groups, addons, access entries |
| IAM | Roles, OIDC provider (IRSA), PassRole for EKS |
| ELB / NLB | ingress-nginx Service |
| Route53 / Cognito | `--domain` / `--production` |
| CloudWatch Logs (optional) | Cluster logging |
| STS GetCallerIdentity | Script sanity checks |

Teardown needs the same principal for stack delete, plus notes for stuck ENIs/NLBs after delete.

## Local rail (Compose + host)

**Install:** `quickstarts/local/install.sh`

- Requires Podman or Docker Compose v2  
- Services: backend, frontend, worker, Postgres, Redis, MinIO  
- Images from `quay.io/redhat-gpte/troshka-*`  
- Dev auth; publish UI/API ports (e.g. `:3100` / `:8200`)  
- Print localhost URL and next step for compute  

**Host bootstrap** (`bootstrap-host.sh` on the Linux environment that will run libvirt + troshkad):

| OS | Happy path |
|---|---|
| **Linux** | Native KVM + libvirt → bootstrap-host |
| **macOS** | Homebrew `libvirt` + `qemu` → define/start a Linux guest → bootstrap-host **inside the guest** |
| **Windows** | WSL2 + nested virtualization + libvirt → bootstrap-host inside WSL |

**Caveats (document clearly):**

- troshkad requires Linux; macOS/Windows never run the agent on the host OS itself.  
- Nested virt on Apple Silicon and some Windows setups is limited; if it fails, use remote Linux or a cloud provider.  
- WSL networking is NAT’d — fine for single-host demos; multi-host mesh is out of quickstart scope.  

UTM / Hyper-V full guests are appendix-only fallbacks, not the happy path.

## Universal compute menu

After any control-plane install, docs present the same choices:

1. Local / near-local host (table above)  
2. Remote Linux with troshkad  
3. Cloud / virt provider: EC2, Azure, GCP, or KubeVirt (link existing install-* guides)  

This applies to **Linux local installs as well**, not only Mac/Windows.

## Shared teardown engine

```
quickstarts/<platform>/teardown.sh
  → lib/wipe-workloads.sh    # destroy all projects via API; poll until gone
  → lib/verify-clean.sh      # fail if projects (or known tagged leftovers) remain
  → platform uninstall       # compose down -v | helm uninstall + ns | cfn delete
```

**Rules:**

- Default: interactive confirm; `--yes` for non-interactive.  
- **Refuse** platform uninstall while projects still exist.  
- On wipe timeout: print stuck resources; do not delete the stack.  
- Optional post-wipe AWS sweep: warn/delete Troshka-tagged leftovers the API missed.  
- `docs/quickstarts/teardown.md` is the full-wipe narrative linked from every rail.  

Platform-specific leftover checklists in that doc: EKS ENIs/NLBs, Compose volumes, WSL/guest notes, OCP namespace finalizers.

```
install.sh → Troshka UI → compute (local | remote | cloud provider)
                              ↓
                         labs / destroy
                              ↓
teardown.sh → API wipe → verify-clean → uninstall platform
```

## Auth defaults

- **OCP / Local happy path:** oauth off → auto-admin (blog transcript).  
- **EKS default:** nginx basic auth at the Ingress; app still oauth-off behind the password.  
- **EKS `--production --domain`:** Cognito Hosted UI via oauth2-proxy; backend `oauth_enabled=true`.  
- OpenShift SSO (`ose-oauth-proxy`) remains documented in `install-ocp.md` / Helm `auth.oauthEnabled` + `route.enabled`.

## Implementation order

1. This design spec (review gate)  
2. Shared `quickstarts/lib` + Compose stack + local docs/scripts  
3. OCP install/teardown wrappers + `docs/quickstarts/ocp.md`  
4. Helm `values-eks.yaml` + CloudFormation + IAM policy + eks scripts/docs  
5. Shared `teardown.md` + hub README + root README link  
6. Smoke install→teardown on each rail where credentials allow  

## Success criteria

- Blog reader can complete one rail with a single install command and a single teardown command.  
- EKS doc answers “what IAM do I need?” without reading the CFN template.  
- Teardown never orphan-bills AWS/OCP by deleting the control plane while labs still exist.  
- Mac/Windows readers have a clear Linux-guest or WSL path **and** a clear “use remote/cloud compute instead” escape hatch.  
