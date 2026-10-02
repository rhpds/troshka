"use client";

import React, { useState, useEffect, useMemo, useCallback } from "react";
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  MiniMap,
  ReactFlowProvider,
  ConnectionMode,
  applyNodeChanges,
  type Node,
  type Edge,
  type NodeChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import VMNode from "./nodes/VMNode";
import NetworkNode from "./nodes/NetworkNode";
import StorageNode from "./nodes/StorageNode";
import { ContainerNode } from "./nodes/ContainerNode";
import ClusterNode from "./nodes/ClusterNode";
import CephClusterNode from "./nodes/CephClusterNode";
import ClusterAnchorEdge from "./edges/ClusterAnchorEdge";
import ReadOnlyPropertiesPanel from "./ReadOnlyPropertiesPanel";
import { CanvasDisplayProvider } from "@/components/canvas/CanvasDisplayContext";
import { useIsDarkTheme } from "@/hooks/useIsDarkTheme";
import {
  CLUSTER_ANCHOR_EDGE_TYPE,
  isClusterAnchorEdge,
} from "@/lib/clusterAnchorEdge";

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

interface PatternPreviewModalProps {
  patternId: string;
  patternName: string;
  onClose: () => void;
}

/** Match project-canvas edge routing (smoothstep) and preserve stored stroke styles. */
function normalizePreviewEdges(edges: Edge[]): Edge[] {
  return edges.map((edge) => {
    if (isClusterAnchorEdge(edge)) {
      return { ...edge, type: CLUSTER_ANCHOR_EDGE_TYPE };
    }
    return {
      ...edge,
      type: "smoothstep",
      style: {
        stroke: "rgba(56,189,248,0.5)",
        strokeWidth: 2,
        strokeDasharray: "6 4",
        ...edge.style,
      },
    };
  });
}

function PreviewCanvas({ initialNodes, initialEdges }: { initialNodes: Node[]; initialEdges: Edge[] }) {
  const stableNodeTypes = useMemo(() => nodeTypes, []);
  const stableEdgeTypes = useMemo(() => edgeTypes, []);
  const isDark = useIsDarkTheme();
  const [nodes, setNodes] = useState<Node[]>(initialNodes);
  const [selectedNode, setSelectedNode] = useState<Node | null>(null);
  const edges = useMemo(() => normalizePreviewEdges(initialEdges), [initialEdges]);

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
    <div className="canvas-wrapper" style={{ position: "relative", width: "100%", height: "100%" }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onNodeClick={onNodeClick}
        onPaneClick={onPaneClick}
        nodeTypes={stableNodeTypes}
        edgeTypes={stableEdgeTypes}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={true}
        panOnDrag={true}
        zoomOnScroll={true}
        connectionMode={ConnectionMode.Loose}
        defaultEdgeOptions={{ type: "smoothstep" }}
        colorMode={isDark ? "dark" : "light"}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        proOptions={{ hideAttribution: true }}
      >
        <Background
          variant={BackgroundVariant.Dots}
          gap={20}
          size={1}
          color="var(--troshka-canvas-dots)"
        />
        <MiniMap
          pannable={false}
          zoomable={false}
          style={{ height: 80, width: 120, background: "var(--troshka-surface)", borderRadius: 8 }}
          maskColor="var(--troshka-minimap-mask)"
        />
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
        <div style={{ flex: 1, minHeight: 0 }}>
          {loading ? (
            <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", opacity: 0.5 }}>
              Loading topology...
            </div>
          ) : topology ? (
            <ReactFlowProvider>
              <CanvasDisplayProvider isPreview>
                <PreviewCanvas initialNodes={topology.nodes} initialEdges={topology.edges} />
              </CanvasDisplayProvider>
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
