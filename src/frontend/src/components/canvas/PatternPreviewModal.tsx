"use client";

import React, { useState, useEffect, useMemo, useCallback } from "react";
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  MiniMap,
  ReactFlowProvider,
  useInternalNode,
  applyNodeChanges,
  type Node,
  type Edge,
  type NodeChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import VMNode from "./nodes/VMNode";
import NetworkNode from "./nodes/NetworkNode";
import StorageNode from "./nodes/StorageNode";

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

function EdgeLine({ sourceId, targetId }: { sourceId: string; targetId: string }) {
  const sourceNode = useInternalNode(sourceId);
  const targetNode = useInternalNode(targetId);

  if (!sourceNode || !targetNode) return null;

  const sw = sourceNode.measured?.width || 200;
  const sh = sourceNode.measured?.height || 80;
  const tw = targetNode.measured?.width || 200;
  const th = targetNode.measured?.height || 80;

  const sx = sourceNode.internals.positionAbsolute.x + sw;
  const sy = sourceNode.internals.positionAbsolute.y + sh / 2;
  const tx = targetNode.internals.positionAbsolute.x;
  const ty = targetNode.internals.positionAbsolute.y + th / 2;

  const mx = (sx + tx) / 2;

  return (
    <path
      d={`M ${sx} ${sy} C ${mx} ${sy}, ${mx} ${ty}, ${tx} ${ty}`}
      fill="none"
      stroke="rgba(251,191,36,0.6)"
      strokeWidth={2}
      strokeDasharray="6 4"
    />
  );
}

function EdgeOverlay({ edges }: { edges: Edge[] }) {
  return (
    <svg
      className="react-flow__edge-overlay"
      style={{ position: "absolute", top: 0, left: 0, width: "100%", height: "100%", pointerEvents: "none" }}
    >
      <g>
        {edges.map((edge, i) => (
          <EdgeLine key={edge.id || `e-${i}`} sourceId={edge.source} targetId={edge.target} />
        ))}
      </g>
    </svg>
  );
}

function PreviewCanvas({ initialNodes, initialEdges }: { initialNodes: Node[]; initialEdges: Edge[] }) {
  const stableNodeTypes = useMemo(() => nodeTypes, []);
  const [nodes, setNodes] = useState<Node[]>(initialNodes);
  const [showEdges, setShowEdges] = useState(false);

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    setNodes((nds) => applyNodeChanges(changes, nds));
  }, []);

  return (
    <ReactFlow
      nodes={nodes}
      edges={[]}
      onNodesChange={onNodesChange}
      nodeTypes={stableNodeTypes}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable={false}
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
