# Cluster terminal image: a glibc (UBI9) wetty terminal with `oc` baked in, for
# the showroom "OpenShift Cluster Terminal" tab. Runs a LOCAL unprivileged shell
# (no SSH) with oc + the merged kubeconfig (injected onto the shared /showroom
# disk at deploy time). Replaces the old approach of fetching a glibc `oc` at
# deploy time into an Alpine (musl) wetty image, where oc could not exec (no
# glibc loader).
FROM registry.access.redhat.com/ubi9/ubi:latest

ARG OCP_VERSION=stable
ARG WETTY_VERSION=2.5.0

# nodejs (for wetty) + build toolchain (wetty's node-pty compiles native), then
# oc from the OpenShift mirror. Build tools are removed after the global install
# to keep the image lean.
RUN dnf install -y nodejs npm gcc-c++ make python3 tar gzip \
    && curl --proto =https -fsSL \
        "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/${OCP_VERSION}/openshift-client-linux.tar.gz" \
        | tar xzf - -C /usr/local/bin oc kubectl \
    && chmod 0755 /usr/local/bin/oc /usr/local/bin/kubectl \
    && npm install -g "wetty@${WETTY_VERSION}" \
    && dnf remove -y gcc-c++ make \
    && dnf clean all \
    && rm -rf /var/cache/dnf

# Unprivileged lab user (uid 1000, no sudo). The interactive shell runs as this
# user; oc + the 0644 kubeconfig on the shared disk are readable by it.
RUN useradd -u 1000 -m -d /home/labuser -s /bin/bash labuser

COPY cluster-shell /usr/local/bin/cluster-shell
RUN chmod 0755 /usr/local/bin/cluster-shell

USER 1000
ENV HOME=/home/labuser
WORKDIR /home/labuser
ENTRYPOINT ["wetty"]
