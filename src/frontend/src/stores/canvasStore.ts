import type React from "react";
import { create } from "zustand";
import { persist } from "zustand/middleware";
import {
  type Node,
  type Edge,
  type OnNodesChange,
  type OnEdgesChange,
  type OnConnect,
  applyNodeChanges,
  applyEdgeChanges,
  addEdge,
} from "@xyflow/react";

/* ---------- Node data shapes ---------- */

export interface VMNic {
  id: string;
  name: string;
  mac: string;
  model: string;
  ip?: string;
}

export interface VMDiskController {
  id: string;
  name: string;
  bus: string;
}

export interface VMNodeData {
  label: string;
  name: string;
  vcpus: number;
  ram: number;
  os: string;
  status: "running" | "stopped" | "redeploying";
  bootOrder?: number;
  bootMethod?: string;
  cloudInit?: boolean;
  icon: string;
  nics: VMNic[];
  diskControllers: VMDiskController[];
  tags?: Record<string, string>;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  [key: string]: any;
}

export interface NetworkNodeData {
  label: string;
  name: string;
  subtype: "network" | "router" | "gateway" | "dhcp" | "dns";
  cidr: string;
  dhcp: boolean;
  dns: boolean;
  dnsDomain?: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  [key: string]: any;
}

export interface StorageNodeData {
  label: string;
  name: string;
  size: number;
  format: "qcow2" | "raw" | "iso";
  icon: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  [key: string]: any;
}

export interface ContainerMount {
  diskNodeId: string;
  mountPath: string;
}

export interface ContainerPort {
  containerPort: number;
  hostPort?: number;
  protocol: "tcp" | "udp";
}

export interface ContainerEnvVar {
  key: string;
  value: string;
}

export interface PodContainer {
  name: string;
  image: string;
  registryCredentialId?: string | null;
  cpus: number;
  memory: number;
  envVars: ContainerEnvVar[];
  ports: ContainerPort[];
  command: string | null;
  mounts: ContainerMount[];
}

export interface ContainerNodeData {
  label: string;
  name: string;
  image: string;
  registryCredentialId: string | null;
  cpus: number;
  memory: number;
  nics: VMNic[];
  envVars: ContainerEnvVar[];
  ports: ContainerPort[];
  command: string | null;
  restartPolicy: "always" | "on-failure" | "never";
  privileged: boolean;
  mounts: ContainerMount[];
  status: "running" | "stopped" | "created";
  icon: string;
  isPod?: boolean;
  initContainers?: PodContainer[];
  podContainers?: PodContainer[];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  [key: string]: any;
}

export type CanvasNodeData = VMNodeData | NetworkNodeData | StorageNodeData | ContainerNodeData;

export interface ExternalIp {
  id: string;
  name: string;
  ip: string;
  _private_ip?: string;
  state?: "pending" | "allocated" | "associated";
}

export interface StartOrderEntry {
  vmId: string;
  containerId?: string;
  entryType?: "vm" | "container";
  autoStart: boolean;
  waitForVm: string | null;
  waitForService: string;
  waitForPort: string;
  delaySeconds: number;
}

/* ---------- Store ---------- */

interface CanvasState {
  nodes: Node[];
  edges: Edge[];
  selectedNodeId: string | null;
  currentProjectId: string | null;
  topologyDirty: boolean;
  projectState: string;
  deployedVmIds: Set<string>;
  deployedDiskSizes: Record<string, number>;
  deployedNodeData: Record<string, string>;
  deployedEdgeKey: string;
  showMinimap: boolean;
  hiddenNodeIds: string[];
  suppressDeleteWarning: boolean;
  panMode: boolean;
  providerType: string | null;

  // React Flow callbacks
  onNodesChange: OnNodesChange;
  onEdgesChange: OnEdgesChange;
  onConnect: OnConnect;

  // Actions
  addNode: (node: Node) => void;
  updateNodeData: (nodeId: string, data: Partial<Record<string, unknown>>) => void;
  setAllVmStatus: (status: "running" | "stopped") => void;
  deleteNode: (nodeId: string) => void;
  setSelectedNode: (nodeId: string | null) => void;
  toggleMinimap: () => void;
  getSelectedNode: () => Node | undefined;
  autoLayout: () => Promise<void>;
  duplicateNode: (nodeId: string) => void;
  deleteEdge: (edgeId: string) => void;
  loadProject: (projectId: string) => void;
  startOrder: StartOrderEntry[];
  setStartOrder: (order: StartOrderEntry[]) => void;
  externalIps: ExternalIp[];
  setExternalIps: (ips: ExternalIp[]) => void;
  hideNode: (nodeId: string) => void;
  unhideNode: (nodeId: string) => void;
  unhideAll: () => void;
  getVisibleNodes: () => Node[];
  getVisibleEdges: () => Edge[];

  // Undo/Redo
  pushHistory: () => void;
  undo: () => void;
  redo: () => void;
  canUndo: boolean;
  canRedo: boolean;
}

// Undo/redo history (not persisted)
interface HistoryEntry { nodes: Node[]; edges: Edge[]; hiddenNodeIds: string[] }
const _undoStack: HistoryEntry[] = [];
const _redoStack: HistoryEntry[] = [];
const MAX_HISTORY = 50;

export function generateNodeId(): string {
  return crypto.randomUUID();
}

