export interface KubevirtCapabilities {
  featureGates: string[];
  videoConfigEnabled: boolean;
  diskBuses: string[];
  videoModels: string[];
  machineTypes: string[];
  inputModels: string[];
}

export interface ClusterCapabilities {
  version?: number;
  updatedAt?: string | null;
  kubevirt?: KubevirtCapabilities | null;
}

export const DISK_BUS_LABELS: Record<string, string> = {
  virtio: "virtio-blk",
  scsi: "virtio-scsi",
  sata: "SATA (AHCI)",
  ide: "IDE",
  usb: "USB",
};

export const VIDEO_MODEL_LABELS: Record<string, string> = {
  virtio: "VirtIO (recommended)",
  vga: "VGA",
  qxl: "QXL",
};

export const MACHINE_TYPE_LABELS: Record<string, string> = {
  q35: "Q35 (pc-q35)",
  i440fx: "i440fx (pc)",
};

export const DEFAULT_DISK_BUSES = ["virtio", "scsi", "sata", "ide", "usb"];
export const DEFAULT_VIDEO_MODELS = ["virtio", "vga", "qxl"];
export const DEFAULT_MACHINE_TYPES = ["q35", "i440fx"];

export function getKubevirtCapabilities(
  clusterCapabilities: ClusterCapabilities | null | undefined,
): KubevirtCapabilities | null {
  return clusterCapabilities?.kubevirt ?? null;
}

export function allowedDiskBuses(
  clusterCapabilities: ClusterCapabilities | null | undefined,
  providerType: string | null,
): string[] {
  if (providerType !== "kubevirt") return DEFAULT_DISK_BUSES;
  const caps = getKubevirtCapabilities(clusterCapabilities);
  return caps?.diskBuses?.length ? caps.diskBuses : ["virtio", "scsi", "sata"];
}

export function allowedVideoModels(
  clusterCapabilities: ClusterCapabilities | null | undefined,
  providerType: string | null,
): string[] {
  if (providerType !== "kubevirt") return DEFAULT_VIDEO_MODELS;
  const caps = getKubevirtCapabilities(clusterCapabilities);
  if (!caps) return DEFAULT_VIDEO_MODELS;
  if (!caps.videoConfigEnabled) return [];
  return caps.videoModels?.length ? caps.videoModels : DEFAULT_VIDEO_MODELS;
}

export function allowedMachineTypes(
  clusterCapabilities: ClusterCapabilities | null | undefined,
  providerType: string | null,
): string[] {
  if (providerType === "kubevirt") {
    const caps = getKubevirtCapabilities(clusterCapabilities);
    return caps?.machineTypes?.length ? caps.machineTypes : ["q35"];
  }
  if (providerType === "ocpvirt") {
    const caps = getKubevirtCapabilities(clusterCapabilities);
    return caps?.machineTypes?.length ? caps.machineTypes : DEFAULT_MACHINE_TYPES;
  }
  return DEFAULT_MACHINE_TYPES;
}
