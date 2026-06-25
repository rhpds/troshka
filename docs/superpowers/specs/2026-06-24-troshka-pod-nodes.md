# Troshka Pod Nodes

**Date**: 2026-06-24
**Status**: Draft
**Goal**: Add pod support to Troshka — groups of containers sharing a network namespace, deployed via `podman pod`.

## Background

Troshka currently supports single-container nodes. Some workloads (showroom, multi-service apps) need multiple containers that share localhost — like a Kubernetes pod. Podman natively supports pods (`podman pod create`), making this a natural extension.

## Use Case: Showroom

Instead of installing showroom on a bastion VM (slow, complex), run it as a pod:

```yaml
containers:
  showroom:
    type: pod
    nics:
      - network: cluster
        ip: 10.0.0.100
    init_containers:
      - name: git-cloner
        image: quay.io/rhpds/showroom-git-cloner:latest
        env:
          GIT_REPO_URL: "{{ showroom_git_repo }}"
          GIT_REPO_REF: main
      - name: antora-builder
        image: quay.io/rhpds/antora:v1.2.4
    containers:
      - name: nginx
        image: quay.io/rhpds/nginx:1.25
        ports: [80]
      - name: content
        image: quay.io/rhpds/showroom-content:v1.4.2
        ports: [8080]
      - name: wetty
        image: quay.io/rhpds/wetty:v2.7.6
        ports: [3000]
        env:
          SSH_HOST: 10.0.0.50
          SSH_USER: lab-user
          SSH_PASS: "{{ common_password }}"
```

## Design

### Canvas
- New node type: `podNode` — displays like a VM but with a container icon
- Shows grouped containers inside the node
- Connects to networks like VMs (gets an IP via veth)

### Template Format
- `containers:` section already exists for single containers
- Add `type: pod` to indicate a pod with sub-containers
- `init_containers:` for one-time setup (git clone, build)
- `containers:` for long-running services

### Troshkad
- `podman pod create --name {pod_name} --network none` (veth attachment like single containers)
- Init containers run sequentially, then main containers start
- Shared volumes between init and main containers
- Pod gets one IP on the bridge (all containers share it)

### Deploy Service
- Pod nodes deployed in the container step (after networks)
- Init containers run first (blocking), then main containers
- Health check: all main containers running

## References
- OCP showroom Helm chart: `rhpds/showroom-deployer` (`showroom-single-pod`)
- Container images: `quay.io/rhpds/showroom-content`, `wetty`, `nginx`, `antora`
- Podman pod docs: `podman-pod-create(1)`
