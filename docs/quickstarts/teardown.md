# Troshka Full Wipe / Teardown

Every quickstart teardown script follows the same order so you do not delete the platform while Troshka still owns labs (and cloud VMs/disks/EIPs).

## Automated path

```bash
./quickstarts/local/teardown.sh      # or ocp/ / eks/
./quickstarts/<rail>/teardown.sh --yes
```

Shared engine (`quickstarts/lib/`):

1. **EKS only:** terminate seeded hosts (`DELETE /api/v1/hosts/{id}`), then delete compute IAM user + Secrets Manager secret  
2. **`wipe-workloads.sh`** — `DELETE /api/v1/projects/{id}` for every project; poll until the list is empty  
3. **`verify-clean.sh`** — refuse to continue if any project remains  
4. **Platform uninstall** — Compose `down -v` / Helm uninstall + namespace / CloudFormation delete  

If wipe times out, the script **exits without** removing the control plane. Fix stuck projects in the UI or API, then re-run teardown.

## What “wipe” covers

Destroying a project through Troshka’s API runs the normal destroy path: nested VMs, disks, lab networks, EIPs Troshka allocated, and related artifacts on the attached host/provider.

It does **not** delete:

- Cloud resources you created outside Troshka (manual EC2, unrelated VPCs)
- The control-plane stack itself (that is step 3)
- A macOS libvirt guest or WSL distro you created by hand (remove those yourself)

## Per-platform leftovers checklist

### Local (Compose)

- [ ] Teardown completed (`compose down -v`)
- [ ] Optional: delete `deploy/compose/.env` if you will not reinstall
- [ ] Optional: remove Homebrew libvirt Linux guest / WSL distro used only for the demo

### OpenShift

- [ ] Namespace `troshka` gone (`oc get ns troshka` → NotFound)
- [ ] If OAuth was enabled outside the quickstart, remove any leftover `OAuthClient` (see [install-ocp.md](../install-ocp.md))
- [ ] PVCs deleted with the namespace (confirm no stuck PVs)

### Amazon EKS

- [ ] CloudFormation stack deleted (`aws cloudformation describe-stacks` → does not exist)
- [ ] ingress-nginx / cert-manager Helm releases removed (teardown does this)
- [ ] Compute IAM user `<cluster>-compute` and secret `<cluster>/compute` removed
- [ ] No leftover Troshka host EC2 instances / `troshka-vpc` (seeded compute VPC — teardown deletes all `Name=troshka-vpc`)
- [ ] If stack delete stuck: look for ENIs, NLBs, or security groups still attached to the VPC; delete orphans; retry delete
- [ ] Optional: delete Route53 CNAME created for `--domain` if you no longer need it

## Manual wipe (if scripts cannot reach the API)

1. Open the Troshka UI while it is still up  
2. Delete/destroy every project; wait until none remain  
3. Run the platform uninstall half of the teardown script, or delete the stack/namespace/Compose project by hand  

Never delete the EKS CloudFormation stack or OCP namespace first — that orphans cloud VMs Troshka was managing.