export function generateNicId(): string {
  return `nic-${crypto.randomUUID()}`;
}

export function generateDiskControllerId(): string {
  return `dp-${crypto.randomUUID()}`;
}

export function generateMac(): string {
  const hex = () => Math.floor(Math.random() * 256).toString(16).padStart(2, "0");
  return `52:54:00:${hex()}:${hex()}:${hex()}`;
}

export function setLatestVmStates(states: Record<string, string>) {
  _latestVmStates = states;
  const store = useCanvasStore.getState();
  if (!store.nodes.length) return;
  let changed = false;
  const updated = store.nodes.map((n) => {
    if (n.type !== "vmNode" || !n.id) return n;
    const newStatus = states[n.id];
    if (newStatus && (n.data as Record<string, unknown>).status !== newStatus) {
      changed = true;
      return { ...n, data: { ...(n.data as Record<string, unknown>), status: newStatus } };
    }
    return n;
  });
  if (changed) useCanvasStore.setState({ nodes: updated });
}

let _latestContainerStates: Record<string, { state: string; ips?: string[] }> = {};
export function setLatestContainerStates(states: Record<string, { state: string; ips?: string[] }>) {
  _latestContainerStates = states;
  const store = useCanvasStore.getState();
  if (!store.nodes.length) return;
  let changed = false;
  const updated = store.nodes.map((n) => {
    if (n.type !== "containerNode" || !n.id) return n;
    const info = states[n.id];
    if (!info) return n;
    const d = n.data as Record<string, unknown>;
    const newStatus = info.state;
    const newIps = info.ips || [];
    if (d.status !== newStatus || JSON.stringify(d.liveIps) !== JSON.stringify(newIps)) {
      changed = true;
      return { ...n, data: { ...d, status: newStatus, liveIps: newIps } };
    }
    return n;
  });
  if (changed) useCanvasStore.setState({ nodes: updated });
}

export function computeTopologyDirty(state: { nodes: Node[]; edges: Edge[]; deployedNodeData: Record<string, string>; deployedEdgeKey: string }): boolean {
  const { nodes, edges, deployedNodeData, deployedEdgeKey } = state;
  if (!deployedEdgeKey && !Object.keys(deployedNodeData).length) return false;
  const currentNodeIds = nodes.map((n) => n.id).sort().join(",");
  const deployedNodeIds = Object.keys(deployedNodeData).sort().join(",");
  if (currentNodeIds !== deployedNodeIds) return true;
  const edgeKey = edges.map((e) => `${e.source}-${e.sourceHandle || ""}-${e.target}-${e.targetHandle || ""}`).sort().join("|");
  if (edgeKey !== deployedEdgeKey) return true;
  for (const n of nodes) {
    const deployed = deployedNodeData[n.id];
    if (!deployed) return true;
    const { status, redeployStep, redeployDetail, liveBootDevs, ...stable } = (n.data || {}) as Record<string, unknown>;
    if (JSON.stringify(stable) !== deployed) return true;
  }
  return false;
}

