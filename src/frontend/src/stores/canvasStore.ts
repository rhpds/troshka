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
  status: "running" | "stopped";
  bootOrder?: number;
  bootMethod?: string;
  cloudInit?: boolean;
  icon: string;
  nics: VMNic[];
  diskControllers: VMDiskController[];
  [key: string]: unknown;
}

export interface NetworkNodeData {
  label: string;
  name: string;
  subtype: "network" | "router" | "gateway" | "dhcp" | "dns";
  cidr: string;
  dhcp: boolean;
  dns: boolean;
  dnsDomain?: string;
  [key: string]: unknown;
}

export interface StorageNodeData {
  label: string;
  name: string;
  size: number;
  format: "qcow2" | "raw" | "iso";
  icon: string;
  [key: string]: unknown;
}

export type CanvasNodeData = VMNodeData | NetworkNodeData | StorageNodeData;

export interface ExternalIp {
  id: string;
  name: string;
  ip: string;
}

export interface StartOrderEntry {
  vmId: string;
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
}

let nodeIdCounter = 1;
export function generateNodeId(): string {
  return `node-${Date.now()}-${nodeIdCounter++}`;
}

export function generateNicId(): string {
  return `nic-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
}

export function generateDiskControllerId(): string {
  return `dp-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
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
            return node ? (node.data as Record<string, unknown>).name as string || node.id : (r as { id: string }).id;
          })
          .join(", ");
        if (!window.confirm(`Delete ${removals.length > 1 ? `${removals.length} items` : names}?`)) {
          set({ nodes: updatedNodes });
          return;
        }
      }
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
    }
    set({ edges: applyEdgeChanges(changes, get().edges) });
  },

  onConnect: (connection) => {
    const sourceNode = get().nodes.find((n) => n.id === connection.source);
    const targetNode = get().nodes.find((n) => n.id === connection.target);
    if (!sourceNode || !targetNode) return;

    const sType = sourceNode.type;
    const tType = targetNode.type;
    const sSub = (sourceNode.data as Record<string, unknown>).subtype as string | undefined;
    const tSub = (targetNode.data as Record<string, unknown>).subtype as string | undefined;

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
      const isIso = storageNode && (storageNode.data as Record<string, unknown>).format === "iso";
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
    } else {
      edgeStyle = {
        stroke: "rgba(34,211,238,0.5)",
        strokeWidth: 2,
        strokeDasharray: "6 4",
      };
      animated = true;
    }

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
  },

  addNode: (node) => {
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
    set({ edges: get().edges.filter((e) => e.id !== edgeId) });
  },

  loadProject: (projectId) => {
    const current = get().currentProjectId;

    _loadingProject = true;
    if (_saveTimer) { clearTimeout(_saveTimer); _saveTimer = null; }

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
          set({
            nodes: t.nodes || [],
            edges: t.edges || [],
            hiddenNodeIds: t.hiddenNodeIds || [],
            startOrder: t.startOrder || [],
            externalIps: t.externalIps || [],
          });
          _lastSavedNodeCount = (t.nodes || []).length;
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

    const allNames = get().nodes.map((n) => (n.data as Record<string, unknown>).name as string).filter(Boolean);
    const baseName = (source.data as Record<string, unknown>).name as string || "node";

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
    let newData = { ...source.data, name: newName, label: newName };
    if (source.type === "vmNode") {
      const nics = (source.data as Record<string, unknown>).nics as Array<{id: string; name: string; mac: string; model: string}> || [];
      newData = {
        ...newData,
        nics: nics.map((nic, i) => ({ ...nic, id: generateNicId(), mac: generateMac() })),
        diskControllers: ((source.data as Record<string, unknown>).diskControllers as Array<{id: string; name: string; bus: string}> || [])
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
      const oldNics = (source.data as Record<string, unknown>).nics as Array<{id: string}> || [];
      const newNics = (newData as Record<string, unknown>).nics as Array<{id: string}> || [];
      const oldDcs = (source.data as Record<string, unknown>).diskControllers as Array<{id: string}> || [];
      const newDcs = (newData as Record<string, unknown>).diskControllers as Array<{id: string}> || [];

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
            const diskName = (otherNode.data as Record<string, unknown>).name as string || "disk";
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
    const networks = nodes.filter((n) => n.type === "networkNode" && (n.data as Record<string, unknown>).subtype === "network");
    const routers = nodes.filter((n) => n.type === "networkNode" && (n.data as Record<string, unknown>).subtype === "router");
    const gateways = nodes.filter((n) => n.type === "networkNode" && (n.data as Record<string, unknown>).subtype === "gateway");
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
      const srcSub = (src.data as Record<string, unknown>).subtype as string;
      const tgtSub = (tgt.data as Record<string, unknown>).subtype as string;
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

// Save topology to API
function _saveTopologyToApi(projectId: string, state: { nodes: Node[]; edges: Edge[]; hiddenNodeIds: string[]; startOrder: StartOrderEntry[]; externalIps: ExternalIp[] }) {
  const topology = {
    nodes: state.nodes,
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
