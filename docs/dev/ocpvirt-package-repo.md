# OCP Virt host packages via HTTP repo (retire the boot DVD ISO)

## Goal

Stop importing the 11 GB RHEL DVD ISO into every dedicated-CI provision. Instead,
extract the DVD's `BaseOS`/`AppStream` repos **once** on the central-S4 cluster,
serve them over an **access-controlled HTTP endpoint**, and have the ocpvirt host's
cloud-init `dnf install` troshkad's dependencies from it.

Per-provision transfer drops from ~11 GB to the troshkad dependency closure
(hundreds of MB, cached on the host), and the boot-ISO DataVolume / cross-region
import / import-wait / 10-min VMI timeout all disappear.

**Scope: ocpvirt provider only.** AWS (`ec2.py`), Azure, GCP install via cloud-init
`packages:` from their built-in cloud RHEL repos; KubeVirt-native (`kubevirt.py`)
doesn't use the DVD. Only `ocpvirt.py` mounts `/dev/sr0` + `file:///mnt/iso`, so
this change cannot affect the cloud providers or prod.

## Current state (being replaced)

- Role `serviceaccount.yaml`: creates DataVolume `rhel-10.2-dvd-iso` by importing the
  11 GB DVD from central S4 (key resolved from `library/manifest.json`).
- Role `configure.yaml`: waits for that import before creating the host (interim fix, PR #53).
- `ocpvirt.py` cloud-init: `mount /dev/sr0`, writes `file:///mnt/iso/{BaseOS,AppStream}`
  repos, `dnf install` the virt stack.
- ocpvirt provider attaches the ISO as a CDROM (`installiso`) and removes it later.

When a package repo URL is configured (see below), cloud-init uses HTTP repos instead
and no DVD ISO is attached.

## Target architecture

1. **Extraction Job** (one-time per RHEL minor version): pulls the RHEL DVD ISO from
   central S4, extracts `BaseOS`/`AppStream` with `bsdtar` (no loop mount), writes
   to a shared PVC.
2. **nginx** on the same PVC: serves `rhel-<ver>/{BaseOS,AppStream}` with HTTP basic auth.
3. **Route** (edge TLS) on the central-S4 cluster.
4. **Host cloud-init**: `/etc/yum.repos.d/troshka-rhel.repo` → `baseurl` at the route,
   with `username`/`password`; `dnf install` as today.
5. No DVD DataVolume, no CDROM, no import-wait.

Infra is deployed by `deploy/ansible/pkg-repo.yaml` (see
[`deployment.md`](deployment.md#ocp-virt-package-repo)).

## Configuration (`src/backend/config/config.yaml`)

Cluster-specific defaults live under `ocpvirt.pkg_repo`:

```yaml
ocpvirt:
  pkg_repo:
    url: "https://repo-troshka-images.apps.ocpv-infra01.dal12.infra.demo.redhat.com/rhel-10.2"
    username: troshka-repo
    password: ""   # config.local.yaml or TROSHKA_OCPVIRT__PKG_REPO__PASSWORD
    route_host: "repo-troshka-images.apps.ocpv-infra01.dal12.infra.demo.redhat.com"
    iso_library_item_name: "RHEL 10.2 Binary DVD"
```

- **`url` / `username` / `password`** — used by the backend when provisioning ocpvirt
  hosts (provider registration can override per provider).
- **`route_host`** — passed to the Ansible playbook below.
- **`iso_library_item_name`** — display name in `library/manifest.json`; the playbook
  resolves the S3 key at extraction time (same mechanism as the agnosticd ISO DataVolume).

## Deploy the repo

```bash
# route_host and iso_library_item_name from config.yaml ocpvirt.pkg_repo
ansible-playbook deploy/ansible/pkg-repo.yaml \
  -e pkg_repo_password='<from-vault>' \
  -e pkg_repo_route_host='<ocpvirt.pkg_repo.route_host>' \
  -e pkg_repo_iso_library_item_name='<ocpvirt.pkg_repo.iso_library_item_name>'
```

Requires `KUBECONFIG` for the cluster that hosts central S4 (`troshka-images` ns).
Password is required via `-e`; nothing sensitive is stored in the playbook.

After the playbook succeeds, set `ocpvirt.pkg_repo.password` in `config.local.yaml`
(or env) so host provisioning can authenticate to the repo.

## Backend / provider config

The backend reads `ocpvirt.pkg_repo` from config by default. Per-provider overrides
at registration time (`POST /api/v1/providers/`) still win:

| Field | Source |
|-------|--------|
| `pkg_repo_url` | provider body, else `ocpvirt.pkg_repo.url` |
| `pkg_repo_username` | provider body, else `ocpvirt.pkg_repo.username` |
| `pkg_repo_password` | provider body, else `ocpvirt.pkg_repo.password` |

When a repo URL is resolved, `ocpvirt.py`:

- Writes `troshka-rhel.repo` with HTTP `baseurl` + basic auth.
- Skips `/dev/sr0` mount and `file://` repos.
- Does not attach the DVD ISO CDROM.

Leave `ocpvirt.pkg_repo.url` empty (and no provider override) to keep the legacy DVD ISO path.

## Role changes (`agnosticd/namespaced_workloads`)

Once the HTTP repo path is validated:

- `serviceaccount.yaml`: remove the manifest-key resolve + `rhel-10.2-dvd-iso` DataVolume
  (+ `central-s4-creds` if unused elsewhere).
- `configure.yaml`: remove the "Wait for ISO import" task.
- New tag (`troshka-dedicated-1.0.3`) + agnosticv pin bump.

## Security

- Repo is behind **HTTP basic auth**; dnf gets the cred via cloud-init (backend-controlled, rotatable).
- Optional defense-in-depth: `haproxy.router.openshift.io/ip_whitelist` on the Route with
  sandbox-cluster egress CIDRs.
- Upgrade path to **mTLS**: serve client-cert-required TLS and ship `sslclientcert`/`sslclientkey`
  via cloud-init instead of basic auth.

## Sequencing / rollout

1. PR #53 (ISO fix) — fallback path if HTTP repo is not configured.
2. Infra: run `pkg-repo.yaml` on the central-S4 cluster (values from `config.yaml`).
3. Backend: set `ocpvirt.pkg_repo.password` in local config; restart backend.
4. Role: drop ISO DV/wait → new tag + pin.
5. Validate a provision; keep the DVD ISO object in S4 for rollback.

## Testing

- From a pod on a sandbox cluster: `curl -u user:pass` against the repo `repomd.xml` URL from config.
- End-to-end dedicated-CI provision on a sandbox cluster.

## Rollback

- Clear `ocpvirt.pkg_repo.url` in config (and any provider overrides) to restore the DVD ISO path.
- Revert the role pin to the ISO tag if agnosticd changes were applied.

## Caveats

- RHEL content redistribution — internal infra only.
- Re-run extraction (or bump `pkg_repo_rhel_version`) when the RHEL minor version changes.
- GPG: RHEL guest already has `/etc/pki/rpm-gpg/RPM-GPG-KEY-redhat-release`.

## Open follow-up (separate)

Two `troshka-host-*` VMs/records are created per provision (one fails
`provision_failed`, one succeeds). Likely a backend `provision_host` retry; investigate
and make host creation single-shot.
