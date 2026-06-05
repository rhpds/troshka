"use client";

import React, { useEffect, useRef } from "react";
import { useCanvasStore } from "@/stores/canvasStore";

interface EdgeContextMenuProps {
  edgeId: string;
  x: number;
  y: number;
  onClose: () => void;
}

export default function EdgeContextMenu({ edgeId, x, y, onClose }: EdgeContextMenuProps) {
  const deleteEdge = useCanvasStore((s) => s.deleteEdge);
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
      <button onClick={() => { deleteEdge(edgeId); onClose(); }} className="danger">
        ✕ Delete Connection
      </button>
    </div>
  );
}
