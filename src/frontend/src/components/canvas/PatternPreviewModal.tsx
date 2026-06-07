"use client";

import React, { useState, useEffect, useMemo, useCallback } from "react";
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  MiniMap,
  ReactFlowProvider,
  useInternalNode,
  useViewport,
  applyNodeChanges,
  type Node,
  type Edge,
  type NodeChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import VMNode from "./nodes/VMNode";
import NetworkNode from "./nodes/NetworkNode";
import StorageNode from "./nodes/StorageNode";
import ReadOnlyPropertiesPanel from "./ReadOnlyPropertiesPanel";

const nodeTypes = {
  vmNode: VMNode,
  networkNode: NetworkNode,
  storageNode: StorageNode,
};

interface PatternPreviewModalProps {
  patternId: string;
  patternName: string;
  onClose: () => void;
}

function getAnchorPoint(
  node: { internals: { positionAbsolute: { x: number; y: number } }; measured?: { width?: number; height?: number } },
  handle: string | undefined,
  role: "source" | "target",
  otherNode: { internals: { positionAbsolute: { x: number; y: number } } },
) {
  const w = node.measured?.width || 200;
  const h = node.measured?.height || 100;
  const x = node.internals.positionAbsolute.x;
  const y = node.internals.positionAbsolute.y;

  if (handle?.includes("-top")) return { px: x + w / 2, py: y, dir: "top" as const };
  if (handle?.includes("-bottom")) return { px: x + w / 2, py: y + h, dir: "bottom" as const };
  if (handle?.includes("-left") || handle === "left") return { px: x, py: y + h / 2, dir: "left" as const };
  if (handle?.includes("-right") || handle === "right") return { px: x + w, py: y + h / 2, dir: "right" as const };
  if (handle === "top") return { px: x + w / 2, py: y, dir: "top" as const };
  if (handle === "bottom") return { px: x + w / 2, py: y + h, dir: "bottom" as const };

  const ox = otherNode.internals.positionAbsolute.x;
  const oy = otherNode.internals.positionAbsolute.y;
  const dx = ox - x;
  const dy = oy - y;
  if (Math.abs(dx) > Math.abs(dy)) {
    return dx > 0
      ? { px: x + w, py: y + h / 2, dir: "right" as const }
      : { px: x, py: y + h / 2, dir: "left" as const };
  }
  return dy > 0
    ? { px: x + w / 2, py: y + h, dir: "bottom" as const }
    : { px: x + w / 2, py: y, dir: "top" as const };
}

function EdgeLine({ sourceId, targetId, sourceHandle, targetHandle }: {
  sourceId: string; targetId: string; sourceHandle?: string; targetHandle?: string;
}) {
  const sourceNode = useInternalNode(sourceId);
  const targetNode = useInternalNode(targetId);

  if (!sourceNode || !targetNode) return null;

  const src = getAnchorPoint(sourceNode, sourceHandle, "source", targetNode);
  const tgt = getAnchorPoint(targetNode, targetHandle, "target", sourceNode);

  const isNic = sourceHandle?.includes("nic-") || targetHandle?.includes("nic-");
  const stroke = isNic ? "rgba(56,189,248,0.6)" : "rgba(251,191,36,0.6)";

  const offset = 60;
  let c1x = src.px, c1y = src.py, c2x = tgt.px, c2y = tgt.py;
  if (src.dir === "right") c1x += offset;
  if (src.dir === "left") c1x -= offset;
  if (src.dir === "top") c1y -= offset;
  if (src.dir === "bottom") c1y += offset;
  if (tgt.dir === "right") c2x += offset;
  if (tgt.dir === "left") c2x -= offset;
  if (tgt.dir === "top") c2y -= offset;
  if (tgt.dir === "bottom") c2y += offset;

  return (
    <path
      d={`M ${src.px} ${src.py} C ${c1x} ${c1y}, ${c2x} ${c2y}, ${tgt.px} ${tgt.py}`}
      fill="none"
      stroke={stroke}
      strokeWidth={2}
      strokeDasharray="6 4"
    />
  );
}

