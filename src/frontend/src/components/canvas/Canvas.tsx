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
  type OnSelectionChangeParams,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import VMNode from "./nodes/VMNode";
import NetworkNode from "./nodes/NetworkNode";
import StorageNode from "./nodes/StorageNode";
import CanvasToolbar from "./CanvasToolbar";
import NodeContextMenu from "./NodeContextMenu";
import EdgeContextMenu from "./EdgeContextMenu";
import { useCanvasStore, generateNodeId, generateNicId, generateDiskControllerId, generateMac } from "@/stores/canvasStore";

const nodeTypes = {
  vmNode: VMNode,
  networkNode: NetworkNode,
  storageNode: StorageNode,
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
  onSavePattern?: () => void;
  onSnapshotVM?: (nodeId: string, nodeName: string, isRunning: boolean) => void;
}

export default function Canvas({ onSavePattern, onSnapshotVM }: CanvasProps) {
  const reactFlowWrapper = useRef<HTMLDivElement>(null);
  const { screenToFlowPosition } = useReactFlow();
  const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null);
  const [edgeContextMenu, setEdgeContextMenu] = useState<EdgeContextMenuState | null>(null);
  const [selectedNodes, setSelectedNodes] = useState<Node[]>([]);

  const allNodes = useCanvasStore((s) => s.nodes);
  const allEdges = useCanvasStore((s) => s.edges);
  const hiddenNodeIds = useCanvasStore((s) => s.hiddenNodeIds);
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
  const visibleEdges = useMemo(
    () => allEdges.filter((e) => !hiddenNodeIds.includes(e.source) && !hiddenNodeIds.includes(e.target)),
    [allEdges, hiddenNodeIds],
  );
  const onNodesChange = useCanvasStore((s) => s.onNodesChange);
  const onEdgesChange = useCanvasStore((s) => s.onEdgesChange);
  const onConnect = useCanvasStore((s) => s.onConnect);
  const setSelectedNode = useCanvasStore((s) => s.setSelectedNode);
  const addNode = useCanvasStore((s) => s.addNode);
  const deleteNode = useCanvasStore((s) => s.deleteNode);
  const duplicateNode = useCanvasStore((s) => s.duplicateNode);
  const updateNodeData = useCanvasStore((s) => s.updateNodeData);
  const hideNode = useCanvasStore((s) => s.hideNode);
  const unhideNode = useCanvasStore((s) => s.unhideNode);
  const unhideAll = useCanvasStore((s) => s.unhideAll);
  const showMinimap = useCanvasStore((s) => s.showMinimap);
  const panMode = useCanvasStore((s) => s.panMode);

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
      setContextMenu({ nodeId: node.id, x: event.clientX, y: event.clientY });
      setSelectedNode(node.id);
    },
    [setSelectedNode],
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

      const id = generateNodeId();
      const allNames = useCanvasStore.getState().nodes.map(
        (n) => (n.data as Record<string, unknown>).name as string
      ).filter(Boolean);

      const nextName = (prefix: string) => {
        let num = 0;
        while (allNames.includes(`${prefix}-${String(num).padStart(2, "0")}`)) num++;
        return `${prefix}-${String(num).padStart(2, "0")}`;
      };

      let newNode: Node;

      if (
        item.type === "vm-linux" ||
        item.type === "vm-windows" ||
        item.type.startsWith("template-")
      ) {
        const defaults = item.defaults || {};
        const name = nextName("vm");
        newNode = {
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
      } else if (
        item.type === "network" ||
        item.type === "router" ||
        item.type === "gateway" ||
        item.type === "dhcp" ||
        item.type === "dns"
      ) {
        // Limit to one gateway per project
        if (item.type === "gateway") {
          const hasGateway = useCanvasStore.getState().nodes.some(
            (n) => n.type === "networkNode" && (n.data as Record<string, unknown>).subtype === "gateway"
          );
          if (hasGateway) return;
        }

        const prefix = item.type === "network" ? "network" : item.type;
        const name = nextName(prefix);

        const existingCidrs = useCanvasStore.getState().nodes
          .filter((n) => n.type === "networkNode")
          .map((n) => (n.data as Record<string, unknown>).cidr as string)
          .filter((c) => c && c.includes("/"));

        let newCidr = "10.0.0.0/24";
        if (existingCidrs.length > 0) {
          for (let i = existingCidrs.length - 1; i >= 0; i--) {
            const match = existingCidrs[i].match(/^(\d+\.\d+\.)(\d+)(\.0\/\d+)$/);
            if (match) {
              let octet3 = parseInt(match[2], 10) + 1;
              while (existingCidrs.includes(`${match[1]}${octet3}${match[3]}`)) octet3++;
              newCidr = `${match[1]}${octet3}${match[3]}`;
              break;
            }
          }
        }

        newNode = {
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
          },
        };
      } else if (item.type === "disk" || item.type === "iso") {
        const prefix = item.type === "iso" ? "boot" : "disk";
        const name = nextName(prefix);
        newNode = {
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
      } else if (item.type === "snapshot") {
        const snapshotId = (item.defaults as Record<string, unknown>)?.snapshotId as string;
        if (!snapshotId) return;
        const projectId = useCanvasStore.getState().currentProjectId;
        if (!projectId) return;
        fetch(`/api/v1/projects/${projectId}/import-vm`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ snapshot_id: snapshotId, position: { x: position.x, y: position.y } }),
        })
          .then((r) => r.ok ? r.json() : null)
          .then((data) => {
            if (data?.topology) {
              useCanvasStore.getState().loadProject(projectId);
            }
          })
          .catch(() => {});
        return; // Don't call addNode — the server handles topology updates
      } else {
        return;
      }

      addNode(newNode);
    },
    [addNode],
  );

  const stableNodeTypes = useMemo(() => nodeTypes, []);

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const isValidConnection = useCallback(
    (connection: any) => {
      if (!connection.source || !connection.target) return false;
      const sourceNode = allNodes.find((n) => n.id === connection.source);
      const targetNode = allNodes.find((n) => n.id === connection.target);
      if (!sourceNode || !targetNode) return false;

      const sHandle = connection.sourceHandle || "";
      const tHandle = connection.targetHandle || "";

      const isNicHandle = (h: string) => h.startsWith("nic-") || h === "top" || h === "bottom";
      const isDiskControllerHandle = (h: string) => h.startsWith("dp-") || h === "left" || h === "right";
      const isRouterHandle = (h: string) => h === "left" || h === "right";
      const isVmNetHandle = (h: string) => h.startsWith("nic-") || h === "top" || h === "bottom";

      const sSub = (sourceNode.data as Record<string, unknown>).subtype as string | undefined;
      const tSub = (targetNode.data as Record<string, unknown>).subtype as string | undefined;
      const sIsRouter = sourceNode.type === "networkNode" && sSub === "router";
      const tIsRouter = targetNode.type === "networkNode" && tSub === "router";
      const sIsGateway = sourceNode.type === "networkNode" && sSub === "gateway";
      const tIsGateway = targetNode.type === "networkNode" && tSub === "gateway";
      const sIsNetwork = sourceNode.type === "networkNode" && !sIsRouter && !sIsGateway;
      const tIsNetwork = targetNode.type === "networkNode" && !tIsRouter && !tIsGateway;

      // VM handles: NIC (top/bottom) for networks, disk controller (left/right) for storage
      if (sourceNode.type === "vmNode") {
        if (targetNode.type === "storageNode" && !isDiskControllerHandle(sHandle)) return false;
        if (tIsNetwork && !isNicHandle(sHandle)) return false;
      }
      if (targetNode.type === "vmNode") {
        if (sourceNode.type === "storageNode" && !isDiskControllerHandle(tHandle)) return false;
        if (sIsNetwork && !isVmNetHandle(tHandle)) return false;
      }

      // Router/Gateway must connect to network's left/right handles
      if ((sIsRouter || sIsGateway) && tIsNetwork && !isRouterHandle(tHandle)) return false;
      if ((tIsRouter || tIsGateway) && sIsNetwork && !isRouterHandle(sHandle)) return false;

      // Network top/bottom handles only for VMs
      if (sIsNetwork && targetNode.type === "vmNode" && !isVmNetHandle(sHandle)) return false;
      if (tIsNetwork && sourceNode.type === "vmNode" && !isVmNetHandle(tHandle)) return false;

      return true;
    },
    [allNodes],
  );

  return (
    <div className="canvas-wrapper" ref={reactFlowWrapper}>
      <ReactFlow
        nodes={visibleNodes}
        edges={visibleEdges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        isValidConnection={isValidConnection}
        onNodeClick={onNodeClick}
        onNodeDoubleClick={onNodeDoubleClick}
        onNodeContextMenu={onNodeContextMenu}
        onEdgeContextMenu={onEdgeContextMenu}
        onPaneClick={onPaneClick}
        onDragOver={onDragOver}
        onDrop={onDrop}
        nodeTypes={stableNodeTypes}
        onSelectionChange={onSelectionChange}
        selectionMode={SelectionMode.Partial}
        panOnDrag={panMode}
        selectionOnDrag={!panMode}
        multiSelectionKeyCode="Shift"
        connectionMode={ConnectionMode.Loose}
        defaultEdgeOptions={{ type: "smoothstep" }}
        connectionLineType={ConnectionLineType.SmoothStep}
        fitView
        deleteKeyCode={["Backspace", "Delete"]}
        proOptions={{ hideAttribution: true }}
      >
        <Background
          variant={BackgroundVariant.Dots}
          gap={24}
          size={1}
          color="rgba(255,255,255,0.06)"
        />
        <CanvasToolbar onSavePattern={onSavePattern} />
        {showMinimap && (
          <MiniMap
            nodeStrokeWidth={3}
            pannable
            zoomable
            style={{
              background: "var(--troshka-surface)",
              borderRadius: 8,
            }}
            maskColor="rgba(0,0,0,0.3)"
          />
        )}
      </ReactFlow>
      {selectedNodes.length > 1 && (
        <div className="selection-toolbar">
          <span className="selection-count">{selectedNodes.length} selected</span>
          <button
            title="Duplicate All"
            onClick={() => selectedNodes.forEach((n) => duplicateNode(n.id))}
          >
            ⧉ Duplicate
          </button>
          {selectedNodes.some((n) => n.type === "vmNode") && (
            <>
              <button
                title="Start VMs"
                onClick={() =>
                  selectedNodes
                    .filter((n) => n.type === "vmNode")
                    .forEach((n) => updateNodeData(n.id, { status: "running" }))
                }
              >
                ▶ Start VMs
              </button>
              <button
                title="Stop VMs"
                onClick={() =>
                  selectedNodes
                    .filter((n) => n.type === "vmNode")
                    .forEach((n) => updateNodeData(n.id, { status: "stopped" }))
                }
              >
                ■ Stop VMs
              </button>
            </>
          )}
          <button
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
      )}
      {hiddenNodeIds.length > 0 && (
        <div className="hidden-items-bar">
          <span className="hidden-items-count">👁 {hiddenNodeIds.length} hidden</span>
          {hiddenNodeIds.map((nid) => {
            const n = nodes.find((node) => node.id === nid);
            if (!n) return null;
            const name = (n.data as Record<string, unknown>).name as string || nid;
            return (
              <button key={nid} onClick={() => unhideNode(nid)} title={`Show ${name}`}>
                {name}
              </button>
            );
          })}
          <button className="show-all" onClick={unhideAll}>Show All</button>
        </div>
      )}
      {contextMenu && (
        <NodeContextMenu
          nodeId={contextMenu.nodeId}
          x={contextMenu.x}
          y={contextMenu.y}
          onClose={() => setContextMenu(null)}
          onSnapshotVM={onSnapshotVM}
        />
      )}
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
