"use client";

import React, { useCallback, useRef, useMemo, useState, useEffect } from "react";
import {
  ReactFlow,
  useReactFlow,
  Background,
  BackgroundVariant,
  ConnectionLineType,
  ConnectionMode,
  MiniMap,
  SelectionMode,
  type Node,
  type Connection,
  type Edge,
  type OnSelectionChangeParams,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import VMNode from "./nodes/VMNode";
import NetworkNode from "./nodes/NetworkNode";
import StorageNode from "./nodes/StorageNode";
import { ContainerNode } from "./nodes/ContainerNode";
import ClusterNode from "./nodes/ClusterNode";
import CephClusterNode from "./nodes/CephClusterNode";
import { layoutCephBetweenClusters } from "./cephClusterLayout";
import ClusterAnchorEdge from "./edges/ClusterAnchorEdge";
import CanvasToolbar from "./CanvasToolbar";
import {
  CLUSTER_ANCHOR_EDGE_TYPE,
  isClusterAnchorEdge,
} from "@/lib/clusterAnchorEdge";
import NodeContextMenu from "./NodeContextMenu";
import EdgeContextMenu from "./EdgeContextMenu";
import DuplicateVMModal from "./DuplicateVMModal";
import ClusterInstallLogModal from "./ClusterInstallLogModal";
import { useCanvasStore, generateNodeId, generateNicId, generateDiskControllerId, generateMac, onRequestDuplicateVM, onRequestDuplicateCluster, type ClusterConfig } from "@/stores/canvasStore";
import { makeCluster } from "@/components/canvas/clusterFactory";
import { resolveMembership, absolutePosition, relativePosition, orderChildAfterParent } from "@/components/canvas/clusterMembership";
import { assignmentDataPatch, materializeClusterInto, applyClusterNetworks, syncClusterCanvasState, roleDiskSpecs } from "@/components/canvas/clusterMaterialize";
import { hasShowroomNode } from "@/lib/showroomScaffold";
import { appConfirm } from "@/lib/confirm";
import { confirmPowerWarn } from "@/lib/powerWarn";
import {
  GATEWAY_NETWORK_SOURCE_HANDLE,
  GATEWAY_NETWORK_TARGET_HANDLE,
} from "@/lib/gatewayValidation";
import {
  isLbBackendNode,
  isLbEdgeHandle,
  isLbWorkloadNicHandle,
  isLoadBalancerNode,
} from "@/lib/loadBalancerValidation";
import {
  isContainerMountLeftHandle,
  isPodContainer,
  isStorageSourceHandle,
  isVmDiskLeftHandle,
  withStorageFlowAnimation,
} from "@/lib/storageEdgeStyle";
import {
  isGatewayNode,
  isCephStorageNetworkNode,
  isShowroomContainer,
  SHOWROOM_GATEWAY_SOURCE_HANDLE,
  SHOWROOM_GATEWAY_TARGET_HANDLE,
} from "@/lib/showroomValidation";
import { useIsDarkTheme } from "@/hooks/useIsDarkTheme";

function removeClusterNetworkAnchor(edge: Edge) {
  const state = useCanvasStore.getState();
  const target = state.nodes.find((node) => node.id === edge.target);
  if (target?.type !== "clusterNode") return;
  const cluster = state.clusters.find((candidate) => candidate.nodeId === target.id);
  if (!cluster) return;
  const networkIds = (cluster.networkIds ?? []).filter((id) => id !== edge.source);
  const updated = { ...cluster, networkIds };
  const next = applyClusterNetworks(updated, state.nodes, state.edges);
  state.updateCluster(cluster.id, { networkIds });
  useCanvasStore.setState({ nodes: next.nodes, edges: next.edges });
}

const nodeTypes = {
  vmNode: VMNode,
  networkNode: NetworkNode,
  storageNode: StorageNode,
  containerNode: ContainerNode,
  clusterNode: ClusterNode,
  cephClusterNode: CephClusterNode,
};

const edgeTypes = {
  [CLUSTER_ANCHOR_EDGE_TYPE]: ClusterAnchorEdge,
};

interface ContextMenuState {
  nodeId: string;
  x: number;
  y: number;
}

interface EdgeContextMenuState {
  edgeId: string;
  x: number;
  y: number;
}

interface CanvasProps {
  onSnapshotVM?: (nodeId: string, nodeName: string, isRunning: boolean) => void;
  onRunWorkload?: (target: {
    mode: "cluster" | "vms";
    clusterIds?: string[];
    vmNames?: string[];
  }) => void;
}
type DropItem = { type: string; label: string; icon: string; defaults?: Record<string, unknown> };
type DropContext = { item: DropItem; id: string; position: { x: number; y: number }; nextName: (prefix: string) => string };

function dropVm({ item, id, position, nextName }: DropContext): Node | undefined {

        const defaults = item.defaults || {};
        const name = nextName("vm");
        const newNode: Node = {
          id,
          type: "vmNode",
          position,
          data: {
            label: name,
            name,
            vcpus: (defaults.vcpus as number) || 2,
            ram: (defaults.ram as number) || 4,
            os: (defaults.os as string) || "rhel10",
            status: "stopped" as const,
            bootOrder: undefined,
            bootMethod: "disk",
            cloudInit: true,
            icon: "🖥",
            nics: [{ id: generateNicId(), name: "eth0", mac: generateMac(), model: "virtio" }],
            diskControllers: [{ id: generateDiskControllerId(), name: "disk0", bus: "virtio" }],
          },
        };

        return newNode;

}

function nextNetworkCidr(): string {
        const existingCidrs = useCanvasStore.getState().nodes
          .filter((n) => n.type === "networkNode")
          .map((n) => (n.data as Record<string, unknown>).cidr as string)
          .filter((c) => c?.includes("/"));

        let newCidr = "10.0.0.0/24";
        if (existingCidrs.length > 0) {
          for (let i = existingCidrs.length - 1; i >= 0; i--) {
            const match = /^(\d+\.\d+\.)(\d+)(\.0\/\d+)$/.exec(existingCidrs[i]);
            if (match) {
              let octet3 = Number.parseInt(match[2], 10) + 1;
              while (existingCidrs.includes(`${match[1]}${octet3}${match[3]}`)) octet3++;
              newCidr = `${match[1]}${octet3}${match[3]}`;
              break;
            }
          }
        }

  return newCidr;
}
function dropNetwork({ item, id, position, nextName }: DropContext): Node | undefined {

        // Limit to one gateway per project
        if (item.type === "gateway") {
          const hasGateway = useCanvasStore.getState().nodes.some(
            (n) => n.type === "networkNode" && (n.data as Record<string, unknown>).subtype === "gateway"
          );
          if (hasGateway) return;
        }

        const prefix = item.type === "network" ? "network" : item.type;
        const name = nextName(prefix);

        const newCidr = nextNetworkCidr();

        const newNode: Node = {
          id,
          type: "networkNode",
          position,
          data: {
            label: name,
            name,
            subtype: item.type as "network" | "router" | "gateway" | "dhcp" | "dns",
            cidr: (item.type === "gateway" || item.type === "router") ? "" : newCidr,
            dhcp: item.type === "dhcp" || item.type === "network",
            dns: item.type === "dns",
            dnsDomain: item.type === "dns" ? "lab.local" : "",
            // Match ocp-sno.yaml gateway defaults so canvas-built OCP installs
            // can pull images / sync time without a manual properties pass.
            ...(item.type === "gateway"
              ? {
                  outboundPolicy: "restrict",
                  outboundPorts: "53,80/tcp,443/tcp,123/udp",
                }
              : {}),
          },
        };

        return newNode;

}

function dropLoadBalancer({ id, position, nextName }: DropContext): Node | undefined {

        const name = nextName("lb");
        const newNode: Node = {
          id,
          type: "networkNode",
          position,
          data: {
            label: name,
            name,
            subtype: "loadbalancer",
            networkType: "loadbalancer",
            frontends: [
              { name: "https", bindPort: 443, mode: "tcp", backendPort: 443 },
              { name: "http", bindPort: 80, mode: "tcp", backendPort: 80 },
            ],
            lbIp: "",
            external: false,
            dnsRecords: [],
            dnsTtl: 30,
          },
        };

        return newNode;

}

function dropStorage({ item, id, position, nextName }: DropContext): Node | undefined {

        const prefix = item.type === "iso" ? "boot" : "disk";
        const name = nextName(prefix);
        const newNode: Node = {
          id,
          type: "storageNode",
          position,
          data: {
            label: name,
            name,
            size: item.type === "iso" ? 4 : 20,
            format: item.type === "iso" ? "iso" : "qcow2",
            icon: item.type === "iso" ? "💿" : "🛢",
          },
        };

        return newNode;

}

function dropContainer({ id, position, nextName }: DropContext): Node | undefined {

        const name = nextName("ctr");
        const newNode: Node = {
          id,
          type: "containerNode",
          position,
          data: {
            label: name,
            name,
            image: "",
            registryCredentialId: null,
            cpus: 1,
            memory: 512,
            status: "stopped" as const,
            icon: "📦",
            nics: [
              {
                id: generateNicId(),
                name: "eth0",
                mac: generateMac(),
                model: "virtio",
              },
            ],
            envVars: [],
            ports: [],
            command: null,
            restartPolicy: "always" as const,
            privileged: false,
            mounts: [],
          },
        };

        return newNode;

}

function dropPod({ id, position, nextName }: DropContext): Node | undefined {

        const name = nextName("pod");
        const newNode: Node = {
          id,
          type: "containerNode",
          position,
          data: {
            label: name,
            name,
            image: "",
            registryCredentialId: null,
            cpus: 1,
            memory: 512,
            status: "stopped" as const,
            icon: "🫛",
            isPod: true,
            nics: [
              {
                id: generateNicId(),
                name: "eth0",
                mac: generateMac(),
                model: "virtio",
              },
            ],
            envVars: [],
            ports: [],
            command: null,
            restartPolicy: "always" as const,
            privileged: false,
            mounts: [],
            initContainers: [],
            podContainers: [
              {
                name: "main",
                image: "",
                cpus: 1,
                memory: 512,
                envVars: [],
                ports: [],
                command: null,
                mounts: [],
              },
            ],
          },
        };

        return newNode;

}

function dropCeph({ id, position }: DropContext): Node | undefined {

        const state = useCanvasStore.getState();
        if (state.nodes.some((n) => n.type === "cephClusterNode")) return;
        const name = "Ceph Storage";
        const net = state.nodes.find(isCephStorageNetworkNode);
        const cidr = (net?.data as Record<string, unknown>)?.cidr as string | undefined;
        let labIp = "";
        if (cidr?.includes("/")) {
          const octets = cidr.split("/")[0].split(".");
          octets[3] = "3";
          labIp = octets.join(".");
        }
        const newNode: Node = {
          id,
          type: "cephClusterNode",
          position,
          data: {
            label: name,
            name,
            networkRef: net?.id || "",
            labIp,
            capacityGi: 300,
            osdCount: 3,
            linkedClusters: [] as string[],
            storageClassName: "troshka-ceph-rbd",
          },
        };
        const laidOut = layoutCephBetweenClusters([...state.nodes, newNode]);
        useCanvasStore.setState({ nodes: laidOut });

}

function dropShowroom({ position }: DropContext): Node | undefined {

        if (hasShowroomNode(useCanvasStore.getState().nodes)) return;
        useCanvasStore.getState().addShowroomScaffold(position);

}

function dropSnapshot({ item, position }: DropContext): Node | undefined {

        const snapshotId = (item.defaults as Record<string, unknown>)?.snapshotId as string;
        if (!snapshotId) return;
        const projectId = useCanvasStore.getState().currentProjectId;
        if (!projectId) return;
        fetch(`/api/v1/projects/${projectId}/import-vm`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ snapshot_id: snapshotId, position_x: position.x, position_y: position.y }),
        })
          .then((r) => r.ok ? r.json() : null)
          .then((data) => {
            if (data?.topology) {
              useCanvasStore.getState().loadProject(projectId);
            }
          })
          .catch(() => {});

}

