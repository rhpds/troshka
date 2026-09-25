# Troshka Quickstart — Amazon EKS

Greenfield install: **CloudFormation** creates a VPC + EKS cluster, then Helm deploys Troshka. One CLI command.

This stack runs the Troshka **control plane only**. Nested-VM hosts are not included — add compute after the UI is up.

## Prerequisites

- AWS account and credentials (`aws` CLI configured)
- [Helm 3](https://helm.sh/), `kubectl`, `curl`, `jq`
- IAM permissions below (AdministratorAccess works for demos)

## Permissions required to deploy

Attach `deploy/eks/iam-deployer-policy.json` to the deploying user/role, or use a broader admin policy for demos.

| Area | Why |
|---|---|
| **CloudFormation** | Create / update / delete the quickstart stack |
| **EC2 / VPC / EIP / NAT / SG** | Network for the cluster |
| **EKS** | Cluster, node groups, addons, access entries |
| **IAM** | Cluster/node roles, PassRole, OIDC provider hooks |
| **Elastic Load Balancing** | ALB for the Troshka Ingress |
| **STS GetCallerIdentity** | Script sanity check |
| **CloudWatch Logs** (optional) | Cluster / controller logging |

Teardown needs the same principal for stack delete. After wipe, stuck **ENIs / ALBs / security groups** can block CFN delete — the teardown script notes this.

## Install

```bash
export AWS_REGION=us-east-1   # or your region
./quickstarts/eks/install.sh
```

The script prints the AWS account, identity, and region (and where the region came from), then asks `[y/N]`. Default is **no**.

Non-interactive / CI (skip confirm, use resolved defaults):

```bash
./quickstarts/eks/install.sh --yes
./quickstarts/eks/install.sh --quiet
./quickstarts/eks/install.sh --no-verify
# or: TROSHKA_NO_VERIFY=1 ./quickstarts/eks/install.sh
```

What it does:

1. Create/update stack from `deploy/eks/cloudformation/troshka-eks.yaml`
2. `aws eks update-kubeconfig`
3. Install AWS Load Balancer Controller
4. `helm upgrade --install` with `deploy/helm/values-eks.yaml` (Ingress, oauth off, Postgres+S4)
5. Print the ALB hostname

Overrides: `TROSHKA_EKS_STACK`, `TROSHKA_EKS_CLUSTER`, `TROSHKA_NAMESPACE`, `TROSHKA_IMAGE_TAG`.

### Console “Launch Stack” (optional)

In the AWS Console → CloudFormation → Create stack → upload `deploy/eks/cloudformation/troshka-eks.yaml`, then run from step 2 of `install.sh` (or re-run `install.sh`, which updates an existing stack name).

## Compute (where lab VMs run)

Not in the CFN stack. After the UI is up:

1. Remote Linux + troshkad  
2. EC2 / Azure / GCP / KubeVirt (see [install-aws.md](../install-aws.md) and siblings)  
3. Local libvirt / WSL / macOS guest — [local.md](local.md)

## Teardown

```bash
./quickstarts/eks/teardown.sh
./quickstarts/eks/teardown.sh --yes
```

Order: API wipe of all projects → verify clean → Helm uninstall → delete CloudFormation stack (cluster + VPC).

Details: [teardown.md](teardown.md).
