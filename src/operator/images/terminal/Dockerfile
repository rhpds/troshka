# Cluster terminal image: a glibc (UBI9) wetty terminal with `oc` baked in, for
# the showroom "OpenShift Cluster Terminal" tab. Runs a LOCAL unprivileged shell
# (no SSH) with oc + the merged kubeconfig (injected onto the shared /showroom
# disk at deploy time). Replaces the old approach of fetching a glibc `oc` at
# deploy time into an Alpine (musl) wetty image, where oc could not exec (no
# glibc loader).

# ---- Builder stage: compile tmux from source ---------------------------------
# tmux is not packaged in UBI9's repos or EPEL, and UBI9 has no yacc/bison to
# build it in place either. Compile it on CentOS Stream 9 (same RHEL9 ABI as
# UBI9, Red Hat-published, and it DOES carry bison + the -devel packages) from
# the official upstream release tarball (pinned + sha256-verified), then COPY
# only the resulting binary into the UBI9 final image below. Demo developers
# require tmux (also matches the bastion's installer session).
FROM quay.io/centos/centos:stream9 AS tmux-builder
ARG TMUX_VERSION=3.5a
ARG TMUX_SHA256=16216bd0877170dfcc64157085ba9013610b12b082548c7c9542cc0103198951
RUN dnf install -y gcc make bison libevent-devel ncurses-devel pkgconf-pkg-config \
        tar gzip \
    && curl --proto =https -fsSL -o /tmp/tmux.tar.gz \
        "https://github.com/tmux/tmux/releases/download/${TMUX_VERSION}/tmux-${TMUX_VERSION}.tar.gz" \
    && echo "${TMUX_SHA256}  /tmp/tmux.tar.gz" | sha256sum -c - \
    && tar xzf /tmp/tmux.tar.gz -C /tmp \
    && cd "/tmp/tmux-${TMUX_VERSION}" \
    && ./configure && make -j"$(nproc)" && make install \
    && tmux -V

# ---- Final stage: UBI9 wetty + oc --------------------------------------------
FROM registry.access.redhat.com/ubi9/ubi:latest

ARG OCP_VERSION=stable
ARG WETTY_VERSION=2.5.0

# nodejs 20 (ubi9's default stream is 16, too old for wetty) + build toolchain
# (wetty's node-pty compiles native), then oc from the OpenShift mirror.
# procps-ng gives `watch`, ncurses gives `clear`. libevent + ncurses-libs are the
# runtime deps for the tmux binary copied from the builder stage. The wetty build
# toolchain is removed in this same layer so the final image stays gcc-free.
RUN dnf module -y reset nodejs \
    && dnf module -y enable nodejs:20 \
    && dnf install -y nodejs npm gcc-c++ make python3 tar gzip util-linux \
        procps-ng ncurses libevent \
    && curl --proto =https -fsSL \
        "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/${OCP_VERSION}/openshift-client-linux.tar.gz" \
        | tar xzf - -C /usr/local/bin oc kubectl \
    && chmod 0755 /usr/local/bin/oc /usr/local/bin/kubectl \
    && npm install -g "wetty@${WETTY_VERSION}" \
    && dnf remove -y gcc-c++ make \
    && dnf clean all \
    && rm -rf /var/cache/dnf

# Real tmux, compiled in the builder stage (no compiler ends up in this image).
COPY --from=tmux-builder /usr/local/bin/tmux /usr/local/bin/tmux

# Unprivileged lab user (uid 1000, no sudo). wetty must run as root to spawn a
# LOCAL shell (as non-root it falls back to SSH and prompts for a username), so
# the container runs as root and cluster-shell drops to this user via setpriv.
RUN useradd -u 1000 -m -d /home/labuser -s /bin/bash labuser

COPY cluster-shell /usr/local/bin/cluster-shell
RUN chmod 0755 /usr/local/bin/cluster-shell

# Global tmux defaults for the browser terminal (mouse scroll + scrollback).
COPY tmux.conf /etc/tmux.conf

ENTRYPOINT ["wetty"]
