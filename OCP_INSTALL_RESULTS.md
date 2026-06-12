# OCP 4.20 Agent-Based Install Results

## Summary

OpenShift 4.20.24 compact 3-node cluster successfully installed using the agent-based installer on Troshka nested virtualization (EC2 instance → libvirt VMs).

**Install method:** Agent-based installer (no nested VMs, no IPI bootstrap)
**Date:** 2026-06-12
**OCP Version:** 4.20.24
**Template:** OpenShift Compact 3-Node (Agent Installer)

## Timeline

| Phase | Duration |
|---|---|
| Agent ISO creation | ~2 min |
| CPs boot from ISO | ~1 min |
| Disk write (all 3 CPs) | ~5 min |
| CPs reboot from disk | ~2 min |
| Bootstrap API initialized | ~3 min |
| Cluster operators deploying | ~20 min |
| **Total install time** | **~35 min** |

## Disk Usage

| VM | Disk Used | Provisioned |
|---|---|---|
| Bastion (qcow2) | 4.7 GB | 50 GB |
| Bastion RHEL DVD ISO | 11 GB | — |
| cp-0 | 15 GB | 120 GB |
| cp-1 | 23 GB | 120 GB |
| cp-2 | 26 GB | 120 GB |
| **Total** | **~80 GB** | **410 GB** |

## CPU/RAM Usage

| VM | vCPU | RAM | CPU Time |
|---|---|---|---|
| Bastion | 2 | 4 GB | 139s |
| cp-0 | 8 | 16 GB | 5,470s |
| cp-1 | 8 | 16 GB | 3,751s |
| cp-2 | 8 | 16 GB | 6,220s |
| **Total** | **26** | **52 GB** | |

## Rightsizing Recommendations

### CP Nodes
- **Disk:** 50 GB sufficient (max observed: 26 GB). 120 GB is overkill for labs.
- **RAM:** 16 GB is OCP minimum — cannot reduce.
- **vCPU:** 4 is OCP minimum. 8 gives faster install but 4 works for labs.

### Bastion
- **Disk:** 20 GB sufficient (4.7 GB used + ISO cache).
- **RAM:** 4 GB is fine — just runs installer and serves ISO.
- **vCPU:** 2 is sufficient.

### Optimized Template (for labs)
| VM | vCPU | RAM | Disk |
|---|---|---|---|
| Bastion | 2 | 4 GB | 20 GB |
| CP (×3) | 4 | 16 GB | 50 GB |
| **Total** | **14** | **52 GB** | **170 GB** |

vs. current template:
| VM | vCPU | RAM | Disk |
|---|---|---|---|
| Bastion | 2 | 4 GB | 150 GB |
| CP (×3) | 8 | 16 GB | 120 GB |
| **Total** | **26** | **52 GB** | **510 GB** |

**Savings: 12 vCPU, 340 GB disk per cluster.**

## Access

- **Console:** https://console-openshift-console.apps.ocp.ocp.local
- **API:** https://api.ocp.ocp.local:6443
- **kubeadmin password:** (generated per install, see install.log)
- **kubeconfig:** ~/ocp-install/auth/kubeconfig

## Key Findings

1. **IPI doesn't work on EC2 instances** — triple-nested virt (EC2 → libvirt → libvirt bootstrap) causes kernel panics at fw_cfg_read_blob. Agent-based installer avoids this entirely.
2. **Boot order matters** — CPs need `hd,cdrom` boot order so they boot from disk after agent writes CoreOS, not re-boot the ISO.
3. **NTP required** — `additionalNTPSources` in agent-config prevents validation failures.
4. **Pull secret must be valid** — expired robot accounts cause cascading errors.
5. **Sushy virtual media** needs `SUSHY_EMULATOR_STORAGE_POOL` (not `VMEDIA_STORAGE_POOL`) and per-project libvirt storage pools.
6. **dnsmasq resilience** — 5-second watchdog, per-project config files, startup restore.
