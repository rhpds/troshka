---
name: pattern-deploy-nic-bug
description: Pattern deploy creates VMs with missing NICs — bastion only gets BMC NIC, cluster NIC missing
metadata:
  type: project
---

Pattern deploy from golden image creates bastion with only 1 NIC (BMC bridge) instead of 2 (cluster + BMC). The cluster network NIC is in the topology but doesn't get passed to virt-install.

**Root cause**: `_find_vm_networks` in deploy_service.py resolves network connections by matching edge sourceHandle/targetHandle with NIC IDs. The pattern's `_remap_topology` generates new NIC IDs and updates edge handles, but the edge handle format might not match what `_find_vm_networks` expects.

**How to reproduce**: Save OCP project as pattern → deploy from pattern → bastion has only BMC NIC

**How to apply**: Check `_find_vm_networks` and `_remap_topology` handle ID format. The edge targetHandle format is `nic-{nic_id}-top` but after remap the nic_id changes. Verify the edge handles are updated consistently.

**Workaround**: Manually add the missing NIC via `virsh attach-interface` on the host.