function dropCluster({ position }: DropContext): undefined {
  const { addNode, addCluster, updateCluster } = useCanvasStore.getState();

        // Unique cluster name: ocp, then ocp-2, ocp-3, … (the name is the DNS
        // subdomain, so it must be unique across clusters).
        const existingClusterNames = new Set(
          useCanvasStore.getState().clusters.map((c) => c.name),
        );
        let clusterName = "ocp";
        let suffix = 2;
        while (existingClusterNames.has(clusterName)) {
          clusterName = `ocp-${suffix++}`;
        }
        const { node, cluster } = makeCluster(clusterName, position);
        addNode(node);
        addCluster(cluster);
        const { nodes: withMembers, edges: diskEdges } = materializeClusterInto(
          cluster,
          useCanvasStore.getState().nodes,
        );
        const currentEdges = useCanvasStore.getState().edges;
        const edgeMap = new Map(currentEdges.map((e) => [e.id, e]));
        diskEdges.forEach((e) => edgeMap.set(e.id, e));
        const mergedEdges = Array.from(edgeMap.values());
        const synced = syncClusterCanvasState(cluster, withMembers, mergedEdges);
        updateCluster(synced.cluster.id, synced.cluster);
        useCanvasStore.setState({ nodes: synced.nodes, edges: mergedEdges });

}

