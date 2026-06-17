"use client";

import React from "react";
import { useReactFlow, useViewport } from "@xyflow/react";
import { useCanvasStore } from "@/stores/canvasStore";

export default function CanvasToolbar() {
  const { zoomIn, zoomOut, fitView } = useReactFlow();
  const { zoom } = useViewport();
  const showMinimap = useCanvasStore((s) => s.showMinimap);
  const toggleMinimap = useCanvasStore((s) => s.toggleMinimap);
  const autoLayout = useCanvasStore((s) => s.autoLayout);
  const panMode = useCanvasStore((s) => s.panMode);
  const canUndo = useCanvasStore((s) => s.canUndo);
  const canRedo = useCanvasStore((s) => s.canRedo);
  const undo = useCanvasStore((s) => s.undo);
  const redo = useCanvasStore((s) => s.redo);

  const zoomPercent = Math.round(zoom * 100);

  return (
    <div className="canvas-toolbar">
      <button
        className="tool-btn"
        onClick={undo}
        disabled={!canUndo}
        title="Undo (Ctrl+Z)"
        style={{ opacity: canUndo ? 1 : 0.3 }}
      >
        ↶
      </button>
      <button
        className="tool-btn"
        onClick={redo}
        disabled={!canRedo}
        title="Redo (Ctrl+Shift+Z)"
        style={{ opacity: canRedo ? 1 : 0.3 }}
      >
        ↷
      </button>

      <span className="tool-sep" />

      <button
        className={`tool-btn ${panMode ? "active" : ""}`}
        onClick={() => useCanvasStore.setState({ panMode: true })}
        title="Pan (drag to move canvas)"
      >
        ✋
      </button>
      <button
        className={`tool-btn ${!panMode ? "active" : ""}`}
        onClick={() => useCanvasStore.setState({ panMode: false })}
        title="Select (drag to select area)"
      >
        ⬚
      </button>

      <span className="tool-sep" />

      <button
        className="tool-btn"
        onClick={() => zoomOut()}
        title="Zoom Out"
      >
        −
      </button>
      <span className="zoom-display">{zoomPercent}%</span>
      <button
        className="tool-btn"
        onClick={() => zoomIn()}
        title="Zoom In"
      >
        +
      </button>

      <span className="tool-sep" />

      <button
        className="tool-btn"
        onClick={() => fitView({ padding: 0.2 })}
        title="Fit View"
      >
        ⊞
      </button>

      <span className="tool-sep" />

      <button
        className="tool-btn"
        onClick={async () => { await autoLayout(); setTimeout(() => fitView({ padding: 0.2 }), 50); }}
        title="Auto Layout"
      >
        ⊞⃗
      </button>

      <span className="tool-sep" />

      <button
        className={`tool-btn ${showMinimap ? "active" : ""}`}
        onClick={toggleMinimap}
        title="Toggle Minimap"
      >
        ⊟
      </button>

    </div>
  );
}
