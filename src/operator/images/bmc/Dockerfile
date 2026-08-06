FROM registry.access.redhat.com/ubi9/python-311:9.5

WORKDIR /opt/sushy

COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary :all: -r requirements.txt # NOSONAR

COPY kubevirt_driver.py entrypoint.py ./

EXPOSE 8000 8443

CMD ["python3", "entrypoint.py"]
