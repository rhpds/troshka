# Troshka Quickstart — Amazon EKS

Greenfield install: **CloudFormation** creates a VPC + EKS cluster, then Helm deploys Troshka and seeds one EC2 compute provider + host. One CLI command.

## Prerequisites

- AWS account and credentials (`aws` CLI configured)
- [Helm 3](https://helm.sh/), `kubectl`, `curl`, `jq`
- IAM permissions below (AdministratorAccess works for demos)

## Permissions required to deploy

Attach `deploy/eks/iam-deployer-policy.json` to the deploying user/role, or use a broader admin policy for demos.

| Area | Why |
|---|---|
| **CloudFormation** | Create / update / delete the quickstart stack |
| **EC2 / VPC / EIP / NAT / SG** | Network for the cluster **and** Troshka compute VPC/host |
| **EKS** | Cluster, node groups, addons, access entries |
| **IAM** | Cluster/node roles, PassRole, OIDC, compute user + access keys |
| **Secrets Manager** | RDS/ElastiCache/S3/compute credential storage |
| **Elastic Load Balancing** | NLB for ingress-nginx |
| **Route53** | Optional `--domain` / `--production` public hostname |
| **Cognito** | `--production` OIDC user pool + admin user |
| **STS GetCallerIdentity** | Script sanity check |
| **CloudWatch Logs** (optional) | Cluster / controller logging |

Teardown needs the same principal for stack delete. After wipe, stuck **ENIs / NLBs / security groups** can block CFN delete — the teardown script notes this.

## Install

```bash
export AWS_REGION=us-east-1   # or your region
./quickstarts/eks/install.sh
```

**Default path (quickstart)**

1. CloudFormation VPC + EKS (+ EBS CSI with IRSA)
2. ingress-nginx (internet-facing NLB) + cert-manager
3. Hostname `troshka.<nlb-ip>.sslip.io` + Let's Encrypt (HTTP-01)
4. nginx **basic auth** (random admin password printed at the end)
5. Helm Troshka (in-cluster Postgres / **S4** / Redis; oauth off behind basic auth)
6. Seed EC2 provider + one host on a **Fedora Cloud** AMI (`m8i.xlarge`, 100 GiB)
7. Import **Fedora Cloud Generic qcow2** into the library via URL (host downloads → S4; no laptop upload)

**Public hostname (still basic auth)**

```bash
./quickstarts/eks/install.sh --domain troshka.example.com
```

Requires a public Route53 hosted zone that can hold that name (use a **subdomain**, not the zone apex — the script upserts a CNAME to the NLB).

**Production**

```bash
./quickstarts/eks/install.sh --production --domain troshka.example.com
# optional:
./quickstarts/eks/install.sh --production --domain troshka.example.com \
  --admin-email you@example.com
```

`--production` enables:

| Piece | What |
|---|---|
| RDS + S3 + ElastiCache | Instead of in-cluster data plane |
| Route53 CNAME | `--domain` → NLB |
| Let's Encrypt | Same cert-manager issuer |
| Cognito User Pool | Hosted UI; admin user created by the script |
| oauth2-proxy | OIDC in front of the UI; `auth.oauthEnabled=true` for the backend |
| Host AMI | **RHEL 9 Hourly** marketplace (`m8i.2xlarge`, 500 GiB) |

OpenShift **ose-oauth-proxy** is not used on EKS (`route.enabled=false`). OCP installs keep their own oauth path unchanged.

Object storage: default uses **in-cluster S4** (no Troshka `type=s3` provider required). `--production` / `--use-s3` switches the control plane to Amazon S3 via Helm secrets.

Managed data plane without full production auth:

```bash
./quickstarts/eks/install.sh --use-rds
./quickstarts/eks/install.sh --use-s3
./quickstarts/eks/install.sh --use-elasticache
```

Passwords/tokens are randomly generated (Helm `randAlphaNum` in-cluster; Secrets Manager for RDS/ElastiCache; IAM access keys for S3/compute; Cognito admin password printed once).

The script prints the AWS account, identity, region, KUBECONFIG, and auth/TLS mode, then asks `[y/N]`. Default is **no**.

If `KUBECONFIG` is unset, it warns and offers a dedicated file (`~/.kube/troshka-eks-<cluster>.yaml`). With `--yes` / `--quiet` / `--no-verify` it selects that path automatically.

```bash
./quickstarts/eks/install.sh --yes
./quickstarts/eks/install.sh --profile my-profile --region us-east-1
```

Overrides: `TROSHKA_EKS_STACK`, `TROSHKA_EKS_CLUSTER`, `TROSHKA_NAMESPACE`, `TROSHKA_DOMAIN`, `TROSHKA_ADMIN_EMAIL`, `TROSHKA_ADMIN_PASSWORD`, `TROSHKA_ACME_EMAIL`, `TROSHKA_EKS_PRODUCTION`, `TROSHKA_HOST_AMI`, `TROSHKA_HOST_INSTANCE_TYPE`, `TROSHKA_HOST_DISK_GB`.

### Auth matrix (do not mix on one release)

| Platform | Edge auth | Backend `oauth_enabled` | Helm gates |
|---|---|---|---|
| OCP + SSO | ose-oauth-proxy | true | `route.enabled` + `auth.oauthEnabled` + `oauth2Proxy` off |
| EKS default | nginx basic auth | false (auto-admin behind basic) | `ingress` + `basicAuth` |
| EKS `--production` | oauth2-proxy → Cognito | true | `ingress` + `oauth2Proxy.enabled` (no `route`) |

### Console “Launch Stack” (optional)

Upload `deploy/eks/cloudformation/troshka-eks.yaml`, then re-run `install.sh` (updates the same stack name) or continue from kubeconfig + Helm steps.

## Compute (where lab VMs run)

Install seeds an EC2 provider (`ec2-<cluster>`) and provisions one host:

| Mode | AMI | Instance | Disk |
|---|---|---|---|
| Default | Fedora Cloud Base (Fedora Project) | `m8i.xlarge` | 100 GiB |
| `--production` | RHEL 9 Hourly (marketplace) | `m8i.2xlarge` | 500 GiB |

Agent install continues in the background after the script exits — watch **Admin → Hosts** until `connected`. Compute VPC/SG are created via Troshka `create-vpc` (separate from the EKS VPC). IAM user `<cluster>-compute` + Secrets Manager `<cluster>/compute` hold the provider keys.

Library: install also registers an `s4-library` provider (in-cluster S4) and imports **Fedora Cloud 43** via `import-url` (troshkad on the seeded host pulls the official Fedora URL into S4). Skip with `TROSHKA_SKIP_FEDORA_IMAGE=1`. Override URL/name via `TROSHKA_FEDORA_QCOW_URL` / `TROSHKA_FEDORA_LIBRARY_NAME`.

Getting Started’s `test-web.yaml` prefers a RHEL library image (+ Binary DVD when present) and falls back to **Fedora Cloud 43**. A subscribed RHEL qcow cannot be auto-fetched; upload it to the library yourself if you want the RHEL path (name it `Prebuilt RHEL 10.2 Bastion`, or change the template).

**S4 from compute hosts:** EKS exposes S3 at `https://s4.<ingress.host>` (same NLB/LE as the UI). Backend pods keep using ClusterIP; troshkad jobs use `s3.host_endpoint_url`. The S3 API is still SigV4-authenticated. IP allowlisting at nginx is not reliable on AWS NLB with `externalTrafficPolicy=Cluster` (client IP is SNAT’d); do not enable `Local` or PROXY protocol on this quickstart NLB without re-validating sslip connectivity.

Further options: [install-aws.md](../install-aws.md), Azure/GCP/KubeVirt guides, or [local.md](local.md).

## Teardown

```bash
./quickstarts/eks/teardown.sh
./quickstarts/eks/teardown.sh --yes
```

Order: terminate hosts → API wipe of all projects (via backend port-forward) → verify clean → delete compute IAM user/secret → Helm uninstall Troshka → uninstall ingress-nginx + cert-manager → delete CloudFormation stack (cluster + VPC).

Details: [teardown.md](teardown.md).