export const useCanvasStore = create<CanvasState>()(persist((set, get) => ({
  nodes: [],
  edges: [],
  selectedNodeId: null,
  showMinimap: true,
  hiddenNodeIds: [],
  suppressDeleteWarning: false,
  panMode: true,
  providerType: null,
  currentProjectId: null as string | null,
  projectState: "draft" as string,
  deployedVmIds: new Set<string>(),
  deployedDiskSizes: {} as Record<string, number>,
  deployedNodeData: {} as Record<string, string>,
  deployedEdgeKey: "",
  topologyDirty: false,
  startOrder: [] as StartOrderEntry[],
  externalIps: [] as ExternalIp[],

  onNodesChange: (changes) => {
    const removals = changes.filter((c) => c.type === "remove");
    const others = changes.filter((c) => c.type !== "remove");

    // Always apply non-removal changes first (selection, position, etc.)
    const updatedNodes = applyNodeChanges(others, get().nodes);

    // Sync selectedNodeId from React Flow's selection state
    const selected = updatedNodes.find((n) => n.selected);
    if (selected && selected.id !== get().selectedNodeId) {
      set({ selectedNodeId: selected.id });
    }

    if (removals.length > 0) {
      if (!get().suppressDeleteWarning) {
        const names = removals
          .map((r) => {
            const node = updatedNodes.find((n) => n.id === (r as { id: string }).id);
            return node ? (node.data as Record<string, any>).name as string || node.id : (r as { id: string }).id;
          })
          .join(", ");
        if (!window.confirm(`Delete ${removals.length > 1 ? `${removals.length} items` : names}?`)) {
          set({ nodes: updatedNodes });
          return;
        }
      }
      get().pushHistory();
      const removedIds = new Set(removals.map((r) => (r as { id: string }).id));
      set({
        nodes: applyNodeChanges(removals, updatedNodes),
        edges: get().edges.filter((e) => !removedIds.has(e.source) && !removedIds.has(e.target)),
        selectedNodeId: removedIds.has(get().selectedNodeId || "") ? null : get().selectedNodeId,
      });
      set({ topologyDirty: computeTopologyDirty(get()) });
    } else {
      set({ nodes: updatedNodes });
    }
  },

  onEdgesChange: (changes) => {
    const removals = changes.filter((c) => c.type === "remove");
    const others = changes.filter((c) => c.type !== "remove");

    if (removals.length > 0 && !get().suppressDeleteWarning) {
      if (!window.confirm(`Delete ${removals.length} connection${removals.length > 1 ? "s" : ""}?`)) {
        if (others.length > 0) set({ edges: applyEdgeChanges(others, get().edges) });
        return;
      }
      get().pushHistory();
    }
    set({ edges: applyEdgeChanges(changes, get().edges) });
    if (removals.length > 0) set({ topologyDirty: computeTopologyDirty(get()) });
  },

  onConnect: (connection) => {
    const sourceNode = get().nodes.find((n) => n.id === connection.source);
    const targetNode = get().nodes.find((n) => n.id === connection.target);
    if (!sourceNode || !targetNode) return;

    const sType = sourceNode.type;
    const tType = targetNode.type;
    const sSub = (sourceNode.data as Record<string, any>).subtype as string | undefined;
    const tSub = (targetNode.data as Record<string, any>).subtype as string | undefined;

    const sIsRouter = sType === "networkNode" && sSub === "router";
    const tIsRouter = tType === "networkNode" && tSub === "router";
    const sIsGateway = sType === "networkNode" && sSub === "gateway";
    const tIsGateway = tType === "networkNode" && tSub === "gateway";
    const sIsLoadBalancer = sType === "networkNode" && sSub === "loadbalancer";
    const tIsLoadBalancer = tType === "networkNode" && tSub === "loadbalancer";
    const sIsNetwork = sType === "networkNode" && !sIsRouter && !sIsGateway && !sIsLoadBalancer;
    const tIsNetwork = tType === "networkNode" && !tIsRouter && !tIsGateway && !tIsLoadBalancer;
    const sIsContainer = sType === "containerNode";
    const tIsContainer = tType === "containerNode";

    // Containers can only connect to networks (via NIC handles) and storage (via mount handles)
    if (sIsContainer || tIsContainer) {
      const otherType = sIsContainer ? tType : sType;
      if (otherType !== "networkNode" && otherType !== "storageNode") return;
      // Containers cannot connect to routers/gateways/loadbalancers directly
      const otherSub = sIsContainer
        ? (targetNode.data as Record<string, any>).subtype
        : (sourceNode.data as Record<string, any>).subtype;
      if (otherSub === "router" || otherSub === "gateway" || otherSub === "loadbalancer") return;
    }

    // Router/Gateway can only connect to networks
    if (sIsRouter || sIsGateway) {
      if (!tIsNetwork) return;
    }
    if (tIsRouter || tIsGateway) {
      if (!sIsNetwork) return;
    }

    // No duplicate connections between the same network and router/gateway
    if ((sIsRouter || sIsGateway) && tIsNetwork) {
      const alreadyConnected = get().edges.some(
        (e) => (e.source === sourceNode.id && e.target === targetNode.id) ||
               (e.source === targetNode.id && e.target === sourceNode.id)
      );
      if (alreadyConnected) return;
    }
    if ((tIsRouter || tIsGateway) && sIsNetwork) {
      const alreadyConnected = get().edges.some(
        (e) => (e.source === sourceNode.id && e.target === targetNode.id) ||
               (e.source === targetNode.id && e.target === sourceNode.id)
      );
      if (alreadyConnected) return;
    }

    // Storage can only connect to VMs and containers, and only one VM/container per disk (ISOs exempt)
    if (sType === "storageNode" && tType !== "vmNode" && tType !== "containerNode") return;
    if (tType === "storageNode" && sType !== "vmNode" && sType !== "containerNode") return;

    const storageId = sType === "storageNode" ? sourceNode.id : tType === "storageNode" ? targetNode.id : null;
    if (storageId) {
      const storageNode = get().nodes.find((n) => n.id === storageId);
      const isIso = storageNode && (storageNode.data as Record<string, any>).format === "iso";
      if (!isIso) {
        const alreadyConnected = get().edges.some(
          (e) => e.source === storageId || e.target === storageId
        );
        if (alreadyConnected) return;
      }
    }

    // VMs connect to networks and storage only
    if (sType === "vmNode" && tType === "vmNode") return;
    if (sType === "vmNode" && (tIsRouter || tIsGateway)) return;
    if (tType === "vmNode" && (sIsRouter || sIsGateway)) return;

    // Networks don't connect to other networks
    if (sIsNetwork && tIsNetwork) return;

    // BMC network: only VMs can connect (the provisioner)
    const isBmcSource = sourceNode?.type === "networkNode" && (sourceNode.data as Record<string, any>).networkType === "bmc";
    const isBmcTarget = targetNode?.type === "networkNode" && (targetNode.data as Record<string, any>).networkType === "bmc";
    if ((isBmcSource || isBmcTarget) && (sourceNode?.type !== "vmNode" && targetNode?.type !== "vmNode")) {
      return;
    }

    let edgeStyle: React.CSSProperties;
    let animated = false;

    let className = "";

    if (sType === "storageNode" || tType === "storageNode") {
      edgeStyle = {
        stroke: "rgba(251,191,36,0.6)",
        strokeWidth: 2,
        strokeDasharray: "4 4",
      };
      className = "edge-storage-pulse";
    } else if (sIsRouter || tIsRouter) {
      edgeStyle = {
        stroke: "rgba(251,146,60,0.5)",
        strokeWidth: 2,
        strokeDasharray: "8 4",
      };
      animated = true;
    } else if (sIsGateway || tIsGateway) {
      edgeStyle = {
        stroke: "rgba(74,222,128,0.5)",
        strokeWidth: 2,
        strokeDasharray: "8 4",
      };
      animated = true;
    } else if (isBmcSource || isBmcTarget) {
      edgeStyle = {
        stroke: "rgba(168,85,247,0.5)",
        strokeWidth: 2,
        strokeDasharray: "6 4",
      };
      animated = true;
    } else if (
      (sourceNode.data as any).networkType === "loadbalancer" ||
      (targetNode.data as any).networkType === "loadbalancer"
    ) {
      edgeStyle = {
        stroke: "rgba(59,130,246,0.5)",
        strokeWidth: 2,
        strokeDasharray: "6 4",
      };
      animated = true;
    } else {
      edgeStyle = {
        stroke: "rgba(34,211,238,0.5)",
        strokeWidth: 2,
        strokeDasharray: "6 4",
      };
      animated = true;
    }

    // Auto-add NIC before creating edge when connecting a network to a VM with no matching handle
    const finalConnection = { ...connection };
    if ((sType === "networkNode" && tType === "vmNode") || (tType === "networkNode" && sType === "vmNode")) {
      const vmNode = sType === "vmNode" ? sourceNode : targetNode;
      const netNode = sType === "networkNode" ? sourceNode : targetNode;
      const vmHandle = sType === "vmNode" ? connection.sourceHandle : connection.targetHandle;
      const vmNics = ((vmNode.data as Record<string, any>).nics || []) as Array<{id: string; name: string; mac: string; model: string; ip?: string}>;
      const nicForHandle = vmNics.find((nic) =>
        vmHandle === `nic-${nic.id}-top` || vmHandle === `nic-${nic.id}-bottom`
      );
      const nicHandleTop = nicForHandle ? `nic-${nicForHandle.id}-top` : null;
      const nicHandleBottom = nicForHandle ? `nic-${nicForHandle.id}-bottom` : null;
      const handleAlreadyConnected = nicForHandle && get().edges.some((e) =>
        (e.source === vmNode.id && (e.sourceHandle === nicHandleTop || e.sourceHandle === nicHandleBottom)) ||
        (e.target === vmNode.id && (e.targetHandle === nicHandleTop || e.targetHandle === nicHandleBottom))
      );
      if (!nicForHandle || handleAlreadyConnected) {
        const netData = netNode.data as Record<string, any>;
        const cidr = netData.cidr || "";
        const base = cidr ? cidr.split("/")[0].split(".").slice(0, 3).join(".") : "";
        let autoIp = "";
        if (base) {
          const usedIps = new Set<string>();
          for (const n of get().nodes) {
            if (n.type !== "vmNode") continue;
            for (const nic of ((n.data as Record<string, any>).nics || []) as Array<{ip?: string}>) {
              if (nic.ip) usedIps.add(nic.ip);
            }
          }
          for (let i = 10; i < 250; i++) {
            const candidate = `${base}.${i}`;
            if (!usedIps.has(candidate)) { autoIp = candidate; break; }
          }
        }
        const suffix = vmHandle?.endsWith("-top") ? "top" : "bottom";
        const newNic = {
          id: generateNicId(),
          name: `eth${vmNics.length}`,
          mac: generateMac(),
          model: "virtio",
          ...(autoIp ? { ip: autoIp } : {}),
        };
        const newHandle = `nic-${newNic.id}-${suffix}`;
        if (sType === "vmNode") finalConnection.sourceHandle = newHandle;
        else finalConnection.targetHandle = newHandle;
        set({
          nodes: get().nodes.map((n) =>
            n.id === vmNode.id
              ? { ...n, data: { ...n.data, nics: [...vmNics, newNic] } }
              : n
          ),
        });
      }
    }

    get().pushHistory();
    set({
      edges: addEdge(
        {
          ...finalConnection,
          type: "smoothstep",
          style: edgeStyle,
          animated,
          className,
        },
        get().edges,
      ),
    });

    // Force disk format to raw when connecting storage to a container
    const diskId = sType === "storageNode" ? sourceNode.id : tType === "storageNode" ? targetNode.id : null;
    const containerConnected = sType === "containerNode" || tType === "containerNode";
    if (diskId && containerConnected) {
      const storageData = (sType === "storageNode" ? sourceNode : targetNode).data as Record<string, unknown>;
      if (storageData.format !== "raw" && storageData.format !== "iso") {
        set({
          nodes: get().nodes.map((n) =>
            n.id === diskId
              ? { ...n, data: { ...n.data, format: "raw" } }
              : n
          ),
        });
      }
    }
    set({ topologyDirty: computeTopologyDirty(get()) });
  },

  addNode: (node) => {
    get().pushHistory();
    const nodes = [...get().nodes, node];
    set({ nodes });
    set({ topologyDirty: computeTopologyDirty(get()) });
  },

  updateNodeData: (nodeId, data) => {
    const isStatusOnly = Object.keys(data).length === 1 && "status" in data;
    const handlesChanged = "nics" in data || "diskControllers" in data;
    set({
      nodes: get().nodes.map((node) =>
        node.id === nodeId
          ? { ...node, data: { ...node.data, ...data } }
          : node,
      ),
      // Force React Flow to re-route edges by creating new edge references
      ...(handlesChanged ? { edges: get().edges.map((e) => ({ ...e })) } : {}),
    });
    if (!isStatusOnly) set({ topologyDirty: computeTopologyDirty(get()) });
  },

  setAllVmStatus: (status) => {
    set({
      nodes: get().nodes.map((node) =>
        node.type === "vmNode"
          ? { ...node, data: { ...node.data, status } }
          : node,
      ),
    });
  },

  deleteNode: (nodeId) => {
    get().pushHistory();
    set({
      nodes: get().nodes.filter((n) => n.id !== nodeId),
      edges: get().edges.filter(
        (e) => e.source !== nodeId && e.target !== nodeId,
      ),
      selectedNodeId:
        get().selectedNodeId === nodeId ? null : get().selectedNodeId,
    });
    set({ topologyDirty: computeTopologyDirty(get()) });
  },

  setSelectedNode: (nodeId) => {
    set({ selectedNodeId: nodeId });
  },

  toggleMinimap: () => {
    set({ showMinimap: !get().showMinimap });
  },

  getSelectedNode: () => {
    const { nodes, selectedNodeId } = get();
    return nodes.find((n) => n.id === selectedNodeId);
  },

  hideNode: (nodeId) => {
    const hidden = get().hiddenNodeIds;
    if (!hidden.includes(nodeId)) {
      set({
        hiddenNodeIds: [...hidden, nodeId],
        selectedNodeId: get().selectedNodeId === nodeId ? null : get().selectedNodeId,
      });
    }
  },

  unhideNode: (nodeId) => {
    set({ hiddenNodeIds: get().hiddenNodeIds.filter((id) => id !== nodeId) });
  },

  unhideAll: () => {
    set({ hiddenNodeIds: [] });
  },

  getVisibleNodes: () => {
    const { nodes, hiddenNodeIds } = get();
    return nodes.filter((n) => !hiddenNodeIds.includes(n.id));
  },

  getVisibleEdges: () => {
    const { edges, hiddenNodeIds } = get();
    return edges.filter(
      (e) => !hiddenNodeIds.includes(e.source) && !hiddenNodeIds.includes(e.target)
    );
  },

  deleteEdge: (edgeId) => {
    get().pushHistory();
    set({ edges: get().edges.filter((e) => e.id !== edgeId) });
  },

  pushHistory: () => {
    const { nodes, edges, hiddenNodeIds } = get();
    _undoStack.push({ nodes: structuredClone(nodes), edges: structuredClone(edges), hiddenNodeIds: [...hiddenNodeIds] });
    if (_undoStack.length > MAX_HISTORY) _undoStack.shift();
    _redoStack.length = 0;
    set({ canUndo: true, canRedo: false });
  },

  undo: () => {
    const entry = _undoStack.pop();
    if (!entry) return;
    const { nodes, edges, hiddenNodeIds } = get();
    _redoStack.push({ nodes: structuredClone(nodes), edges: structuredClone(edges), hiddenNodeIds: [...hiddenNodeIds] });
    set({ nodes: entry.nodes, edges: entry.edges, hiddenNodeIds: entry.hiddenNodeIds, selectedNodeId: null, canUndo: _undoStack.length > 0, canRedo: true });
  },

  redo: () => {
    const entry = _redoStack.pop();
    if (!entry) return;
    const { nodes, edges, hiddenNodeIds } = get();
    _undoStack.push({ nodes: structuredClone(nodes), edges: structuredClone(edges), hiddenNodeIds: [...hiddenNodeIds] });
    set({ nodes: entry.nodes, edges: entry.edges, hiddenNodeIds: entry.hiddenNodeIds, selectedNodeId: null, canUndo: true, canRedo: _redoStack.length > 0 });
  },

  canUndo: false,
  canRedo: false,

  loadProject: (projectId) => {
    const current = get().currentProjectId;

    _loadingProject = true;
    if (_saveTimer) { clearTimeout(_saveTimer); _saveTimer = null; }
    _undoStack.length = 0;
    _redoStack.length = 0;
    set({ canUndo: false, canRedo: false });

    // Only save+clear when switching to a different project
    if (current && current !== projectId) {
      if (get().nodes.length > 0) {
        _saveTopologyToApi(current, get());
      }
      set({ currentProjectId: projectId, nodes: [], edges: [], hiddenNodeIds: [], startOrder: [], externalIps: [], selectedNodeId: null });
    } else {
      set({ currentProjectId: projectId });
    }

    fetch(`/api/v1/projects/${projectId}`)
      .then((r) => r.ok ? r.json() : null)
      .then((project) => {
        if (project?.topology) {
          const t = project.topology;
          const prevNodes = get().nodes;
          const prevStatusMap: Record<string, string> = {};
          for (const n of prevNodes) {
            if (n.type === "vmNode" && n.data?.status) {
              prevStatusMap[n.id] = (n.data as Record<string, unknown>).status as string;
            }
            if (n.type === "containerNode" && n.data?.status) {
              prevStatusMap[n.id] = (n.data as Record<string, unknown>).status as string;
            }
          }
          const deployed = get().deployedVmIds;
          const nodes = (t.nodes || []).map((n: Record<string, unknown>) => {
            if (n.type === "containerNode" && n.id) {
              const ctrState = _latestContainerStates[n.id as string];
              if (ctrState) {
                return { ...n, data: { ...(n.data as Record<string, unknown>), status: ctrState } };
              }
              const prev = prevStatusMap[n.id as string];
              if (prev) {
                return { ...n, data: { ...(n.data as Record<string, unknown>), status: prev } };
              }
            }
            if (n.type === "vmNode" && n.id) {
              const wsState = _latestVmStates[n.id as string];
              if (wsState) {
                return { ...n, data: { ...(n.data as Record<string, unknown>), status: wsState } };
              }
              const prev = prevStatusMap[n.id as string];
              if (prev) {
                return { ...n, data: { ...(n.data as Record<string, unknown>), status: prev } };
              }
              if (deployed.has(n.id as string)) {
                return { ...n, data: { ...(n.data as Record<string, unknown>), status: "running" } };
              }
            }
            return n;
          });
          set({
            nodes,
            edges: t.edges || [],
            hiddenNodeIds: t.hiddenNodeIds || [],
            startOrder: t.startOrder || [],
            externalIps: t.externalIps || [],
          });
          _lastSavedNodeCount = (t.nodes || []).length;
        }

        // Expose BMC data to properties panel
        if (project?.bmc) {
          (window as any).__deployedTopology = { bmc: project.bmc };
        } else if (project?.deployed_topology?.bmc) {
          (window as any).__deployedTopology = project.deployed_topology;
        }

        // Clean up BMC data when project is in draft
        if (project?.state === "draft") {
          delete (window as any).__deployedTopology;
        }


        _loadingProject = false;
      })
      .catch(() => { _loadingProject = false; });
  },

  setStartOrder: (order) => {
    set({ startOrder: order });
  },

  setExternalIps: (ips) => {
    set({ externalIps: ips });
  },

  duplicateNode: (nodeId) => {
    const source = get().nodes.find((n) => n.id === nodeId);
    if (!source) return;

    const allNames = get().nodes.map((n) => (n.data as Record<string, any>).name as string).filter(Boolean);
    const baseName = (source.data as Record<string, any>).name as string || "node";

    const trailingNum = baseName.match(/^(.*?)[-_]?(\d+)$/);
    let newName: string;
    if (trailingNum) {
      const prefix = trailingNum[1];
      const sep = baseName.includes("-") ? "-" : "";
      let num = parseInt(trailingNum[2], 10) + 1;
      const padLen = trailingNum[2].length;
      while (allNames.includes(`${prefix}${sep}${String(num).padStart(padLen, "0")}`)) num++;
      newName = `${prefix}${sep}${String(num).padStart(padLen, "0")}`;
    } else {
      let suffix = 2;
      while (allNames.includes(`${baseName}-${suffix}`)) suffix++;
      newName = `${baseName}-${suffix}`;
    }

    const newId = generateNodeId();

    // Generate new NIC IDs and MACs for VMs
    let newData: Record<string, any> = { ...source.data, name: newName, label: newName };
    if (source.type === "vmNode") {
      const nics = (source.data as Record<string, any>).nics as Array<{id: string; name: string; mac: string; model: string; ip?: string}> || [];
      const usedIps = new Set<string>();
      for (const n of get().nodes) {
        if (n.type !== "vmNode") continue;
        for (const nic of ((n.data as Record<string, any>).nics || []) as Array<{ip?: string}>) {
          if (nic.ip) usedIps.add(nic.ip);
        }
      }
      newData = {
        ...newData,
        nics: nics.map((nic, i) => {
          let newIp = nic.ip;
          if (newIp) {
            const parts = newIp.split(".");
            const base = parts.slice(0, 3).join(".");
            let octet = parseInt(parts[3], 10) + 1;
            while (octet < 250 && usedIps.has(`${base}.${octet}`)) octet++;
            newIp = octet < 250 ? `${base}.${octet}` : newIp;
            usedIps.add(newIp);
          }
          return { ...nic, id: generateNicId(), mac: generateMac(), ...(newIp ? { ip: newIp } : {}) };
        }),
        diskControllers: ((source.data as Record<string, any>).diskControllers as Array<{id: string; name: string; bus: string}> || [])
          .map((dc) => ({ ...dc, id: generateDiskControllerId() })),
      };
      if (newData.bmcEnabled && newData.bmcIp) {
        newData.bmcIp = allocateBmcIp();
      }
    }

    const newNode: Node = {
      ...source,
      id: newId,
      position: { x: source.position.x + 40, y: source.position.y + 40 },
      selected: false,
      data: newData,
    };

    // Duplicate connections
    const newEdges: Edge[] = [];
    const newNodes: Node[] = [newNode];
    const diskIdMap: Record<string, string> = {};

    if (source.type === "vmNode") {
      const sourceEdges = get().edges.filter((e) => e.source === nodeId || e.target === nodeId);
      const oldNics = (source.data as Record<string, any>).nics as Array<{id: string}> || [];
      const newNics = (newData as Record<string, any>).nics as Array<{id: string}> || [];
      const oldDcs = (source.data as Record<string, any>).diskControllers as Array<{id: string}> || [];
      const newDcs = (newData as Record<string, any>).diskControllers as Array<{id: string}> || [];

      for (const edge of sourceEdges) {
        const otherNodeId = edge.source === nodeId ? edge.target : edge.source;
        const otherNode = get().nodes.find((n) => n.id === otherNodeId);
        if (!otherNode) continue;

        // Network connections: duplicate the edge pointing to the same network
        if (otherNode.type === "networkNode") {
          let newHandle = edge.source === nodeId ? edge.sourceHandle : edge.targetHandle;
          // Map old NIC handle to new NIC handle
          if (newHandle) {
            for (let i = 0; i < oldNics.length; i++) {
              if (newHandle.includes(oldNics[i].id) && newNics[i]) {
                newHandle = newHandle.replace(oldNics[i].id, newNics[i].id);
                break;
              }
            }
          }
          const newEdge: Edge = {
            ...edge,
            id: `${newId}-${otherNodeId}-${Date.now()}-${Math.random().toString(36).slice(2, 5)}`,
            source: edge.source === nodeId ? newId : otherNodeId,
            target: edge.target === nodeId ? newId : otherNodeId,
            sourceHandle: edge.source === nodeId ? newHandle : edge.sourceHandle,
            targetHandle: edge.target === nodeId ? newHandle : edge.targetHandle,
          };
          newEdges.push(newEdge);
        }

        // Storage connections: duplicate the disk (not ISOs) and connect
        if (otherNode.type === "storageNode") {
          let diskTargetId = otherNodeId;

          {
            // Clone the disk/ISO node visually (ISOs share the same backend image)
            const diskNewId = generateNodeId();
            const diskName = (otherNode.data as Record<string, any>).name as string || "disk";
            const diskTrailing = diskName.match(/^(.*?)[-_]?(\d+)$/);
            let diskNewName: string;
            if (diskTrailing) {
              const prefix = diskTrailing[1];
              const sep = diskName.includes("-") ? "-" : "";
              let num = parseInt(diskTrailing[2], 10) + 1;
              const padLen = diskTrailing[2].length;
              while (allNames.includes(`${prefix}${sep}${String(num).padStart(padLen, "0")}`)) num++;
              diskNewName = `${prefix}${sep}${String(num).padStart(padLen, "0")}`;
            } else {
              let suffix = 2;
              while (allNames.includes(`${diskName}-${suffix}`)) suffix++;
              diskNewName = `${diskName}-${suffix}`;
            }
            allNames.push(diskNewName);

            newNodes.push({
              ...otherNode,
              id: diskNewId,
              position: { x: otherNode.position.x + 40, y: otherNode.position.y + 40 },
              selected: false,
              data: { ...otherNode.data, name: diskNewName, label: diskNewName },
            });
            diskIdMap[otherNodeId] = diskNewId;
            diskTargetId = diskNewId;
          }

          let newHandle = edge.source === nodeId ? edge.sourceHandle : edge.targetHandle;
          if (newHandle) {
            for (let i = 0; i < oldDcs.length; i++) {
              if (newHandle.includes(oldDcs[i].id) && newDcs[i]) {
                newHandle = newHandle.replace(oldDcs[i].id, newDcs[i].id);
                break;
              }
            }
          }

          const newEdge: Edge = {
            ...edge,
            id: `${newId}-${diskTargetId}-${Date.now()}-${Math.random().toString(36).slice(2, 5)}`,
            source: edge.source === nodeId ? newId : diskTargetId,
            target: edge.target === nodeId ? newId : diskTargetId,
            sourceHandle: edge.source === nodeId ? newHandle : edge.sourceHandle,
            targetHandle: edge.target === nodeId ? newHandle : edge.targetHandle,
          };
          newEdges.push(newEdge);
        }
      }
    }

    // Remap bootDevices to cloned disk IDs
    if (source.type === "vmNode" && Object.keys(diskIdMap).length > 0) {
      const bootDevs = (newNode.data as Record<string, any>).bootDevices as string[] | undefined;
      if (bootDevs) {
        (newNode.data as Record<string, any>).bootDevices = bootDevs.map(
          (id) => diskIdMap[id] || id
        );
      }
    }

    set({
      nodes: [...get().nodes, ...newNodes],
      edges: [...get().edges, ...newEdges],
      selectedNodeId: newId,
    });
  },

  autoLayout: async () => {
    const nodes = get().nodes;
    const edges = get().edges;
    if (nodes.length === 0) return;

    try {
      const resp = await fetch("/api/v1/projects/auto-layout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nodes, edges }),
      });
      if (!resp.ok) return;
      const result = await resp.json();
      set({ nodes: result.nodes, edges: result.edges });
    } catch {
      // Layout is best-effort — don't break the UI if the backend is down
    }
  },
}), {
  name: "troshka-canvas-settings",
  partialize: (state) => ({
    showMinimap: state.showMinimap,
    suppressDeleteWarning: state.suppressDeleteWarning,
  }),
}));

