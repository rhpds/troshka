# Deployment (Helm / Kustomize / Ansible) Reference

> Extracted from the top-level `CLAUDE.md` to keep it lean. Read this file when working on the topics below.

## Deployment

Three deployment methods — all produce equivalent results:
### Helm Chart (`deploy/helm/`)
```bash
helm install troshka deploy/helm/ -n troshka \
  --set postgres.deploy=true \
  --set auth.oauthEnabled=true \
  --set auth.allowedGroups="rhpds-admins\,troshka-users" \
  --set auth.adminGroups="rhpds-admins" \
  --set route.host=troshka.apps.cluster.example.com
```
- Full-stack chart: backend, frontend, PostgreSQL, S4, OAuth proxy, RBAC, migration Job
- All components conditional via `values.yaml` toggles (`postgres.deploy`, `s4.deploy`, `auth.oauthEnabled`)
- Global `deploy: false` suppresses all resources (ArgoCD pattern)
- Migration runs as Helm pre-install/pre-upgrade hook
- Secrets auto-generated on first install, preserved on upgrade (`helm.sh/resource-policy: keep`)
- ConfigMap changes trigger pod restart via `checksum/config` annotation
- **OAuth proxy has NO auto-rollout** — changing `--skip-auth-regex` or other args requires a manual `oc rollout restart deployment/troshka-oauth-proxy`. Do NOT add a checksum annotation to the OAuth proxy pod template — the template contains `randAlphaNum` for secrets, so any re-render regenerates the client-secret and breaks OAuth login.
### Kustomize (`deploy/base/` + `deploy/overlays/`)
```bash
oc apply -k deploy/overlays/postgres   # base + in-cluster PostgreSQL
oc apply -k deploy/overlays/sso        # base + OAuth proxy
oc apply -k deploy/overlays/s4         # base + in-cluster S4
```
- Base: namespace, backend (Deployment + Service + ConfigMap + Secret), frontend (Deployment + Service + Route), RBAC (ServiceAccount + ClusterRole + ClusterRoleBinding)
- Overlays compose additively — apply multiple overlays by creating a new overlay that references them
### Ansible (`deploy/ansible/`)
```bash
ansible-playbook deploy/ansible/deploy.yaml \
  -e kubeconfig=~/secrets/cluster.kubeconfig \
  -e troshka_deploy_postgres=true \
  -e troshka_oauth_enabled=true \
  -e troshka_allowed_groups="rhpds-admins,troshka-users"
```
- Variables in `deploy/ansible/inventory/group_vars/all.yaml`
- Task order: namespace → RBAC → secrets → PostgreSQL → S4 → backend → migration → frontend → OAuth
- Secrets auto-generated on first deploy, preserved on re-deploy
- Undeploy: `ansible-playbook deploy/ansible/undeploy.yaml`
### Container Images
- Built by GitHub Actions on push to `main` or version tags
- Images at `quay.io/redhat-gpte/troshka-{backend,frontend,operator,dnsmasq,gateway,tools,bmc,vnc-proxy}`
- Containerfiles in `deploy/containerfiles/` (backend, frontend) and `src/operator/images/` (operator components)
