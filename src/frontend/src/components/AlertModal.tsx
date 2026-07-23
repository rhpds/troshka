"use client";

interface AlertModalProps {
  message: string | null;
  title?: string;
  onClose: () => void;
}

export default function AlertModal({ message, title, onClose }: AlertModalProps) {
  if (!message) return null;
  return (
    <div className="start-order-overlay" onClick={onClose}>
      <div className="start-order-modal" style={{ maxWidth: 480 }} onClick={(e) => e.stopPropagation()}>
        <div className="start-order-header">
          <span>{title || "Notice"}</span>
          <button onClick={onClose}>&#x2715;</button>
        </div>
        <div className="start-order-body" style={{ padding: 16, whiteSpace: "pre-wrap" }}>
          {message}
        </div>
        <div className="start-order-footer">
          <button className="start-order-btn save" onClick={onClose}>OK</button>
        </div>
      </div>
    </div>
  );
}
