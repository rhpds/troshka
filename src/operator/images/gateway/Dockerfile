FROM registry.access.redhat.com/ubi9/ubi-minimal:9.5

RUN microdnf install -y nftables iproute socat && microdnf clean all

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]
