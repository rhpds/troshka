FROM registry.access.redhat.com/ubi9/python-311:latest

WORKDIR /opt/operator

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["kopf", "run", "operator.py", "--verbose"]
