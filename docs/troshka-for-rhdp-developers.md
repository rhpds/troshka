# Troshka for RHDP Catalog Item Developers

## What Is Troshka?

Troshka is RHDP's nested virtualization platform. It creates complete lab environments — VMs, networks, storage, BMC endpoints, containers — inside host VMs running on AWS, GCP, Azure, or OpenShift Virtualization. A single Troshka host can run an entire multi-node lab (e.g., a 6-node OCP cluster with bastion, DNS, and load balancer) that would otherwise require 8+ separate cloud instances.

Think of it as "a data center in a VM." Students get a fully isolated environment with its own networking, DNS, DHCP, and console access — all running on one or two cloud instances behind the scenes.

## Why Use Troshka for Your Catalog Item?

### Cost

Traditional RHDP catalog items provision one cloud instance per VM. An OCP 4 cluster lab might need 8 EC2 instances running for 4-8 hours. With Troshka, that same lab runs inside a single large instance (or two for multi-host), cutting cloud costs by 60-80%.

### Speed

**Pattern deploys take 2-5 minutes, not 60-90 minutes.** Build your lab once, save it as a pattern (full disk snapshots stored in S3), and every subsequent order deploys from that snapshot. Students get a running OCP cluster in minutes instead of waiting over an hour for an install.

Three deploy modes let you choose the right tradeoff:

| Mode | What Happens | Deploy Time | Use When |
|------|-------------|-------------|----------|
| `template` | Full build: VMs + bastion services + OCP install + workloads | 60-90 min | First-time setup, building a new pattern |
| `pattern` | Deploy from saved snapshot, skip all workloads | 2-5 min | Lab is self-contained, no post-deploy config needed |
| `pattern_workloads` | Deploy from snapshot, then run software workloads on top | 5-15 min | Pattern provides base infra, workloads customize per order |

### Reproducibility

Patterns are immutable disk snapshots. Every student gets a byte-identical environment. No more "it worked when I built it" failures from package updates, mirror outages, or API changes between when you developed the lab and when a student orders it.

### Capabilities Not Available with Traditional Cloud Providers

| Capability | What It Enables |
|-----------|----------------|
| **Virtual BMC (IPMI + Redfish)** | Baremetal simulation — students can power-cycle VMs via `ipmitool` or Redfish API, attach virtual media, change boot order. Enables ACM, ZTP, and IBI labs without physical hardware. |
| **PXE Network Boot** | Boot VMs from network — auto-extracts kernel/initrd from RHEL/Ubuntu ISOs. Students experience real PXE workflows. |
| **Clock Backdating** | Set all VMs to a past date at the hypervisor level. Certificates, licenses, and time-sensitive software behave as if it's that date. |
| **Isolated L2 Networking** | Every project gets its own network namespace with real DHCP, DNS, and NAT. VMs communicate on a true L2 segment — ARP, broadcast, multicast all work. No cloud VPC limitations. |
| **Browser VNC Console** | Students access VM consoles directly in the browser via noVNC. No SSH keys, no bastion hopping — just click and type. |
| **Multi-Host Mesh** | Labs too large for one host automatically span multiple hosts with encrypted WireGuard tunnels. L2 adjacency preserved via VXLAN inside the tunnel — VMs don't know they're on different hosts. |
| **Containers & Pods** | Run podman containers and pods alongside VMs on the same networks. Mix containerized services with traditional VMs. |

## How It Works — Two Paths, One Platform

### Path 1: Visual Canvas (Interactive Design)

The Troshka web UI provides a drag-and-drop topology canvas built on React Flow. You can:

- Add VMs, networks, storage, containers visually
- Configure CPU, memory, disks, NICs, boot order per VM
- Connect VMs to networks by dragging edges
- Deploy and watch progress in real time
- Open VNC consoles to any VM from the browser
- Save the running environment as a reusable pattern

**Best for:** Prototyping a new lab, one-off demos, exploring what's possible before committing to YAML.

Once you're happy with the topology, **Export Template** produces a clean `infra_template.yaml` you can use in your catalog item.

### Path 2: YAML Templates + Ansible (CI/CD Automation)

This is how production catalog items work. No UI interaction required.

**`infra_template.yaml`** is a declarative YAML file that defines your entire lab topology:

```yaml
# Example: 3-node OCP cluster with bastion
networks:
  - name: ocp-network
    cidr: 192.168.50.0/24
    dhcp: true
    dns: true
    gateway: true
    external_access: true

vms:
  - name: bastion
    cpus: 2
    memory_mb: 4096
    disks:
      - size_gb: 50
    nics:
      - network: ocp-network
    cloud_init:
      user: lab-user
      password: "{{ common_password }}"

  - name: master-1
    cpus: 8
    memory_mb: 32768
    disks:
      - size_gb: 120
        library_item_name: rhcos-4.17
      - size_gb: 100
    nics:
      - network: ocp-network
    firmware: uefi

  - name: master-2
    # ... (same pattern)

  - name: master-3
    # ... (same pattern)

containers:
  - name: registry
    type: container
    image: docker.io/library/registry:2
    network: ocp-network
    ports:
      - "5000:5000"
```

This YAML is included in your AgnosticV catalog item via `#include` and consumed by the Ansible collection at deploy time.

**The Ansible Collection** (`agnosticd.cloud_provider_troshka`) handles the full lifecycle:

