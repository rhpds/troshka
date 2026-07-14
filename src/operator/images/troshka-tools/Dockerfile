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
    openssl \
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
# Must match OCP's etcd version exactly — version mismatch corrupts WAL
COPY --from=gcr.io/etcd-development/etcd:v3.6.0 /usr/local/bin/etcd /usr/local/bin/etcd
COPY --from=gcr.io/etcd-development/etcd:v3.6.0 /usr/local/bin/etcdctl /usr/local/bin/etcdctl

# recert for OCP certificate regeneration
COPY --from=quay.io/edge-infrastructure/recert:latest /usr/local/bin/recert /usr/local/bin/recert
RUN recert --help > /dev/null 2>&1 && etcd --version > /dev/null 2>&1 && echo "binaries verified"

CMD ["sleep", "infinity"]
