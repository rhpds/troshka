FROM quay.io/fedora/fedora:41

RUN dnf install -y \
    qemu-img \
    guestfs-tools \
    genisoimage \
    isomd5sum \
    python3 \
    python3-pip \
    awscli \
    && dnf clean all

CMD ["sleep", "infinity"]
