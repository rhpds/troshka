FROM registry.access.redhat.com/ubi9/ubi:latest

RUN dnf install -y \
    qemu-img \
    guestfs-tools \
    genisoimage \
    isomd5sum \
    python3 \
    python3-pip \
    && dnf clean all

RUN pip3 install awscli

CMD ["sleep", "infinity"]