1. Reads your `infra_template.yaml` from AgnosticV merged vars
2. Calls `POST /projects/from-template` to create the project
3. Calls `POST /projects/{id}/deploy` to start deployment
4. Polls for completion
5. Returns connection details (IPs, passwords, console URLs) as Ansible facts

**The AgnosticD Cloud Provider** (`env_type: troshka`) makes Troshka a first-class citizen in the agnosticd-v2 pipeline:

```yaml
# In your AgnosticV catalog item
env_type: troshka
cloud_provider: troshka

# Deploy mode: template, pattern, or pattern_workloads
troshka_deploy_mode: pattern

# Pattern ID (from a saved pattern)
troshka_pattern_id: "abc123-def456"

# Bastion services run as pre_software_workloads
pre_software_workloads:
  - role: disconnected_registry
  - role: bastion_gitea

# Application workloads run as software_workloads
software_workloads:
  - role: my_custom_workload
```

**Deploy flow:**

```
Babylon order
  → AAP2 job
    → agnosticd-v2 (env_type: troshka)
      → Ansible collection calls Troshka API
        → VMs + networks created on Troshka host
          → Bastion services configured (pre_software_workloads)
            → OCP installed (if auto_install_ocp: true)
              → Your workloads run (software_workloads)
```

This is the same Babylon → AAP2 → agnosticd pipeline used by every other cloud provider. Swapping to Troshka requires changing `env_type` and adding an `infra_template.yaml` — your existing workload roles work unchanged.

### Round-Trip Workflow

The two paths are fully interoperable:

1. **Design visually** → Export Template → get `infra_template.yaml`
2. **Write YAML** → Import Template → see it on the canvas, tweak visually
3. **Build once** → Save as Pattern → deploy in minutes via Ansible

## What Catalog Items Use Troshka Today?

- **OpenShift 4 Cluster Labs** — IPI and UPI installs with configurable node counts
- **Image Based Install (IBI)** — Single-node OpenShift from disk image, with virtual BMC
- **5G RAN / ZTP Labs** — ACM hub + 3 SNO clusters, Redfish-driven bare metal provisioning
- **OSAC / Sovereign Cloud** — Multi-node isolated cloud environments
- **Disconnected OCP** — Air-gapped installs with local registry mirrors

## Getting Started

### 1. Design Your Topology

Either use the Troshka UI canvas at your instance URL, or write an `infra_template.yaml` directly. Start from an existing template — ask the RHDP Infra team for examples similar to your lab.

### 2. Build and Test

Order your catalog item in dev mode (`template` deploy mode). This builds everything from scratch. Verify your lab works end to end.

### 3. Save a Pattern

Once the lab is working, save it as a pattern. This captures full disk snapshots of every VM. The pattern is stored in S3 and can be deployed to any Troshka host.

### 4. Switch to Pattern Deploy

Update your AgnosticV config to use `troshka_deploy_mode: pattern` with the pattern ID. Deploy time drops from 60+ minutes to under 5 minutes.

### 5. Add Workloads (Optional)

If you need per-order customization on top of the pattern, use `pattern_workloads` mode. The pattern deploys first, then your `software_workloads` roles run on top.

## Key Differences from Traditional Catalog Items

| Aspect | Traditional (AWS/GCP/Azure) | Troshka |
|--------|---------------------------|---------|
| **Cloud instances per lab** | 1 per VM (3-8 instances) | 1-2 hosts for the whole lab |
| **Networking** | Cloud VPC, security groups | Real L2 with DHCP/DNS/NAT |
| **Deploy time (fresh)** | 60-90 min | 60-90 min (same) |
| **Deploy time (pattern)** | N/A | 2-5 min |
| **BMC / baremetal sim** | Not possible | Full IPMI + Redfish |
| **PXE boot** | Not possible | Supported |
| **Console access** | SSH only | Browser VNC + SSH |
| **Cost per lab-hour** | $$$ (multiple instances) | $ (single host, shared) |
| **AgnosticD integration** | `env_type: ocp4-cluster` etc. | `env_type: troshka` |
| **Workload roles** | Same | Same (unchanged) |

## Architecture at a Glance

```
┌─ RHDP Catalog ─────────────────────────────────────────────┐
│  Babylon → AAP2 → agnosticd-v2 (env_type: troshka)         │
│                        │                                     │
│              Ansible Collection                              │
│         (cloud_provider_troshka)                             │
│                        │                                     │
│                   Troshka API                                │
│              (FastAPI backend)                               │
│                        │                                     │
│         ┌──────────────┼──────────────┐                      │
│         ▼              ▼              ▼                      │
│    AWS Hosts     GCP Hosts     OCP Virt Hosts                │
│   (EC2 metal)   (N2 nested)   (KubeVirt VMs)                │
│         │              │              │                      │
│    troshkad       troshkad       troshkad                    │
│   (host agent)   (host agent)   (host agent)                │
│         │              │              │                      │
│    ┌────┴────┐    ┌────┴────┐    ┌────┴────┐                 │
│    │ libvirt │    │ libvirt │    │ libvirt │                  │
│    │  VMs    │    │  VMs    │    │  VMs    │                  │
│    │  nets   │    │  nets   │    │  nets   │                  │
│    │  BMC    │    │  BMC    │    │  BMC    │                  │
│    └─────────┘    └─────────┘    └─────────┘                 │
└─────────────────────────────────────────────────────────────┘
```

## Questions?

Contact the RHDP Infrastructure team (Slack: #forum-rhdp-infra) or file a GPTEINFRA Jira ticket.
