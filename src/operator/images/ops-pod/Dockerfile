# Troshka ops-pod execution environment (EE)
#
# The in-cluster *ops pod* (Plan 4) runs the OpenShift agent-based install for
# one or more clusters entirely from inside the project network — no bastion VM.
# This image bundles everything the install runner (Task 5) shells out to:
#   - oc / openshift-install (agent create image, wait-for)
#   - ansible-core + ansible-navigator + the troshka.cloud collection
#   - curl (Redfish virtual-media / power control) + jq
#   - python3 (serves the agent ISO over http.server) + bind-utils (dig/nslookup)
#
# BUILT BY CI ONLY — never `podman build` locally (project convention). CI pins
# the OpenShift client/installer via `--build-arg OCP_VERSION=<x.y.z>`.
FROM registry.access.redhat.com/ubi9/python-311:9.5

# OpenShift client + installer version to bake in. "stable" tracks the latest
# stable release; CI overrides with an exact version (e.g. 4.20.0) for a pinned,
# reproducible image. The install runner may still re-download a per-project
# version at runtime, but this default keeps the image self-sufficient.
ARG OCP_VERSION=stable
ARG OCP_MIRROR=https://mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp

USER 0
WORKDIR /workdir

# Redfish/curl tooling, DNS lookups, and the git client for the collection pull.
# ubi9 ships curl; add jq, bind-utils, tar, gzip, openssh-clients, git.
RUN dnf install -y --setopt=install_weak_deps=False \
        jq \
        bind-utils \
        tar \
        gzip \
        openssh-clients \
        git \
    && dnf clean all \
    && rm -rf /var/cache/dnf

# oc + kubectl and openshift-install, fetched at build from the OCP mirror.
RUN curl --proto '=https' --tlsv1.2 -fsSL \
        "${OCP_MIRROR}/${OCP_VERSION}/openshift-client-linux.tar.gz" \
        | tar xzf - -C /usr/local/bin oc kubectl \
    && curl --proto '=https' --tlsv1.2 -fsSL \
        "${OCP_MIRROR}/${OCP_VERSION}/openshift-install-linux.tar.gz" \
        | tar xzf - -C /usr/local/bin openshift-install \
    && chmod +x /usr/local/bin/oc /usr/local/bin/kubectl /usr/local/bin/openshift-install \
    && oc version --client \
    && openshift-install version

# Ansible runtime + the troshka.cloud collection.
# The collection lives in a separate repo (github.com/rhpds/troshka-ansible-collection,
# namespace `troshka`, name `cloud` -> troshka.cloud). Installed from git so the
# image tracks the published source; a Galaxy/Automation-Hub ref can replace the
# git URL once the collection is published there.
ARG TROSHKA_COLLECTION_GIT=git+https://github.com/rhpds/troshka-ansible-collection.git
RUN pip install --no-cache-dir \
        ansible-core \
        ansible-navigator \
    && ansible-galaxy collection install "${TROSHKA_COLLECTION_GIT}" \
    && ansible --version \
    && ansible-galaxy collection list 2>/dev/null | grep -i troshka.cloud

# Keep the pod alive; the install runner (Task 5) execs the generated script.
CMD ["sleep", "infinity"]
