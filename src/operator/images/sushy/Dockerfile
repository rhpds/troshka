FROM registry.access.redhat.com/ubi9/python-311:latest

WORKDIR /opt/sushy

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY kubevirt_driver.py entrypoint.py ./

EXPOSE 8000

CMD ["python3", "entrypoint.py"]
