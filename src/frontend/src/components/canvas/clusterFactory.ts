import type { Node } from "@xyflow/react";
import type { ClusterConfig } from "@/stores/canvasStore";

/**
 * Default cluster sizing + config values.
 *
 * These MUST match Plan 1's backend normalization defaults exactly
 * (see docs/superpowers/plans/2026-09-01-multi-cluster-ocp-plan1-data-model.md):
 *   CP     cpu 8  / memory 16384 / disk 120
 *   worker cpu 4  / memory 8192  / disk 100
 *   type "standard", controlPlane 3, workers 0.
 *
 * ocpVersion defaults to "" and is auto-set to the latest Full Support release
 * once the version list loads (see the cluster editor).
 *
 * baseDomain defaults to the shared parent "local" (NOT name-derived): OCP DNS
 * is api/api-int/*.apps.<name>.<baseDomain>, so the cluster NAME is the unique
 * subdomain and the base domain is a shared parent (e.g. "ocp3" + "local" ->
 * api.ocp3.local). Baking the name into the base domain double-labels it.
 */
const CLUSTER_DEFAULTS = {
  type: "standard",
  controlPlane: 3,
  workers: 0,
  controlPlaneCpu: 8,
  controlPlaneMemory: 16384,
  controlPlaneDisk: 120,
  workerCpu: 4,
  workerMemory: 8192,
  workerDisk: 100,
  baseDomain: "local",
  apiVip: "",
  ingressVip: "",
  ocpVersion: "", // auto-set to latest Full Support once the version list loads
  pullThroughRegistry: null as string | null,
  controlPlaneDisks: [{ sizeGb: 120, bootable: true }, { sizeGb: 100 }],
  workerDisks: [{ sizeGb: 120, bootable: true }, { sizeGb: 100 }],
  // OCP control-plane options (projected onto member VMs at deploy).
  recert: false,
  monitorHealth: true,
  configureBastionBrowser: false,
} as const;

/** The default rendered footprint of a cluster boundary node (px). */
const CLUSTER_NODE_SIZE = { width: 520, height: 320 } as const;

/** Convert a human name into a URL/id-safe slug. */
function slugify(name: string): string {
  const slug = name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || "cluster";
}

/**
 * Build a cluster boundary node plus its matching ClusterConfig.
 *
 * The returned `node.id` and `cluster.nodeId` share the value
 * `cluster-<slug>-<short>`; `cluster.id` is `<slug>-<short>`. A short random
 * suffix guarantees uniqueness across multiple drops (multi-cluster is the
 * core use case — two "ocp" drops must not collide).
 */
export function makeCluster(
  name: string,
  position: { x: number; y: number },
): { node: Node; cluster: ClusterConfig } {
  const slug = slugify(name);
  const short = Math.random().toString(36).slice(2, 8);
  const clusterId = `${slug}-${short}`;
  const nodeId = `cluster-${clusterId}`;

  const node: Node = {
    id: nodeId,
    type: "clusterNode",
    position,
    style: { width: CLUSTER_NODE_SIZE.width, height: CLUSTER_NODE_SIZE.height },
    data: {
      label: name,
      name,
      clusterId,
      type: CLUSTER_DEFAULTS.type,
      controlPlane: CLUSTER_DEFAULTS.controlPlane,
      workers: CLUSTER_DEFAULTS.workers,
      baseDomain: CLUSTER_DEFAULTS.baseDomain,
      apiVip: CLUSTER_DEFAULTS.apiVip,
      ingressVip: CLUSTER_DEFAULTS.ingressVip,
      networkIds: [],
      controlPlaneDisks: [...CLUSTER_DEFAULTS.controlPlaneDisks],
      workerDisks: [...CLUSTER_DEFAULTS.workerDisks],
    },
  };

  const cluster: ClusterConfig = {
    id: clusterId,
    name,
    nodeId,
    type: CLUSTER_DEFAULTS.type,
    controlPlane: CLUSTER_DEFAULTS.controlPlane,
    workers: CLUSTER_DEFAULTS.workers,
    controlPlaneCpu: CLUSTER_DEFAULTS.controlPlaneCpu,
    controlPlaneMemory: CLUSTER_DEFAULTS.controlPlaneMemory,
    controlPlaneDisk: CLUSTER_DEFAULTS.controlPlaneDisk,
    workerCpu: CLUSTER_DEFAULTS.workerCpu,
    workerMemory: CLUSTER_DEFAULTS.workerMemory,
    workerDisk: CLUSTER_DEFAULTS.workerDisk,
    baseDomain: CLUSTER_DEFAULTS.baseDomain,
    apiVip: CLUSTER_DEFAULTS.apiVip,
    ingressVip: CLUSTER_DEFAULTS.ingressVip,
    ocpVersion: CLUSTER_DEFAULTS.ocpVersion,
    pullThroughRegistry: CLUSTER_DEFAULTS.pullThroughRegistry ?? undefined,
    networkIds: [],
    controlPlaneDisks: [...CLUSTER_DEFAULTS.controlPlaneDisks],
    workerDisks: [...CLUSTER_DEFAULTS.workerDisks],
    recert: CLUSTER_DEFAULTS.recert,
    monitorHealth: CLUSTER_DEFAULTS.monitorHealth,
    configureBastionBrowser: CLUSTER_DEFAULTS.configureBastionBrowser,
  };

  return { node, cluster };
}
