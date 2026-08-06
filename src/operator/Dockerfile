FROM registry.access.redhat.com/ubi9/python-311:9.5

WORKDIR /opt/operator

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes --only-binary :all: -r requirements.lock

COPY . .

CMD ["kopf", "run", "operator.py", "--verbose"]
