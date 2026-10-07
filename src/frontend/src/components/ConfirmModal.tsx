"use client";

interface ConfirmModalProps {
  message: string;
  title?: string;
  confirmLabel?: string;
  variant?: "danger" | "primary";
  checkboxLabel?: string;
  onCheckboxChange?: (checked: boolean) => void;
  onConfirm: () => void;
  onCancel: () => void;
}

export default function ConfirmModal({ message, title, confirmLabel, variant, checkboxLabel, onCheckboxChange, onConfirm, onCancel }: ConfirmModalProps) {
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
        {checkboxLabel && (
          <div style={{ padding: "0 16px 16px" }}>
            <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, cursor: "pointer" }}>
              <input type="checkbox" onChange={(e) => onCheckboxChange?.(e.target.checked)} />
              <span>{checkboxLabel}</span>
            </label>
          </div>
        )}
        <div className="start-order-footer" style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button className="start-order-btn" onClick={onCancel}>Cancel</button>
          <button className={`start-order-btn ${variant === "danger" ? "delete" : "save"}`} onClick={onConfirm}>{confirmLabel || "Confirm"}</button>
        </div>
      </div>
    </div>
  );
}
