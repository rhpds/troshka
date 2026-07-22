# Troshka Demo Video Script (5 minutes)

**Format:** Story-driven, internal stakeholders / leadership
**Tone:** Start with the pain, reveal the solution
**Target:** ~750 words narration, full A/V directions

---

## ACT 1: The Problem (0:00 – 0:50)

| TIME | NARRATION | ON SCREEN |
|------|-----------|-----------|
| 0:00 | Every hands-on lab we deliver starts the same way. Someone writes an Ansible playbook. It provisions cloud instances. It installs packages, configures networking, deploys OpenShift, sets up the lab content. | Timelapse of a terminal running an Ansible playbook — scrolling output, yellow "changed" lines flying by. Clock overlay fast-forwarding. |
| 0:15 | That process takes 45 minutes to 90 minutes — per environment. For a workshop with 50 students, that's 50 separate deploys, each one hoping nothing times out or fails halfway through. | Split screen: left shows a deploy log with a red FAILED line. Right shows a Slack message: "my lab isn't working." |
| 0:30 | And when the workshop is over? Those environments sit idle, burning money, until someone remembers to tear them down. We're paying for infrastructure that's doing nothing most of the time. | AWS billing dashboard with a cost graph trending upward. Highlight idle EC2 instances. |
| 0:45 | We built Troshka to fix this. | Cut to black. Troshka logo fades in, centered. Beat. |

---

## ACT 2: Design (0:50 – 1:50)

| TIME | NARRATION | ON SCREEN |
|------|-----------|-----------|
| 0:50 | Troshka is a self-service platform for building, deploying, and sharing nested VM environments. Everything starts on the canvas. | Troshka UI — projects page. Click "New Project," name it "OpenShift Lab." Canvas opens, empty. |
| 1:00 | You drag out the components you need — VMs, networks, gateways, storage — and wire them together visually. No YAML, no playbooks, no guessing at IP addresses. | Drag a gateway node onto the canvas. Drag a network. Connect them. Drag 3 VM nodes, connect each to the network. Drag storage disks onto VMs. Smooth, deliberate movements. |
| 1:20 | The properties panel lets you configure everything — CPU, memory, disk images, cloud-init, boot order, NIC models. Click a VM, set it up, move on. | Click a VM node. Properties panel slides open on the right. Set vCPU to 8, RAM to 32 GB, select a RHEL image from the library dropdown. Scroll to show cloud-init section. |
| 1:35 | You can also import an existing topology from YAML — round-trip between code and canvas. Teams that prefer infrastructure-as-code can export their canvas back to a template file. | Click "Import Template YAML" on an empty canvas. Paste a YAML snippet. Canvas populates with a full OCP topology — bastion, 3 control planes, workers, networks, all wired up. |

---

## ACT 3: Deploy (1:50 – 2:50)

| TIME | NARRATION | ON SCREEN |
|------|-----------|-----------|
| 1:50 | When the topology looks right, you hit Deploy. Troshka places the project on a host, downloads disk images, creates the VMs, wires the networks, and starts everything — in about five minutes. | Click the blue "Deploy" button. Deploy progress modal appears. Show stages ticking through: "Downloading images... Creating disks... Defining VMs... Starting VMs... Configuring DNS..." Progress bar advancing. |
| 2:10 | All of this runs on a single cloud instance. The VMs are nested inside it — fully isolated with their own overlay networks. No cross-account VPC peering, no security group sprawl. One host, one project, complete isolation. | Simple diagram animation: a cloud instance box containing smaller VM boxes, connected by internal network lines. Overlay text: "VXLAN overlay · network namespaces · nftables isolation." |
| 2:30 | Once it's running, you get a browser-based console for every VM. No VPN, no SSH keys to distribute. Students open a URL and they're in. | MegaConsole view — grid of 4-6 VM consoles, all showing login prompts or running desktops. Mouse clicks into one, types a command, output appears. |

---

## ACT 4: Patterns — The Multiplier (2:50 – 3:50)

