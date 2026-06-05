"use client";

import React, { useEffect, useRef } from "react";
import { useCanvasStore } from "@/stores/canvasStore";

interface NodeContextMenuProps {
  nodeId: string;
  x: number;
  y: number;
  onClose: () => void;
}

export default function NodeContextMenu({ nodeId, x, y, onClose }: NodeContextMenuProps) {
  const duplicateNode = useCanvasStore((s) => s.duplicateNode);
  const deleteNode = useCanvasStore((s) => s.deleteNode);
  const hideNode = useCanvasStore((s) => s.hideNode);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as HTMLElement)) onClose();
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [onClose]);

  return (
    <div
      ref={ref}
      className="node-context-menu"
      style={{ position: "fixed", left: x, top: y, zIndex: 9999 }}
    >
      <button onClick={() => { duplicateNode(nodeId); onClose(); }}>
        ⧉ Duplicate
      </button>
      <button onClick={() => { hideNode(nodeId); onClose(); }}>
        👁 Hide
      </button>
      <button onClick={() => { deleteNode(nodeId); onClose(); }} className="danger">
        ✕ Delete
      </button>
    </div>
  );
}