function dropPaletteItem(item: DropItem, position: { x: number; y: number }) {
      const id = generateNodeId();
      const allNames = new Set(useCanvasStore.getState().nodes.map(
        (n) => (n.data as Record<string, unknown>).name as string
      ).filter(Boolean));

      const nextName = (prefix: string) => {
        let num = 0;
        while (allNames.has(`${prefix}-${String(num).padStart(2, "0")}`)) num++;
        return `${prefix}-${String(num).padStart(2, "0")}`;
      };


  const context = { item, id, position, nextName };
  const handlers: Record<string, (context: DropContext) => Node | undefined> = {
    "vm-linux": dropVm, "vm-windows": dropVm,
    network: dropNetwork, router: dropNetwork, gateway: dropNetwork, dhcp: dropNetwork, dns: dropNetwork,
    loadbalancer: dropLoadBalancer, disk: dropStorage, iso: dropStorage,
    container: dropContainer, pod: dropPod, "project-ceph": dropCeph,
    showroom: dropShowroom, snapshot: dropSnapshot, cluster: dropCluster,
  };
  const handler = item.type.startsWith("template-") ? dropVm : handlers[item.type];
  const newNode = handler?.(context);
  if (newNode) useCanvasStore.getState().addNode(newNode);
}

function isPlainNetwork(node: Node): boolean {
  return node.type === "networkNode" && !["router", "gateway"].includes(node.data.subtype as string) && !isLoadBalancerNode(node);
}
function isVmNetworkHandle(handle: string): boolean {
  return handle.startsWith("nic-") || handle === "top" || handle === "bottom";
}
function validShowroomLink(other: Node, handle: string, otherHandle: string): boolean {
  if (other.type === "storageNode") return isContainerMountLeftHandle(handle);
  if (!isGatewayNode(other)) return false;
  return handle === SHOWROOM_GATEWAY_SOURCE_HANDLE && otherHandle === SHOWROOM_GATEWAY_TARGET_HANDLE;
}
function validPodLink(other: Node, handle: string, otherHandle: string): boolean {
  if (other.type === "storageNode") return isContainerMountLeftHandle(handle);
  if (isLoadBalancerNode(other)) return isLbEdgeHandle(otherHandle) && isLbWorkloadNicHandle(handle);
  return other.type === "networkNode";
}
function validLbLink(other: Node, handle: string, otherHandle: string): boolean {
  return isLbBackendNode(other) && isLbEdgeHandle(handle) && isLbWorkloadNicHandle(otherHandle);
}
function validEndpointHandle(node: Node, other: Node, handle: string): boolean {
  if (node.type === "vmNode") {
    if (other.type === "storageNode" && !isVmDiskLeftHandle(handle)) return false;
    if (isPlainNetwork(other) && !isVmNetworkHandle(handle)) return false;
  }
  if (node.type === "storageNode" && !isStorageSourceHandle(handle)) return false;
  if (isPlainNetwork(node) && other.type === "vmNode" && !isVmNetworkHandle(handle)) return false;
  return true;
}
function validInfrastructureHandle(node: Node, other: Node, handle: string, otherHandle: string): boolean {
  if (node.type !== "networkNode" || !isPlainNetwork(other)) return true;
  if (node.data.subtype === "router") return otherHandle === "left" || otherHandle === "right";
  if (node.data.subtype === "gateway") return handle === GATEWAY_NETWORK_SOURCE_HANDLE && otherHandle === GATEWAY_NETWORK_TARGET_HANDLE;
  return true;
}
function validCephLink(other: Node, handle: string, otherHandle: string): boolean {
  if (other.type !== "clusterNode") return false;
  return (handle === "right" && otherHandle === "ceph-left") || (handle === "left" && otherHandle === "ceph-right");
}
function specialCanvasConnection(source: Node, target: Node, sHandle: string, tHandle: string): boolean | undefined {
  if (isShowroomContainer(source)) return validShowroomLink(target, sHandle, tHandle);
  if (isShowroomContainer(target)) return validShowroomLink(source, tHandle, sHandle);
  if (isPodContainer(source)) return validPodLink(target, sHandle, tHandle);
  if (isPodContainer(target)) return validPodLink(source, tHandle, sHandle);
  if (isLoadBalancerNode(source)) return validLbLink(target, sHandle, tHandle);
  if (isLoadBalancerNode(target)) return validLbLink(source, tHandle, sHandle);
  return undefined;
}
function validCanvasConnection(connection: Connection | Edge, nodes: Node[]): boolean {
  if (!connection.source || !connection.target) return false;
  const source = nodes.find((node) => node.id === connection.source);
  const target = nodes.find((node) => node.id === connection.target);
  if (!source || !target) return false;
  const sHandle = connection.sourceHandle || "";
  const tHandle = connection.targetHandle || "";
  const special = specialCanvasConnection(source, target, sHandle, tHandle);
  if (special !== undefined) return special;
  if (!validEndpointHandle(source, target, sHandle) || !validEndpointHandle(target, source, tHandle)) return false;
  if (!validInfrastructureHandle(source, target, sHandle, tHandle) || !validInfrastructureHandle(target, source, tHandle, sHandle)) return false;
  return validClusterConnection(source, target, sHandle, tHandle);
}
function validClusterConnection(source: Node, target: Node, sHandle: string, tHandle: string): boolean {
  if (source.type === "cephClusterNode") return validCephLink(target, sHandle, tHandle);
  if (target.type === "cephClusterNode") return validCephLink(source, tHandle, sHandle);
  if (source.type === "clusterNode" && tHandle.startsWith("cluster-net-")) return isPlainNetwork(source);
  if (target.type === "clusterNode" && sHandle.startsWith("cluster-net-")) return isPlainNetwork(target);
  return true;
}

