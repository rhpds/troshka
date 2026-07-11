FROM registry.access.redhat.com/ubi9/ubi-minimal:latest

RUN microdnf install -y nftables iproute && microdnf clean all

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]
