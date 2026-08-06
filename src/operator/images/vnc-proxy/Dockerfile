FROM registry.access.redhat.com/ubi9/python-311:9.5

WORKDIR /opt/vnc-proxy

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes --only-binary :all: -r requirements.lock

COPY vnc_proxy.py .

EXPOSE 8080

CMD ["python3", "vnc_proxy.py"]