function SelectionToolbar({ selectedNodes, setSelectedNodes }: Readonly<{ selectedNodes: Node[]; setSelectedNodes: (nodes: Node[]) => void }>) {
  const { deployedVmIds, currentProjectId, duplicateNode, deleteNode, supportsHibernate } = useCanvasStore();
        const selectedVmNodes = selectedNodes.filter(
          (n) => n.type === "vmNode" && deployedVmIds.has(n.id),
        );
        const runOnSelectedVms = (action: "start" | "unpause" | "stop" | "pause" | "hibernate") => {
          selectedVmNodes.forEach((n) => {
            fetch(`/api/v1/projects/${currentProjectId}/vms/${n.id}/${action}`, { method: "POST" }).catch(() => {});
          });
        };
        return (
          <div className="selection-toolbar">
            <span className="selection-count">{selectedNodes.length} selected</span>
            <button type="button"
              title="Duplicate All"
              onClick={() => selectedNodes.forEach((n) => duplicateNode(n.id))}
            >
              ⧉ Duplicate
            </button>
            {selectedVmNodes.length > 0 && (
              <>
                <button type="button"
                  title="Start/Resume VMs"
                  onClick={() => {
                    selectedVmNodes.forEach((n) => {
                      const status = (n.data as Record<string, unknown>).status as string | undefined;
                      const action = status === "paused" ? "unpause" : "start";
                      fetch(`/api/v1/projects/${currentProjectId}/vms/${n.id}/${action}`, { method: "POST" }).catch(() => {});
                    });
                  }}
                >
                  ▶ Start VMs
                </button>
                <button type="button"
                  title="Graceful Shutdown VMs"
                  onClick={async () => {
                    if (!(await appConfirm({ message: `Shut down ${selectedVmNodes.length} VM(s)?`, confirmLabel: "Shut down" }))) return;
                    runOnSelectedVms("stop");
                  }}
                >
                  ■ Stop VMs
                </button>
                <button type="button"
                  title="Pause VMs"
                  onClick={async () => {
                    if (!(await confirmPowerWarn("pause", { title: `Pause ${selectedVmNodes.length} VM(s)?`, confirmLabel: "Pause" }))) return;
                    runOnSelectedVms("pause");
                  }}
                >
                  ⏸ Pause VMs
                </button>
                <button type="button"
                  title={supportsHibernate ? "Hibernate VMs" : "Not supported on this host type"}
                  disabled={!supportsHibernate}
                  style={supportsHibernate ? undefined : { opacity: 0.4, cursor: "not-allowed" }}
                  onClick={async () => {
                    if (!supportsHibernate) return;
                    if (!(await confirmPowerWarn("hibernate", { title: `Hibernate ${selectedVmNodes.length} VM(s)?`, confirmLabel: "Hibernate" }))) return;
                    runOnSelectedVms("hibernate");
                  }}
                >
                  💤 Hibernate VMs
                </button>
              </>
            )}
            <button type="button"
              className="danger"
              title="Delete All"
              onClick={() => {
                selectedNodes.forEach((n) => deleteNode(n.id));
                setSelectedNodes([]);
              }}
            >
              ✕ Delete
            </button>
          </div>
        );

}
export default function Canvas({ onSnapshotVM, onRunWorkload }: Readonly<CanvasProps>) {
  const reactFlowWrapper = useRef<HTMLDivElement>(null);
  const { screenToFlowPosition } = useReactFlow();
  const isDark = useIsDarkTheme();
  const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null);
  const [edgeContextMenu, setEdgeContextMenu] = useState<EdgeContextMenuState | null>(null);
  const [selectedNodes, setSelectedNodes] = useState<Node[]>([]);
  const [duplicateVmId, setDuplicateVmId] = useState<string | null>(null);

  useEffect(() => {
    onRequestDuplicateVM((id) => setDuplicateVmId(id));
    return () => onRequestDuplicateVM(null);
  }, []);

  // Full cluster clone: a fresh ClusterConfig + boundary + materialized members,
  // copying the source cluster's type/sizing/options (the generic node duplicate
  // can't do this — it lives in the store, these helpers live here).
  useEffect(() => {
    onRequestDuplicateCluster((nodeId) => {
      const st = useCanvasStore.getState();
      const src = st.nodes.find((n) => n.id === nodeId);
      const srcCluster = st.clusters.find(
        (c) => c.nodeId === nodeId || `cluster-${c.id}` === nodeId,
      );
      if (!src || !srcCluster) return;
      // Unique name based on the source (ocp -> ocp-2, ocp-3, …).
      const existing = new Set(st.clusters.map((c) => c.name));
      const m = /^(.*?)-(\d+)$/.exec(srcCluster.name);
      const base = m ? m[1] : srcCluster.name;
      let n = m ? Number.parseInt(m[2], 10) + 1 : 2;
      while (existing.has(`${base}-${n}`)) n++;
      const newName = `${base}-${n}`;

      const { node, cluster } = makeCluster(newName, {
        x: src.position.x + (src.width ?? 520) + 60,
        y: src.position.y,
      });
      // Copy the source cluster's shape/sizing/options onto the clone.
      const merged: ClusterConfig = {
        ...cluster,
        type: srcCluster.type,
        controlPlane: srcCluster.controlPlane,
        workers: srcCluster.workers,
        controlPlaneCpu: srcCluster.controlPlaneCpu,
        controlPlaneMemory: srcCluster.controlPlaneMemory,
        controlPlaneDisk: srcCluster.controlPlaneDisk,
        workerCpu: srcCluster.workerCpu,
        workerMemory: srcCluster.workerMemory,
        workerDisk: srcCluster.workerDisk,
        baseDomain: srcCluster.baseDomain,
        ocpVersion: srcCluster.ocpVersion,
        pullThroughRegistry: srcCluster.pullThroughRegistry,
        usePullThroughRegistry: srcCluster.usePullThroughRegistry,
        controlPlaneDisks: roleDiskSpecs(srcCluster, "control-plane").map(disk => ({ ...disk })),
        workerDisks: roleDiskSpecs(srcCluster, "worker").map(disk => ({ ...disk })),
        vmDefaults: srcCluster.vmDefaults ? structuredClone(srcCluster.vmDefaults) : undefined,
        recert: srcCluster.recert,
        monitorHealth: srcCluster.monitorHealth,
        installOnDeploy: srcCluster.installOnDeploy,
        // A clone is never the (single) bastion-browser owner by default.
        configureBastionBrowser: false,
        networkIds: [],
      };
      // Mirror the shape onto the boundary node so the header + materialize match.
      const boundaryNode = {
        ...node,
        data: {
          ...(node.data as Record<string, unknown>),
          type: merged.type,
          controlPlane: merged.controlPlane,
          workers: merged.workers,
          baseDomain: merged.baseDomain,
          controlPlaneDisks: merged.controlPlaneDisks,
          workerDisks: merged.workerDisks,
          vmDefaults: merged.vmDefaults,
        },
      };
      st.addNode(boundaryNode);
      st.addCluster(merged);
      const { nodes: withMembers, edges: memberEdges } = materializeClusterInto(
        merged,
        useCanvasStore.getState().nodes,
      );
      const edgeMap = new Map(useCanvasStore.getState().edges.map((e) => [e.id, e]));
      memberEdges.forEach((e) => edgeMap.set(e.id, e));
      useCanvasStore.setState({
        nodes: withMembers,
        edges: Array.from(edgeMap.values()),
        selectedNodeId: boundaryNode.id,
      });
    });
    return () => onRequestDuplicateCluster(null);
  }, []);

  const allNodes = useCanvasStore((s) => s.nodes);
  const allEdges = useCanvasStore((s) => s.edges);
  const hiddenNodeIds = useCanvasStore((s) => s.hiddenNodeIds);
  const projectState = useCanvasStore((s) => s.projectState);
  const canvasLocked = ["deploying", "reconfiguring", "starting", "stopping"].includes(projectState);
  const nodes = allNodes;

  // Ctrl+Z / Ctrl+Shift+Z for undo/redo
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "z") {
        e.preventDefault();
        if (e.shiftKey) {
          useCanvasStore.getState().redo();
        } else {
          useCanvasStore.getState().undo();
        }
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  const visibleNodes = useMemo(
    () => allNodes.filter((n) => !hiddenNodeIds.includes(n.id)),
    [allNodes, hiddenNodeIds],
  );
  // Cluster-member VMs don't render nic handles (their network wiring is shown via
  // the cluster box's edge, not per-member); an edge to a member nic can't attach,
  // which spams React Flow "couldn't create edge for target handle". Keep such
  // edges in the store (deploy resolves VM networks from them) but don't render
  // them.
  const clusterMemberIds = useMemo(
    () =>
      new Set(
        allNodes
          .filter(
            (n) =>
              n.type === "vmNode" &&
              !!(n.data as Record<string, unknown>)?.clusterId,
          )
          .map((n) => n.id),
      ),
    [allNodes],
  );
  const visibleEdges = useMemo(
    () =>
      allEdges
        .filter(
          (e) =>
            !hiddenNodeIds.includes(e.source) &&
            !hiddenNodeIds.includes(e.target) &&
            !(
              clusterMemberIds.has(e.target) &&
              (e.targetHandle || "").startsWith("nic-")
            ),
        )
        .map((e) =>
          isClusterAnchorEdge(e)
            ? { ...e, type: CLUSTER_ANCHOR_EDGE_TYPE, zIndex: -1 }
            : withStorageFlowAnimation(e, allNodes),
        ),
    [allEdges, hiddenNodeIds, clusterMemberIds, allNodes],
  );
  const onNodesChange = useCanvasStore((s) => s.onNodesChange);
  const storeOnEdgesChange = useCanvasStore((s) => s.onEdgesChange);
  const storeOnConnect = useCanvasStore((s) => s.onConnect);
  const clusters = useCanvasStore((s) => s.clusters);
  const updateCluster = useCanvasStore((s) => s.updateCluster);
  const setSelectedNode = useCanvasStore((s) => s.setSelectedNode);
  const updateNodeData = useCanvasStore((s) => s.updateNodeData);
  const unhideNode = useCanvasStore((s) => s.unhideNode);
  const unhideAll = useCanvasStore((s) => s.unhideAll);
  const showMinimap = useCanvasStore((s) => s.showMinimap);
  const panMode = useCanvasStore((s) => s.panMode);

  // Wrapper for onConnect to handle cluster network anchors
  const onConnect = useCallback(
    (connection: Parameters<typeof storeOnConnect>[0]) => {
      const sourceNode = allNodes.find((n) => n.id === connection.source);
      const targetNode = allNodes.find((n) => n.id === connection.target);
      if (!sourceNode || !targetNode) return;

      // Cluster network anchor: network → cluster-net-top/bottom
      if (
        targetNode.type === "clusterNode" &&
        (connection.targetHandle?.startsWith("cluster-net-") ?? false)
      ) {
        const sSub = (sourceNode.data as Record<string, unknown>).subtype as string | undefined;
        const sIsNetwork = sourceNode.type === "networkNode" && sSub === "network";
        const isBmc = sourceNode.type === "networkNode" && (sourceNode.data as Record<string, unknown>).networkType === "bmc";
        if (!sIsNetwork || isBmc) {
          return; // Reject non-network, non-network subtypes, and BMC networks
        }
        // Add network to cluster's networkIds
        const clusterId = targetNode.id;
        const networkId = sourceNode.id;
        const cluster = clusters.find((c) => c.nodeId === clusterId);
        if (!cluster) return;
        const newNetworkIds = [...(cluster.networkIds ?? [])];
        if (!newNetworkIds.includes(networkId)) {
          newNetworkIds.push(networkId);
        }
        // FIX 2: Create and persist the anchor edge so it renders and can be deleted
        const anchorEdge = {
          id: `edge-clusternet-${networkId}-to-${clusterId}`,
          source: networkId,
          target: clusterId,
          sourceHandle: connection.sourceHandle,
          targetHandle: connection.targetHandle,
          type: CLUSTER_ANCHOR_EDGE_TYPE,
          animated: true,
          style: { stroke: "rgba(34,211,238,0.7)", strokeWidth: 2 },
        };
        // Update cluster and apply networks to members
        const updated = { ...cluster, networkIds: newNetworkIds };
        useCanvasStore.getState().pushHistory();
        const { nodes: nextNodes, edges: nextEdges } = applyClusterNetworks(
          updated,
          useCanvasStore.getState().nodes,
          useCanvasStore.getState().edges,
        );
        const edgeMap = new Map(nextEdges.map((e) => [e.id, e]));
        edgeMap.set(anchorEdge.id, anchorEdge);
        const edgesWithAnchor = Array.from(edgeMap.values());
        const synced = syncClusterCanvasState(updated, nextNodes, edgesWithAnchor);
        updateCluster(synced.cluster.id, synced.cluster);
        useCanvasStore.setState({ nodes: synced.nodes, edges: edgesWithAnchor });
        return;
      }

      // Fall back to store onConnect for all other connection types
      storeOnConnect(connection);
    },
    [allNodes, clusters, updateCluster, storeOnConnect],
  );

  // Keep cluster membership changes inside the store's confirmed removal transaction.
  const onEdgesChange = useCallback(
    (changes: Parameters<typeof storeOnEdgesChange>[0]) => storeOnEdgesChange(changes, () => {
      for (const change of changes) {
        if (change.type !== "remove") continue;
        const edge = useCanvasStore.getState().edges.find((candidate) => candidate.id === change.id);
        if (edge?.targetHandle?.startsWith("cluster-net-")) removeClusterNetworkAnchor(edge);
      }
    }),
    [storeOnEdgesChange],
  );

  const onNodeClick = useCallback(
    (_event: React.MouseEvent, node: Node) => {
      setSelectedNode(node.id);
      setContextMenu(null);
      setEdgeContextMenu(null);
    },
    [setSelectedNode],
  );

  const onNodeDoubleClick = useCallback(
    (_event: React.MouseEvent, node: Node) => {
      setSelectedNode(node.id);
      setContextMenu(null);
      setEdgeContextMenu(null);
      const propsPanel = document.querySelector(".canvas-properties");
      if (propsPanel) {
        propsPanel.scrollTo({ top: 0, behavior: "smooth" });
        const firstInput = propsPanel.querySelector("input");
        if (firstInput) setTimeout(() => firstInput.focus(), 100);
      }
    },
    [setSelectedNode],
  );

  const onNodeContextMenu = useCallback(
    (event: React.MouseEvent, node: Node) => {
      event.preventDefault();
      // Cluster-member VMs are managed by the OCP box — suppress the per-VM
      // context menu BEFORE deploy (also avoids a stray connection-line artifact
      // on right-click). Once deployed, keep it: the menu provides start/stop/
      // restart/console for the running member.
      if (node.type === "vmNode" && (node.data as Record<string, unknown>)?.clusterId) {
        if (!useCanvasStore.getState().deployedVmIds.has(node.id)) return;
      }
      setContextMenu({ nodeId: node.id, x: event.clientX, y: event.clientY });
      setSelectedNode(node.id);
    },
    [setSelectedNode],
  );

  // Dragging an RHCOS VM into/out of a cluster boundary sets/clears its
  // membership (data.clusterId + React Flow parentId). The point-in-rect and
  // absolute/relative position math live in the pure clusterMembership helpers;
  // the v12 gotcha is that setting parentId makes `position` relative, so we
  // convert absolute↔relative to keep the node visually put on reparent.
  const onNodeDragStop = useCallback(
    (_event: MouseEvent | TouchEvent, node: Node) => {
      if (node.type !== "vmNode" || (node.data as Record<string, unknown>)?.os !== "rhcos") return;
      const clusters = allNodes.filter((n) => n.type === "clusterNode");
      if (clusters.length === 0 && !node.parentId) return;

      const absPos = absolutePosition(node, clusters);
      const membership = resolveMembership({ ...node, position: absPos }, clusters);
      const currentClusterId = ((node.data as Record<string, unknown>)?.clusterId as string) ?? null;
      const unchanged =
        membership.clusterId === currentClusterId &&
        (membership.parentId ?? null) === (node.parentId ?? null);
      if (unchanged) return;

      const newParent = membership.parentId ? clusters.find((c) => c.id === membership.parentId) ?? null : null;
      const newPosition = relativePosition(absPos, newParent);

      // Structural change first: set parentId + reparented position. React Flow
      // v12 requires a child to appear AFTER its parent in the array, so on
      // assignment reorder the node to immediately follow its cluster boundary.
      const withParent = useCanvasStore.getState().nodes.map((n) =>
        n.id === node.id ? { ...n, parentId: membership.parentId ?? undefined, position: newPosition } : n,
      );
      const ordered = membership.parentId
        ? orderChildAfterParent(withParent, node.id, membership.parentId)
        : withParent;
      useCanvasStore.setState({ nodes: ordered });
      // Then the data + dirty recompute (runs over the final, reordered nodes).
      // A VM newly assigned to a cluster with no role yet defaults to worker
      // (see assignmentDataPatch); an existing role is never overridden.
      updateNodeData(
        node.id,
        assignmentDataPatch(node, membership.clusterId ?? null, currentClusterId),
      );
    },
    [allNodes, updateNodeData],
  );

  const onEdgeContextMenu = useCallback(
    (event: React.MouseEvent, edge: { id: string }) => {
      event.preventDefault();
      setEdgeContextMenu({ edgeId: edge.id, x: event.clientX, y: event.clientY });
      setContextMenu(null);
    },
    [],
  );

  const onSelectionChange = useCallback(
    ({ nodes: selected }: OnSelectionChangeParams) => {
      setSelectedNodes(selected);
    },
    [],
  );

  const onPaneClick = useCallback(() => {
    setSelectedNode(null);
    setContextMenu(null);
    setEdgeContextMenu(null);
    setSelectedNodes([]);
  }, [setSelectedNode]);

  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
  }, []);

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();

      const raw = event.dataTransfer.getData("application/troshka-node");
      if (!raw) return;

      let item: {
        type: string;
        label: string;
        icon: string;
        defaults?: Record<string, unknown>;
      };
      try {
        item = JSON.parse(raw);
      } catch {
        return;
      }

      const position = screenToFlowPosition({
        x: event.clientX,
        y: event.clientY,
      });

      dropPaletteItem(item, position);
    },
    [screenToFlowPosition],
  );

  const stableNodeTypes = useMemo(() => nodeTypes, []);

  const isValidConnection = useCallback(
    (connection: Connection | Edge) => validCanvasConnection(connection, allNodes),
    [allNodes],
  );

  return (
    <div className="canvas-wrapper" ref={reactFlowWrapper}>
      <ReactFlow
        {...(canvasLocked ? {} : { onNodesChange, onEdgesChange, onConnect, onNodeDoubleClick, onNodeContextMenu, onNodeDragStop, onEdgeContextMenu, onDragOver, onDrop })}
        nodes={visibleNodes}
        edges={visibleEdges}
        isValidConnection={canvasLocked ? () => false : isValidConnection}
        onNodeClick={onNodeClick}
        onPaneClick={onPaneClick}
        nodeTypes={stableNodeTypes}
        edgeTypes={edgeTypes}
        onSelectionChange={onSelectionChange}
        selectionMode={SelectionMode.Partial}
        nodesDraggable={!canvasLocked}
        nodesConnectable={!canvasLocked}
        panOnDrag={panMode}
        selectionOnDrag={!panMode}
        multiSelectionKeyCode="Shift"
        connectionMode={ConnectionMode.Loose}
        defaultEdgeOptions={{ type: "smoothstep" }}
        connectionLineType={ConnectionLineType.SmoothStep}
        colorMode={isDark ? "dark" : "light"}
        fitView
        deleteKeyCode={canvasLocked ? null : ["Backspace", "Delete"]}
        proOptions={{ hideAttribution: true }}
      >
        <Background
          variant={BackgroundVariant.Dots}
          gap={24}
          size={1}
          color="var(--troshka-canvas-dots)"
        />
        <CanvasToolbar />
        {showMinimap && (
          <MiniMap
            nodeStrokeWidth={3}
            pannable
            zoomable
            style={{
              background: "var(--troshka-surface)",
              borderRadius: 8,
            }}
            maskColor="var(--troshka-minimap-mask)"
          />
        )}
      </ReactFlow>
      <ClusterInstallLogModal />
      {selectedNodes.length > 1 && <SelectionToolbar selectedNodes={selectedNodes} setSelectedNodes={setSelectedNodes} />}

      {hiddenNodeIds.length > 0 && (
        <div className="hidden-items-bar">
          <span className="hidden-items-count">👁 {hiddenNodeIds.length} hidden</span>
          {hiddenNodeIds.map((nid) => {
            const n = nodes.find((node) => node.id === nid);
            if (!n) return null;
            const name = (n.data as Record<string, unknown>).name as string || nid;
            return (
              <button type="button" key={nid} onClick={() => unhideNode(nid)} title={`Show ${name}`}>
                {name}
              </button>
            );
          })}
          <button type="button" className="show-all" onClick={unhideAll}>Show All</button>
        </div>
      )}
      {contextMenu && (
        <NodeContextMenu
          nodeId={contextMenu.nodeId}
          x={contextMenu.x}
          y={contextMenu.y}
          onClose={() => setContextMenu(null)}
          onSnapshotVM={onSnapshotVM}
          onDuplicateVM={(id) => setDuplicateVmId(id)}
          onRunWorkload={onRunWorkload}
        />
      )}
      {duplicateVmId && (() => {
        const vmNode = allNodes.find((n) => n.id === duplicateVmId);
        const vmData = (vmNode?.data ?? {}) as Record<string, unknown>;
        return (
          <DuplicateVMModal
            vmName={vmData.name as string || "VM"}
            hasUuid={!!(vmData.smbiosUuid || vmData.domainUuid)}
            onConfirm={(cloneUuid) => {
              useCanvasStore.getState().duplicateNode(duplicateVmId, { cloneUuid });
              setDuplicateVmId(null);
            }}
            onCancel={() => setDuplicateVmId(null)}
          />
        );
      })()}
      {edgeContextMenu && (
        <EdgeContextMenu
          edgeId={edgeContextMenu.edgeId}
          x={edgeContextMenu.x}
          y={edgeContextMenu.y}
          onClose={() => setEdgeContextMenu(null)}
        />
      )}
    </div>
  );
}
