# infra/

Infrastructure configuration files applied during provider and host setup.

| File | Purpose | Used By |
|------|---------|---------|
| `iam-policy.json` | AWS IAM policy for the `troshka` user — grants EC2, EBS, VPC, S3, FSx, Route53 permissions | Manual IAM setup (see [AWS Install Guide](../docs/install-aws.md)) |
| `ocpvirt-rbac.yaml` | OpenShift RBAC manifest — creates namespace, ServiceAccount, ClusterRole, and binding for the OCP Virt provider | `oc apply -f` (see [OCP Virt Install Guide](../docs/install-ocpvirt.md)) |
| `troshka-fs-monitor.sh` | Host utility script — real-time storage viewer showing disk usage grouped by project | Installed to `/usr/local/bin/troshka-fs-monitor` by agent deployer; run with `sudo troshka-fs-monitor` on any host |
