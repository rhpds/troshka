# Network Automation Workshop template

Deployable Troshka template: `net-automation-workshop.yaml`

Bootstrap, router day-0 config, showroom overlays, and pattern-build workflow
live in **demo_workloads**:

- Playbook: `~/demo_workloads/playbooks/net-automation-workshop/main.yml`
- Role: `~/demo_workloads/roles/troshka_workload_net_automation_workshop/`
- Docs: `~/demo_workloads/playbooks/net-automation-workshop/PATTERN-BUILD.md`

## Bootstrap notes

After deploy, run the Ansible bootstrap from `demo_workloads` (not from this
repo). Key gotchas discovered in practice:

1. **Inventory filename** — the `troshka.cloud.troshka` plugin only loads
   `*.troshka.yml` files. Use
   `.generated/${TROSHKA_PROJECT_ID}/inventory.troshka.yml`, not
   `troshka_inventory.yml`.
2. **RHSM** — required for vscode package install on unregistered `rhel-9.6`
   images. Portal creds (`redhat_username`/`redhat_password`) in
   `~/agnosticv/includes/secrets/aap2-casc-registry-creds.yaml` — not satellite.
   Original zt-ansiblebu CI applies demosat content views at provision time instead.
3. **Showroom networking** — showroom listens on the transit infra IP;
   gateway 80/443→infra port-forwards are injected at deploy.