function EdgeOverlay({ edges }: { edges: Edge[] }) {
  const { x, y, zoom } = useViewport();
  return (
    <svg
      className="react-flow__edge-overlay"
      style={{ position: "absolute", top: 0, left: 0, width: "100%", height: "100%", pointerEvents: "none", overflow: "visible" }}
    >
      <g transform={`translate(${x}, ${y}) scale(${zoom})`}>
        {edges.map((edge, i) => (
          <EdgeLine
            key={edge.id || `e-${i}`}
            sourceId={edge.source}
            targetId={edge.target}
            sourceHandle={edge.sourceHandle ?? undefined}
            targetHandle={edge.targetHandle ?? undefined}
          />
        ))}
      </g>
    </svg>
  );
}

function PreviewCanvas({ initialNodes, initialEdges }: { initialNodes: Node[]; initialEdges: Edge[] }) {
  const stableNodeTypes = useMemo(() => nodeTypes, []);
  const [nodes, setNodes] = useState<Node[]>(initialNodes);
  const [showEdges, setShowEdges] = useState(false);
  const [selectedNode, setSelectedNode] = useState<Node | null>(null);

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    setNodes((nds) => applyNodeChanges(changes, nds));
  }, []);

  const onNodeClick = useCallback((_: React.MouseEvent, node: Node) => {
    setSelectedNode(node);
  }, []);

  const onPaneClick = useCallback(() => {
    setSelectedNode(null);
  }, []);

  return (
    <div style={{ position: "relative", width: "100%", height: "100%" }}>
      <ReactFlow
        nodes={nodes}
        edges={[]}
        onNodesChange={onNodesChange}
        onNodeClick={onNodeClick}
        onPaneClick={onPaneClick}
        nodeTypes={stableNodeTypes}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={true}
        panOnDrag={true}
        zoomOnScroll={true}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        proOptions={{ hideAttribution: true }}
        onInit={() => {
          setTimeout(() => setShowEdges(true), 300);
        }}
      >
        {showEdges && <EdgeOverlay edges={initialEdges} />}
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} />
        <MiniMap pannable={false} zoomable={false} style={{ height: 80, width: 120 }} />
      </ReactFlow>
      {selectedNode && (
        <ReadOnlyPropertiesPanel node={selectedNode} onClose={() => setSelectedNode(null)} />
      )}
    </div>
  );
}

export default function PatternPreviewModal({ patternId, patternName, onClose }: PatternPreviewModalProps) {
  const [topology, setTopology] = useState<{ nodes: Node[]; edges: Edge[] } | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`/api/v1/patterns/${patternId}`)
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (data?.topology) {
          setTopology({
            nodes: data.topology.nodes || [],
            edges: data.topology.edges || [],
          });
        }
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [patternId]);

  return (
    <div style={{
      position: "fixed", inset: 0, zIndex: 10000,
      display: "flex", alignItems: "center", justifyContent: "center",
      background: "rgba(0,0,0,0.6)",
    }} onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div style={{
        background: "var(--pf-t--global--background--color--primary--default)",
        borderRadius: 12, padding: 0, width: "80vw", height: "70vh", maxWidth: 1200,
        boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
        border: "1px solid var(--pf-t--global--border--color--default)",
        display: "flex", flexDirection: "column", overflow: "hidden",
      }}>
        <div style={{
          padding: "12px 20px",
          borderBottom: "1px solid var(--pf-t--global--border--color--default)",
          display: "flex", justifyContent: "space-between", alignItems: "center",
        }}>
          <h2 style={{ margin: 0, fontSize: 16 }}>{patternName}</h2>
          <button
            onClick={onClose}
            style={{
              background: "none", border: "none", color: "var(--pf-t--global--text--color--regular)",
              fontSize: 18, cursor: "pointer", padding: "4px 8px",
            }}
          >
            ✕
          </button>
        </div>
        <div style={{ flex: 1 }}>
          {loading ? (
            <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", opacity: 0.5 }}>
              Loading topology...
            </div>
          ) : topology ? (
            <ReactFlowProvider>
              <PreviewCanvas initialNodes={topology.nodes} initialEdges={topology.edges} />
            </ReactFlowProvider>
          ) : (
            <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", opacity: 0.5 }}>
              No topology data
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
