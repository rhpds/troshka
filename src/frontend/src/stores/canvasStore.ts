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

export type CanvasNodeData = VMNodeData | NetworkNodeData | StorageNodeData;

export interface ExternalIp {
  id: string;
  name: string;
  ip: string;
  _private_ip?: string;
  state?: "pending" | "allocated" | "associated";
}

export interface StartOrderEntry {
  vmId: string;
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
  showMinimap: boolean;
  hiddenNodeIds: string[];
  suppressDeleteWarning: boolean;
  panMode: boolean;

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
  autoLayout: () => void;
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

export const useCanvasStore = create<CanvasState>()(persist((set, get) => ({
  nodes: [],
  edges: [],
  selectedNodeId: null,
  showMinimap: true,
  hiddenNodeIds: [],
  suppressDeleteWarning: false,
  panMode: true,
  currentProjectId: null as string | null,
  projectState: "draft" as string,
  deployedVmIds: new Set<string>(),
  deployedDiskSizes: {} as Record<string, number>,
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

      // Remove bmc NICs when disconnecting from BMC network
      const nodes = get().nodes;
      const edges = get().edges;
      for (const removal of removals) {
        const edge = edges.find((e) => e.id === (removal as { id: string }).id);
        if (!edge) continue;
        const srcNode = nodes.find((n) => n.id === edge.source);
        const tgtNode = nodes.find((n) => n.id === edge.target);
        const bmcNet = [srcNode, tgtNode].find((n) => n?.type === "networkNode" && (n.data as Record<string, any>).networkType === "bmc");
        const vmNode = [srcNode, tgtNode].find((n) => n?.type === "vmNode");
        if (bmcNet && vmNode) {
          const nics = ((vmNode.data as Record<string, any>).nics || []).filter(
            (nic: Record<string, string>) => !nic.name.startsWith("bmc")
          );
          get().updateNodeData(vmNode.id, { nics });
        }
      }
    }
    set({ edges: applyEdgeChanges(changes, get().edges) });
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
    const sIsNetwork = sType === "networkNode" && !sIsRouter && !sIsGateway;
    const tIsNetwork = tType === "networkNode" && !tIsRouter && !tIsGateway;

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

    // Storage can only connect to VMs, and only one VM per disk (ISOs exempt)
    if (sType === "storageNode" && tType !== "vmNode") return;
    if (tType === "storageNode" && sType !== "vmNode") return;

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
    } else {
      edgeStyle = {
        stroke: "rgba(34,211,238,0.5)",
        strokeWidth: 2,
        strokeDasharray: "6 4",
      };
      animated = true;
    }

    // Auto-add a bmc0 NIC and connect from its handle
    let bmcNicHandle: string | null = null;
    let bmcVmIsSource = false;
    if (isBmcSource || isBmcTarget) {
      const vmNode = sType === "vmNode" ? sourceNode : targetNode;
      bmcVmIsSource = sType === "vmNode";
      const existingNics = (vmNode.data as Record<string, any>).nics || [];
      let bmcNic = existingNics.find((n: Record<string, string>) => n.name.startsWith("bmc"));
      if (!bmcNic) {
        const nicId = generateNicId();
        const hex = () => Math.floor(Math.random() * 256).toString(16).padStart(2, "0");
        bmcNic = { id: nicId, name: "bmc0", mac: `52:54:01:${hex()}:${hex()}:${hex()}`, model: "virtio" };
        get().updateNodeData(vmNode.id, { nics: [...existingNics, bmcNic] });
      }
      bmcNicHandle = `${bmcNic.id}-top`;
    }

    get().pushHistory();

    // Create edge initially on the dragged handle, then swap to bmc0 after handles render
    set({
      edges: addEdge(
        {
          ...connection,
          type: "smoothstep",
          style: edgeStyle,
          animated,
          className,
        },
        get().edges,
      ),
      topologyDirty: true,
    });

    if (bmcNicHandle) {
      const handle = bmcNicHandle;
      const src = connection.source!;
      const tgt = connection.target!;
      const isSource = bmcVmIsSource;
      // Wait for React to render the new NIC handles, then swap the edge
      const trySwap = (attempts: number) => {
        if (attempts <= 0) return;
        requestAnimationFrame(() => {
          const el = document.querySelector(`[data-handleid="${handle}"]`);
          if (el) {
            set({
              edges: get().edges.map((e) => {
                if (e.source !== src || e.target !== tgt) return e;
                return isSource
                  ? { ...e, sourceHandle: handle, id: `xy-edge__${src}${handle}-${tgt}${e.targetHandle || ""}` }
                  : { ...e, targetHandle: handle, id: `xy-edge__${src}${e.sourceHandle || ""}-${tgt}${handle}` };
              }),
            });
          } else {
            setTimeout(() => trySwap(attempts - 1), 100);
          }
        });
      };
      trySwap(20);
    }
  },

  addNode: (node) => {
    get().pushHistory();
    set({ nodes: [...get().nodes, node], topologyDirty: true });
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
      ...(isStatusOnly ? {} : { topologyDirty: true }),
      // Force React Flow to re-route edges by creating new edge references
      ...(handlesChanged ? { edges: get().edges.map((e) => ({ ...e })) } : {}),
    });
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
      topologyDirty: true,
    });
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
    // Clean up bmc NICs if disconnecting from BMC network
    const edge = get().edges.find((e) => e.id === edgeId);
    if (edge) {
      const nodes = get().nodes;
      const srcNode = nodes.find((n) => n.id === edge.source);
      const tgtNode = nodes.find((n) => n.id === edge.target);
      const bmcNet = [srcNode, tgtNode].find((n) => n?.type === "networkNode" && (n.data as Record<string, any>).networkType === "bmc");
      const vmNode = [srcNode, tgtNode].find((n) => n?.type === "vmNode");
      if (bmcNet && vmNode) {
        const nics = ((vmNode.data as Record<string, any>).nics || []).filter(
          (nic: Record<string, string>) => !nic.name.startsWith("bmc")
        );
        get().updateNodeData(vmNode.id, { nics });
      }
    }
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
          }
          const deployed = get().deployedVmIds;
          const nodes = (t.nodes || []).map((n: Record<string, unknown>) => {
            if (n.type === "vmNode" && n.id) {
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
      const nics = (source.data as Record<string, any>).nics as Array<{id: string; name: string; mac: string; model: string}> || [];
      newData = {
        ...newData,
        nics: nics.map((nic, i) => ({ ...nic, id: generateNicId(), mac: generateMac() })),
        diskControllers: ((source.data as Record<string, any>).diskControllers as Array<{id: string; name: string; bus: string}> || [])
          .map((dc) => ({ ...dc, id: generateDiskControllerId() })),
      };
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

    set({
      nodes: [...get().nodes, ...newNodes],
      edges: [...get().edges, ...newEdges],
      selectedNodeId: newId,
    });
  },

  autoLayout: () => {
    const nodes = get().nodes;
    const edges = get().edges;
    if (nodes.length === 0) return;

    const updated = new Map<string, { x: number; y: number }>();

    // Classify nodes
    const networks = nodes.filter((n) => n.type === "networkNode" && (n.data as Record<string, any>).subtype === "network");
    const routers = nodes.filter((n) => n.type === "networkNode" && (n.data as Record<string, any>).subtype === "router");
    const gateways = nodes.filter((n) => n.type === "networkNode" && (n.data as Record<string, any>).subtype === "gateway");
    const vmNodes = nodes.filter((n) => n.type === "vmNode");
    const storageNodes = nodes.filter((n) => n.type === "storageNode");

    // Build connection maps
    const vmToNetworks = new Map<string, string[]>();
    const networkToVms = new Map<string, string[]>();
    const vmToStorage = new Map<string, string[]>();
    const storageToVm = new Map<string, string>();

    for (const e of edges) {
      const src = nodes.find((n) => n.id === e.source);
      const tgt = nodes.find((n) => n.id === e.target);
      if (!src || !tgt) continue;

      if (src.type === "vmNode" && tgt.type === "networkNode") {
        vmToNetworks.set(src.id, [...(vmToNetworks.get(src.id) || []), tgt.id]);
        networkToVms.set(tgt.id, [...(networkToVms.get(tgt.id) || []), src.id]);
      }
      if (tgt.type === "vmNode" && src.type === "networkNode") {
        vmToNetworks.set(tgt.id, [...(vmToNetworks.get(tgt.id) || []), src.id]);
        networkToVms.set(src.id, [...(networkToVms.get(src.id) || []), tgt.id]);
      }
      if (src.type === "vmNode" && tgt.type === "storageNode") {
        vmToStorage.set(src.id, [...(vmToStorage.get(src.id) || []), tgt.id]);
        storageToVm.set(tgt.id, src.id);
      }
      if (tgt.type === "vmNode" && src.type === "storageNode") {
        vmToStorage.set(tgt.id, [...(vmToStorage.get(tgt.id) || []), src.id]);
        storageToVm.set(src.id, tgt.id);
      }
    }

    // Sizing
    const netW = 240;
    const netH = 70;
    const vmW = 200;
    const vmH = 230;
    const diskW = 170;
    const diskH = 90;
    const gapX = 40;
    const gapY = 80;
    const diskGap = 30;

    // Column width for VMs: disk + gap + VM + gap
    const colW = diskW + diskGap + vmW + gapX;

    // Determine which networks connect via top vs bottom handles on VMs
    const topNetIds = new Set<string>();
    const bottomNetIds = new Set<string>();
    for (const e of edges) {
      const src = nodes.find((n) => n.id === e.source);
      const tgt = nodes.find((n) => n.id === e.target);
      if (!src || !tgt) continue;
      const sH = (e.sourceHandle || "").toLowerCase();
      const tH = (e.targetHandle || "").toLowerCase();
      if (src.type === "vmNode" && tgt.type === "networkNode") {
        if (sH.includes("top")) topNetIds.add(tgt.id);
        else if (sH.includes("bottom")) bottomNetIds.add(tgt.id);
        else topNetIds.add(tgt.id);
      }
      if (tgt.type === "vmNode" && src.type === "networkNode") {
        if (tH.includes("top")) topNetIds.add(src.id);
        else if (tH.includes("bottom")) bottomNetIds.add(src.id);
        else topNetIds.add(src.id);
      }
    }
    // Unconnected networks go to top
    for (const n of networks) {
      if (!topNetIds.has(n.id) && !bottomNetIds.has(n.id)) topNetIds.add(n.id);
    }

    const topNets = networks.filter((n) => topNetIds.has(n.id));
    const bottomNets = networks.filter((n) => bottomNetIds.has(n.id));

    // --- Layout rows ---
    let currentY = 40;

    // Row 0: Gateways (top, spaced wide)
    if (gateways.length > 0) {
      const gwSpacing = Math.max(netW + gapX, colW);
      gateways.forEach((n, i) => {
        updated.set(n.id, { x: 40 + i * gwSpacing, y: currentY });
      });
      currentY += netH + gapY;
    }

    // Row 1: Top networks, with routers placed to the right of their connected networks
    const routerToNets = new Map<string, string[]>();
    for (const e of edges) {
      const src = nodes.find((n) => n.id === e.source);
      const tgt = nodes.find((n) => n.id === e.target);
      if (!src || !tgt) continue;
      const srcSub = (src.data as Record<string, any>).subtype as string;
      const tgtSub = (tgt.data as Record<string, any>).subtype as string;
      if (src.type === "networkNode" && tgt.type === "networkNode") {
        if (srcSub === "router" || srcSub === "gateway") {
          routerToNets.set(src.id, [...(routerToNets.get(src.id) || []), tgt.id]);
        }
        if (tgtSub === "router" || tgtSub === "gateway") {
          routerToNets.set(tgt.id, [...(routerToNets.get(tgt.id) || []), src.id]);
        }
      }
    }

    const placedInfra = new Set<string>();
    if (topNets.length > 0 || routers.length > 0) {
      let netX = 40;
      for (const net of topNets) {
        updated.set(net.id, { x: netX, y: currentY });
        placedInfra.add(net.id);
        netX += netW + gapX;

        // Place routers connected to this network immediately to its right
        for (const r of routers) {
          if (placedInfra.has(r.id)) continue;
          const connNets = routerToNets.get(r.id) || [];
          if (connNets.includes(net.id)) {
            updated.set(r.id, { x: netX, y: currentY });
            placedInfra.add(r.id);
            netX += netW + gapX;
          }
        }
      }
      // Place unconnected routers at the end
      for (const r of routers) {
        if (placedInfra.has(r.id)) continue;
        updated.set(r.id, { x: netX, y: currentY });
        netX += netW + gapX;
      }
      currentY += netH + gapY;
    }

    // VM row: place VMs left to right, accounting for disk space only when needed
    const vmRowY = currentY;
    const placedVms = new Set<string>();
    let cursorX = 40;
    let maxVmBottom = vmRowY;
    let vmCount = 0;

    for (const vm of vmNodes) {
      if (placedVms.has(vm.id)) continue;

      const disks = vmToStorage.get(vm.id) || [];
      const hasDisk = disks.length > 0;

      // Position disks to the left of the VM
      if (hasDisk) {
        disks.forEach((diskId, di) => {
          updated.set(diskId, {
            x: cursorX,
            y: vmRowY + 20 + di * (diskH + 10),
          });
        });
        cursorX += diskW + diskGap;
      }

      updated.set(vm.id, { x: cursorX, y: vmRowY });
      const vmBottom = vmRowY + vmH;
      if (vmBottom > maxVmBottom) maxVmBottom = vmBottom;

      cursorX += vmW + gapX;
      placedVms.add(vm.id);
      vmCount++;
    }

    currentY = maxVmBottom + gapY;

    // Bottom networks row — centered under the VM area
    if (bottomNets.length > 0) {
      const vmAreaWidth = cursorX - 40;
      const netTotalWidth = bottomNets.length * (netW + gapX) - gapX;
      const netStartX = 40 + (vmAreaWidth - netTotalWidth) / 2;
      bottomNets.forEach((n, i) => {
        updated.set(n.id, { x: Math.max(40, netStartX + i * (netW + gapX)), y: currentY });
      });
      currentY += netH + gapY;
    }

    // Unattached storage
    const unattached = storageNodes.filter((n) => !storageToVm.has(n.id));
    if (unattached.length > 0) {
      unattached.forEach((n, i) => {
        updated.set(n.id, { x: 40 + i * (diskW + gapX), y: currentY });
      });
    }

    set({
      nodes: nodes.map((n) => {
        const pos = updated.get(n.id);
        return pos ? { ...n, position: pos } : n;
      }),
    });
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
useCanvasStore.subscribe((state) => {
  if (!state.currentProjectId) return;
  if (state.projectState === "deploying" || state.projectState === "starting" || state.projectState === "stopping") return;
  if (_loadingProject) return;
  if (state.nodes.length === 0) return;
  if (_saveTimer) clearTimeout(_saveTimer);
  _saveTimer = setTimeout(() => {
    if (_loadingProject) return;
    const s = useCanvasStore.getState();
    if (s.nodes.length === 0) return;
    _lastSavedNodeCount = s.nodes.length;
    _saveTopologyToApi(s.currentProjectId!, s);
  }, 1000);
});