| TIME | NARRATION | ON SCREEN |
|------|-----------|-----------|
| 2:50 | Here's where it gets interesting. Once you've built and configured an environment — installed OpenShift, deployed workloads, set up the lab content — you can save the entire thing as a pattern. | Canvas view of a running project. Click "Save as Pattern" in the toolbar. Modal appears, enter name "OCP 4.17 + ACM Lab." Click save. Progress indicator: "Capturing disks..." |
| 3:10 | A pattern is a snapshot — every disk, every VM, the full topology — compressed and stored in S3 for pennies a month. | Patterns library page. Grid of pattern cards with names, dates, disk counts. Highlight one showing "4 disks, 89 GB total." |
| 3:20 | Now deploying that same environment takes five to ten minutes instead of ninety. And it's bit-for-bit identical every time. No Ansible variability, no "works on my machine." Fifty students, fifty identical labs. | Click "Deploy from Pattern." Naming modal: enter "acm-workshop-{1..50}." Deploy starts. Fast-cut montage of progress bars completing. Projects page showing 50 projects in "active" state. |
| 3:40 | When the workshop ends, you tear them all down. The pattern stays. Next month, deploy again — same five minutes, same cost. | Select all 50 projects. Click "Delete." Confirmation modal. Projects disappear. Cut to the pattern card — still there, unchanged. |

---

## ACT 5: Multi-Cloud and Scale (3:50 – 4:30)

| TIME | NARRATION | ON SCREEN |
|------|-----------|-----------|
| 3:50 | Troshka runs on AWS, GCP, Azure, and OpenShift Virtualization — same UI, same API, same patterns. You pick the cloud that fits your budget or your customer's requirements. | Admin providers page showing 4 provider cards: AWS, GCP, Azure, OCP Virt. Each with a green "connected" badge. |
| 4:05 | Hosts scale up and down with demand. Provision them for a workshop, tear them down after. No idle infrastructure. Auto-stop timers shut down forgotten environments before they burn budget. | Hosts admin page. Show capacity bars (vCPU, RAM, storage). Mouse hovers over auto-stop timer badge on a project. Tooltip: "Auto-stop in 2h 15m." |
| 4:15 | For teams with shared storage, live migration moves running projects between hosts with zero downtime. Evacuate a host for maintenance without interrupting a single student. | Terminal or admin UI showing a migration in progress. "Migrating project... VMs: 3/3 complete." Host A goes from 3 projects to 2, Host B from 1 to 2. |

---

## ACT 6: The Takeaway (4:30 – 5:00)

| TIME | NARRATION | ON SCREEN |
|------|-----------|-----------|
| 4:30 | So here's what changed. | Cut to clean comparison slide. |
| 4:33 | Deploy time went from 45–90 minutes to under ten. | Animated stat: "90 min → 5 min" with a downward arrow. |
| 4:38 | Cost went from always-on instances to on-demand plus cold storage. | Animated stat: "24/7 EC2 → deploy on demand, patterns in S3." |
| 4:43 | Reliability went from "run the playbook and hope" to bit-for-bit identical snapshots, every time. | Animated stat: "Ansible variability → identical patterns." |
| 4:48 | And scale went from "how many can we babysit" to self-service. Lab authors design it once. Students deploy it themselves. | Animated stat: "Manual provisioning → self-service portal." |
| 4:55 | That's Troshka. | Troshka logo, centered. Tagline below: "Design. Deploy. Repeat." |
| 4:58 | | URL or QR code to internal demo instance. Fade to black. |

---

## Production Notes

- **Total narration:** ~720 words (~4:50 at natural pace, leaves 10s for pauses and transitions)
- **Screen recordings needed:**
  1. Projects page → create project → canvas (Act 2)
  2. Canvas drag-and-drop with properties panel (Act 2)
  3. Template YAML import populating canvas (Act 2)
  4. Deploy progress modal, start to finish (Act 3)
  5. MegaConsole with multiple running VMs (Act 3)
  6. Save as Pattern flow (Act 4)
  7. Bulk deploy from pattern (Act 4)
  8. Bulk delete projects (Act 4)
  9. Providers admin page showing multi-cloud (Act 5)
  10. Hosts admin page with capacity bars (Act 5)
- **Graphics/animations needed:**
  1. Troshka logo (exists in repo)
  2. Nested VM architecture diagram (Act 3) — simple box-in-box
  3. Comparison stats slides (Act 6) — 4 animated stat lines
- **B-roll suggestions:**
  - Terminal with Ansible scrolling (Act 1 — can be staged)
  - AWS billing dashboard (Act 1 — screenshot or mockup is fine)
  - Slack "my lab isn't working" message (Act 1 — staged)
- **Music:** Low-key background track. Tension in Act 1, resolve at the logo reveal (0:45), energy through Acts 2-5, clean finish in Act 6.
