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
    qemu-nbd \
    kmod \
    e2fsprogs \
    xfsprogs \
    util-linux \
    tesseract \
    unzip \
    && dnf clean all \
    && curl --proto =https -sL https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz \
    | tar xzf - -C /usr/local/bin oc kubectl \
    && curl -fsO https://downloads.rclone.org/rclone-current-linux-amd64.zip \
    && unzip -q rclone-current-linux-amd64.zip \
    && cp rclone-*/rclone /usr/local/bin/ \
    && chmod +x /usr/local/bin/rclone \
    && rm -rf rclone-*

# recert for OCP certificate regeneration
COPY --from=quay.io/edge-infrastructure/recert:latest /usr/local/bin/recert /usr/local/bin/recert
RUN recert --help > /dev/null 2>&1 && echo "recert binary verified"

# etcd for recert — must match OCP's etcd major.minor version
# OCP 4.22 uses etcd 3.6.0, same as troshkad's ETCD_IMAGE
COPY --from=gcr.io/etcd-development/etcd:v3.6.0 /usr/local/bin/etcd /usr/local/bin/etcd
COPY --from=gcr.io/etcd-development/etcd:v3.6.0 /usr/local/bin/etcdctl /usr/local/bin/etcdctl

CMD ["sleep", "infinity"]
