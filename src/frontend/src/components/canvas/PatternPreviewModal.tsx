"use client";

import React, { useState, useEffect, useMemo } from "react";
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  MiniMap,
  ReactFlowProvider,
  type Node,
  type Edge,
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

function PreviewCanvas({ nodes, edges }: { nodes: Node[]; edges: Edge[] }) {
  const stableNodeTypes = useMemo(() => nodeTypes, []);

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={stableNodeTypes}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable={false}
      panOnDrag={true}
      zoomOnScroll={true}
      fitView
      fitViewOptions={{ padding: 0.2 }}
      proOptions={{ hideAttribution: true }}
    >
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
              <PreviewCanvas nodes={topology.nodes} edges={topology.edges} />
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
