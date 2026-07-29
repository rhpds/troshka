"use client";

interface ConfirmModalProps {
  message: string;
  title?: string;
  confirmLabel?: string;
  variant?: "danger" | "primary";
  onConfirm: () => void;
  onCancel: () => void;
}

export default function ConfirmModal({ message, title, confirmLabel, variant, onConfirm, onCancel }: ConfirmModalProps) {
  return (
    <div className="start-order-overlay" onClick={onCancel}>
      <div className="start-order-modal" style={{ maxWidth: 480 }} onClick={(e) => e.stopPropagation()}>
        <div className="start-order-header">
          <span>{title || "Confirm"}</span>
          <button onClick={onCancel}>&#x2715;</button>
        </div>
        <div className="start-order-body" style={{ padding: 16, whiteSpace: "pre-wrap" }}>
          {message}
        </div>
        <div className="start-order-footer" style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button className="start-order-btn" onClick={onCancel}>Cancel</button>
          <button className={`start-order-btn ${variant === "danger" ? "delete" : "save"}`} onClick={onConfirm}>{confirmLabel || "Confirm"}</button>
        </div>
      </div>
    </div>
  );
}
