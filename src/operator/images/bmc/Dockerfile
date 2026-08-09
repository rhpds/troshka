FROM registry.access.redhat.com/ubi9/python-311:9.5

WORKDIR /opt/sushy

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes --only-binary :all: -r requirements.lock

COPY kubevirt_driver.py entrypoint.py ./

EXPOSE 8000 8443

CMD ["python3", "entrypoint.py"]
