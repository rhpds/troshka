FROM registry.access.redhat.com/ubi9/python-311:9.5

WORKDIR /opt/operator

COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary :all: -r requirements.txt # NOSONAR

COPY . .

CMD ["kopf", "run", "operator.py", "--verbose"]