/**
 * Auto-create or remove the BMC network node based on whether any VMs have BMC enabled.
 * Called by PropertiesPanel when bmcEnabled is toggled.
 */
export function syncBmcNetwork() {
  const state = useCanvasStore.getState();
  const nodes = state.nodes;

  const hasBmcVm = nodes.some(
    (n) => n.type === "vmNode" && (n.data as Record<string, any>).bmcEnabled
  );
  const bmcNetNode = nodes.find(
    (n) => n.type === "networkNode" && (n.data as Record<string, any>).networkType === "bmc"
  );

  if (hasBmcVm && !bmcNetNode) {
    // Auto-create BMC network node
    const chars = "abcdefghijklmnopqrstuvwxyz0123456789";
    const randomBytes = new Uint8Array(16);
    crypto.getRandomValues(randomBytes);
    const password = Array.from(randomBytes, (b) => chars[b % chars.length]).join("");

    // Position near the center of existing BMC-enabled VM nodes
    const vmNodes = nodes.filter((n) => n.type === "vmNode" && (n.data as Record<string, any>).bmcEnabled);
    const avgX = vmNodes.reduce((sum, n) => sum + (n.position?.x || 0), 0) / Math.max(vmNodes.length, 1);
    const avgY = vmNodes.reduce((sum, n) => sum + (n.position?.y || 0), 0) / Math.max(vmNodes.length, 1);

    const bmcNode = {
      id: `bmc-network-${Date.now()}`,
      type: "networkNode",
      position: { x: avgX + 300, y: avgY },
      data: {
        label: "BMC Network",
        name: "BMC Network",
        subtype: "network" as const,
        networkType: "bmc",
        cidr: "192.168.100.0/24",
        dhcp: true,
        dns: false,
        bmcUsername: "admin",
        bmcPassword: password,
      },
    };
    state.addNode(bmcNode);
  } else if (!hasBmcVm && bmcNetNode) {
    // Auto-remove BMC network and its edges
    state.deleteNode(bmcNetNode.id);
  }
}

