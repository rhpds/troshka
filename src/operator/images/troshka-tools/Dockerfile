FROM quay.io/fedora/fedora:41

RUN dnf install -y \
    qemu-img \
    guestfs-tools \
    genisoimage \
    isomd5sum \
    python3 \
    python3-pip \
    awscli \
    openssh-clients \
    sshpass \
    libvirt-client \
    nmap-ncat \
    && dnf clean all

RUN dnf install -y \
    qemu-nbd \
    kmod \
    e2fsprogs \
    xfsprogs \
    util-linux \
    && dnf clean all

RUN curl -sL https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz \
    | tar xzf - -C /usr/local/bin oc kubectl

# etcd for recert (offline cert regeneration)
RUN curl -sL https://github.com/etcd-io/etcd/releases/download/v3.5.21/etcd-v3.5.21-linux-amd64.tar.gz \
    | tar xzf - -C /usr/local/bin --strip-components=1 etcd-v3.5.21-linux-amd64/etcd etcd-v3.5.21-linux-amd64/etcdctl

# recert for OCP certificate regeneration
COPY --from=quay.io/edge-infrastructure/recert:latest /usr/local/bin/recert /usr/local/bin/recert
RUN recert --help > /dev/null 2>&1 && etcd --version > /dev/null 2>&1 && echo "binaries verified"

CMD ["sleep", "infinity"]
