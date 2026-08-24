#!/bin/bash
# Troshka variant — cloud-user instead of rhel; no containerlab SSH key wait.
set -euo pipefail
USER=rhel
HOME_DIR=/home/rhel
EE_IMAGE="registry.redhat.io/ansible-automation-platform-26/ee-supported-rhel9:latest"
NETWORK_EE_IMAGE="quay.io/acme_corp/network-ee"
REPO_URL="https://github.com/rhpds/zt-network-automation-workshop.git"
REPO_DIR="${HOME_DIR}/zt-network-automation-workshop"

echo "Setup vm control (troshka)" > /tmp/progress.log
chmod 666 /tmp/progress.log

registry_login() {
  if [[ -n "${REG_USER:-}" && -n "${REG_PASS:-}" ]]; then
    echo "Logging in to registry.redhat.io as ${REG_USER}..." >> /tmp/progress.log
    podman login registry.redhat.io -u "$REG_USER" -p "$REG_PASS" >> /tmp/progress.log 2>&1 || true
    sudo -u "$USER" -H podman login registry.redhat.io -u "$REG_USER" -p "$REG_PASS" >> /tmp/progress.log 2>&1 || true
  else
    echo "REG_USER/REG_PASS not set; skipping registry login" >> /tmp/progress.log
  fi
}

pull_ee_image() {
  echo "Pulling EE image ${EE_IMAGE}..." >> /tmp/progress.log
  sudo -u "$USER" -H podman pull "${EE_IMAGE}" >> /tmp/progress.log 2>&1 \
    && echo "EE image pulled" >> /tmp/progress.log \
    || echo "WARNING: EE pull failed" >> /tmp/progress.log
}

pull_network_ee() {
  echo "Pulling Network EE ${NETWORK_EE_IMAGE}..." >> /tmp/progress.log
  sudo -u "$USER" -H podman pull "${NETWORK_EE_IMAGE}" >> /tmp/progress.log 2>&1 \
    && echo "Network EE pulled" >> /tmp/progress.log \
    || echo "WARNING: Network EE pull failed" >> /tmp/progress.log
}

clone_repo() {
  if [[ -f "${REPO_DIR}/lab-automation/playbooks/site.yml" ]]; then
    echo "Workshop repo already present" >> /tmp/progress.log
    return 0
  fi
  echo "Cloning ${REPO_URL}..." >> /tmp/progress.log
  sudo -u "$USER" -H git clone "${REPO_URL}" "${REPO_DIR}" >> /tmp/progress.log 2>&1
}

install_rpms() {
  local rpm_dir="${REPO_DIR}/rpms"
  if [[ -d "${rpm_dir}" ]]; then
    rpm -ivh "${rpm_dir}"/*.rpm >> /tmp/progress.log 2>&1 || true
  fi
}

registry_login
pull_ee_image &
EE_PID=$!
pull_network_ee &
NET_EE_PID=$!
clone_repo
install_rpms
wait $EE_PID 2>/dev/null || true
wait $NET_EE_PID 2>/dev/null || true
echo "setup-control-troshka.sh complete (lab-automation deferred to router bootstrap)" >> /tmp/progress.log
