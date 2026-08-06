FROM registry.access.redhat.com/ubi9/python-311:9.5

WORKDIR /opt/vnc-proxy

COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary :all: -r requirements.txt # NOSONAR

COPY vnc_proxy.py .

EXPOSE 8080

CMD ["python3", "vnc_proxy.py"]
