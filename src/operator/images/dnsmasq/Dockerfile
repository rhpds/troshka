FROM registry.access.redhat.com/ubi9/ubi-minimal:latest

RUN microdnf install -y dnsmasq tftp-server python3 && microdnf clean all

EXPOSE 53/udp 53/tcp 67/udp 69/udp 8080/tcp

CMD ["dnsmasq", "--no-daemon", "--conf-file=/etc/dnsmasq/dnsmasq.conf"]