/**
 * Allocate the next available BMC IP from the BMC network CIDR.
 */
export function allocateBmcIp(): string {
  const state = useCanvasStore.getState();
  const nodes = state.nodes;

  const bmcNet = nodes.find(
    (n) => n.type === "networkNode" && (n.data as Record<string, any>).networkType === "bmc"
  );
  const cidr = (bmcNet?.data as Record<string, any>)?.cidr || "192.168.100.0/24";
  const base = cidr.split("/")[0].split(".").slice(0, 3).join(".");

  // Collect all used BMC IPs
  const usedIps = new Set<string>();
  for (const n of nodes) {
    if (n.type === "vmNode") {
      const ip = (n.data as Record<string, any>).bmcIp;
      if (ip) usedIps.add(ip);
    }
  }

  // Allocate from .11 upward (gateway is .1)
  for (let i = 11; i < 250; i++) {
    const candidate = `${base}.${i}`;
    if (!usedIps.has(candidate)) return candidate;
  }
  return `${base}.11`;
}

// Save topology to API
function _saveTopologyToApi(projectId: string, state: { nodes: Node[]; edges: Edge[]; hiddenNodeIds: string[]; startOrder: StartOrderEntry[]; externalIps: ExternalIp[] }) {
  const cleanNodes = state.nodes.map((n) => {
    if (n.type !== "vmNode") return n;
    const { status, redeployStep, redeployDetail, ...rest } = n.data as Record<string, any>;
    return { ...n, data: rest };
  });
  const topology = {
    nodes: cleanNodes,
    edges: state.edges,
    hiddenNodeIds: state.hiddenNodeIds,
    startOrder: state.startOrder,
    externalIps: state.externalIps,
  };
  fetch(`/api/v1/projects/${projectId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ topology }),
  }).catch(() => {});
}

// Debounced auto-save to API — only save in draft mode
let _saveTimer: ReturnType<typeof setTimeout> | null = null;
let _lastSavedNodeCount = 0;
let _loadingProject = false;
let _latestVmStates: Record<string, string> = {};
let _lastSavedTopologyKey = "";
useCanvasStore.subscribe((state) => {
  if (!state.currentProjectId) return;
  if (state.projectState === "deploying" || state.projectState === "starting" || state.projectState === "stopping") return;
  if (_loadingProject) return;
  if (state.nodes.length === 0) return;

  // Recompute dirty flag on every change (only after deployed state is loaded)
  if (Object.keys(state.deployedNodeData).length > 0) {
    const dirty = computeTopologyDirty(state);
    if (dirty !== state.topologyDirty) {
      useCanvasStore.setState({ topologyDirty: dirty });
    }
  }

  if (_saveTimer) clearTimeout(_saveTimer);
  _saveTimer = setTimeout(() => {
    if (_loadingProject) return;
    const s = useCanvasStore.getState();
    if (s.nodes.length === 0) return;
    const topoKey = s.nodes.map((n) => {
      if (n.type === "vmNode") {
        const { status, redeployStep, redeployDetail, liveBootDevs, ...stable } = (n.data || {}) as Record<string, unknown>;
        return `${n.id}:${JSON.stringify(stable)}`;
      }
      return `${n.id}:${JSON.stringify(n.data)}`;
    }).join("|") + "||" + s.edges.map((e) => `${e.source}-${e.target}`).join("|");
    if (topoKey === _lastSavedTopologyKey) return;
    _lastSavedTopologyKey = topoKey;
    _lastSavedNodeCount = s.nodes.length;
    _saveTopologyToApi(s.currentProjectId!, s);
  }, 1000);
});
