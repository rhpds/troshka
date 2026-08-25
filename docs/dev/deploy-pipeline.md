# Deploy Pipeline & Deploy-Time Behavior Reference

> Extracted from the top-level `CLAUDE.md` to keep it lean. Read this file when working on the topics below.

### Template Import/Export
- **Import**: `POST /projects/{id}/import-template` — takes `template_yaml` dict, generates topology via `resolve_inline_template` + `generate_topology_from_template` (includes auto-layout), patches project in-place. Frontend validates YAML syntax and required sections (`vms`, `networks`) before sending. Only works on `draft` projects.
- **Export**: `GET /projects/{id}/export-template` and `GET /patterns/{id}/export-template` — reverse-maps canvas topology JSONB to simple `infra_template.yaml` format via `export_topology_to_template()`. Returns YAML with `text/yaml` content type. Includes OCP metadata, disconnected config, bastion services, and DNS records if present on the topology.
- **Inline templates**: `resolve_inline_template()` accepts template YAML from external sources (e.g. agnosticv `#include`) without needing files on disk
- **Round-trip**: import → edit on canvas → export produces valid template YAML that can be re-imported or used in agnosticv
- **Library item references**: disks can include `library_item_id` / `library_item_name` to reference a library image; VMs can include `pxe_boot_iso_id` / `pxe_boot_iso_name` for PXE boot ISOs. Import validates all referenced items exist in the DB. Blank disks (no `library_item_id`) create empty qcow2 at the specified `size_gb`.
- **Frontend UI**: "Import Template YAML" button on blank canvas opens paste/upload modal. "Export Template" button in action bar (next to MegaConsole/Save as Pattern) opens confirmation modal noting only infra topology is exported (not disk images — use Save as Pattern for that).
### Cloud-Init
- Seed ISO with NoCloud datasource (cidata volume label)
- `instance-id` must be unique per deploy (UUID suffix) for cloud-init to re-run
- `chpasswd` uses new `users:` format (not deprecated `list: |`)
- Custom user-data is YAML-validated before appending
- **SELinux** (GCP, Azure, OCP Virt): RHEL images have SELinux enforcing — cloud-init must run `semanage fcontext -a -t virt_image_t '/var/lib/troshka(/.*)?' && restorecon -R /var/lib/troshka` so QEMU can access disk images and symlinks. Without this, VMs fail to start with "Permission denied" on ISOs. AWS Amazon Linux does not have SELinux enforcing.
- **Firewalld** (GCP, Azure only): RHEL images on GCP/Azure have firewalld enabled — cloud-init must open ports 31337 (agent) and 443 (console) with `firewall-cmd --add-port=31337/tcp --add-port=443/tcp --permanent && firewall-cmd --reload`. AWS uses security groups (no host firewall), OCP Virt uses OCP Routes — neither needs firewalld rules.
- **Chrony NTP**: when `gateway_ip` is set on VM data, cloud-init writes `/etc/chrony.conf` pointing at the gateway and restarts chronyd. VMs never use public NTP pools — the gateway namespace runs chrony as the authoritative time source.
### Deploy Pipeline
- Parallel VM deployment: disk creation, VM definition, and start run concurrently per VM
- Progress: byte-level download tracking with active transfer detail
- External access toggle: `externalAccess` on gateway node — when off, no EIPs or port forwards are provisioned (gateway stays for outbound NAT)
- Topology templates: predefined OCP templates with version dropdown, deploy time estimates, auto-sizing from install results
- **Pattern deploy `common_password`**: `PatternDeployRequest` accepts `common_password` to override BMC and cloud-init credentials baked in the pattern's topology. Without this, pattern-deployed projects get the original builder's password instead of the current deployment's. Overrides `bmcPassword` on BMC networks and `ciCloudUserPassword` on cloud-init VMs.
- **Pattern deploy showroom overrides**: `PatternDeployRequest.showroom` accepts optional `content_repo`, `content_ref`, and `build_content`. Use when deploying from a pattern with baked showroom HTML but the catalog item should pull a newer git tag (or different repo). Changing repo or ref defaults `build_content` to `true` so git-cloner and Antora run at deploy; pass `build_content: false` explicitly to keep the pattern disk snapshot only.
### Clock Backdating
- **Project-level setting**: `Project.clock_target` (DateTime, nullable) — all VMs in a project share one target datetime
- **Hypervisor offset**: `--clock offset=variable,adjustment=N` in virt-install — guest sees target time from BIOS/UEFI, ticks forward in real time
- **Offset calculation**: `int((clock_target - now_utc).total_seconds())` — negative for past dates
- **Gateway NTP**: chronyd runs per-project in the gateway namespace (`ip netns exec`), serves `local stratum 3` — VMs sync from gateway only
- **Cloud-init**: all VMs get chrony pointing at gateway IP with `makestep 1 -1` (immediate step on any offset)
- **Template YAML**: top-level `clock_target: "2025-01-15T00:00:00Z"` — imported to Project model, exported back
- **Live adjustment**: PATCH `clock_target` on active project triggers `adjust_clocks_async()` — updates libvirt XML + pushes time via guest-agent/exec fallback
- **`/vms/set-clock` endpoint**: troshkad handler for live clock updates (XML + time push)
- **Pattern integration**: optional "Capture clock target" checkbox in SavePatternModal — saves `clock_target` on Pattern model, restored on deploy
- **Frontend**: Clock toggle + datetime picker in Palette (Project section) — toggle shows/hides picker, explicit "Set" button to apply
- **Service**: `src/backend/app/services/clock_service.py` — `compute_clock_offset()`, `adjust_clocks_async()`
### Pull-Through Registry
- **Settings toggle**: User model `pull_through_registry` bool + `pull_through_registry_url`, `_user`, `_password` columns
- **Frontend**: Switch on settings page under OCP Pull Secret — when on, replaces pull secret textarea with URL/username/password fields
- **Pull secret construction**: backend builds `{"auths":{"<url>":{"auth":"<base64(user:pass)>"}}}` from the three fields
- **OCP deploy injection**: in `/from-template`, if user has toggle enabled and template doesn't already have `pull_through_registry`, backend injects the config via `_build_pull_through_config()`
- **Priority**: agnosticv template `pull_through_registry` > user toggle > no config (direct pulls)
- **Config dict shape**: `{"enabled": True, "url": str, "orgs": {"registry.redhat.io": "registry_redhat_io", "quay.io": "quay_io"}}`
- **Org convention**: Quay proxy-cache standard — source registry dots replaced with underscores
- **What it enables**: `imageDigestSources` in install-config, `registries.conf` on bastions, podman mirror config — all handled by existing `agent_template.py` code
- **API**: `GET/PUT/DELETE /auth/ocp-pull-secret` (extended), `PATCH /auth/ocp-pull-secret` (new, toggle only)
### AgnosticD-v2 Integration
- **Architecture**: Babylon → AAP2 → agnosticd-v2 (with `troshka` cloud provider + bastion service roles) → Troshka API
- **Catalog items**: defined in agnosticv repo (`troshka/` directory), infrastructure topology in `infra_template.yaml` included via `#include`
- **Config**: `env_type: troshka` — agnosticd-v2 config at `ansible/configs/troshka/`
- **Three deploy modes** (`troshka_deploy_mode`):
  - `template` — full build: infra + bastion services (pre_software_workloads) + OCP + workloads (software_workloads)
  - `pattern` — deploy from saved snapshot, skip all workloads
  - `pattern_workloads` — deploy from snapshot, skip pre-software, run software workloads on top
- **`auto_install_ocp`**: boolean (default true) — when false, `software.yml` skips both `host_ocp4_agent_installer` and `host_ocp4_ibi_installer` roles. Used by IBI lab (students install manually) and non-OCP templates.
- **Pattern deploy `common_password`**: `infrastructure_deployment.yml` passes `common_password` to `project_deploy` module so baked credentials are overridden with the current GUID's password
- **Bastion service roles** (agnosticd-v2): `disconnected_registry`, `disconnected_mirror`, `bastion_gitea`, `bastion_minio`
- **Ansible collection**: `agnosticd.cloud_provider_troshka` — deploy role assembles `template_yaml` from agnosticv merged vars, calls `POST /projects/from-template` then `POST /projects/{id}/deploy`
- **agnosticv `#include`**: files inside catalog item dirs are treated as catalog items by default (causes recursion). Register non-catalog files like `infra_template.yaml` in `.agnosticv.yaml` `related_files` list to prevent this.
- **No catalog-item-specific Python** in Troshka — all lab config comes from YAML templates. Troshka engine is generic; catalog items live in agnosticv.
