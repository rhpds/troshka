FROM registry.access.redhat.com/ubi9/python-311:9.5

WORKDIR /opt/operator

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes --only-binary :all: -r requirements.lock

# Fixed POST/SPDY-capable client for create-only collector pod exec.
RUN curl -fsSL --retry 3 -o /tmp/openshift-client.tar.gz \
      https://mirror.openshift.com/pub/openshift-v4/clients/ocp/4.20.10/openshift-client-linux-amd64-rhel9.tar.gz \
    && echo '9088ce68b6b97e659e568aaff568685df92afd1a2742bb310d72096976ff9125  /tmp/openshift-client.tar.gz' | sha256sum -c - \
    && tar -xzf /tmp/openshift-client.tar.gz -C /opt/app-root/bin oc \
    && rm /tmp/openshift-client.tar.gz

COPY . .

CMD ["kopf", "run", "main.py", "--verbose"]
