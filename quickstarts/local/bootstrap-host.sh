#!/usr/bin/env bash
# Bootstrap a Linux environment as a Troshka libvirt host (run ON Linux / WSL / guest).
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  cat <<EOF >&2
error: bootstrap-host.sh must run on Linux.

  macOS:  brew install libvirt qemu → create a Linux guest → run this script inside the guest
  Windows: use WSL2 with nested virtualization enabled → run this script inside WSL
  Or skip local virt and attach EC2 / Azure / GCP / KubeVirt / a remote Linux box from the UI.

EOF
  exit 1
fi

echo "Installing libvirt / KVM packages (best-effort by distro)..."
if command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y libvirt qemu-kvm virt-install libvirt-client || true
  sudo systemctl enable --now libvirtd || true
elif command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y qemu-kvm libvirt-daemon-system libvirt-clients virtinst || true
  sudo systemctl enable --now libvirtd || true
else
  echo "warning: unknown package manager — install libvirt/KVM manually" >&2
fi

if ! command -v virsh >/dev/null 2>&1; then
  echo "error: virsh not available after package install" >&2
  exit 1
fi

echo
echo "libvirt looks available. Next steps in the Troshka UI:"
echo "  1. Open the Troshka UI (local quickstart defaults to http://localhost:3100)"
echo "  2. Admin → Hosts → add this machine (SSH reachable from the Troshka backend)"
echo "  3. Use Install Agent on the host (or scripts/reinstall-agent.sh from a full checkout)"
echo
echo "WSL note: ensure nested virtualization is enabled; networking is NAT'd (single-host demos only)."
echo "If nested virt fails, use a remote Linux host or EC2/Azure/GCP/KubeVirt instead."
echo
echo "Done."
