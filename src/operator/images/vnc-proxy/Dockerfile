FROM registry.access.redhat.com/ubi9/python-311:latest

WORKDIR /opt/vnc-proxy

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vnc_proxy.py .

EXPOSE 8080

CMD ["python3", "vnc_proxy.py"]
