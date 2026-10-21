"use client";

import ModalBackdrop from "@/components/ModalBackdrop";

interface AlertModalProps {
  message: string | null;
  title?: string;
  onClose: () => void;
}

export default function AlertModal({ message, title, onClose }: Readonly<AlertModalProps>) {
  if (!message) return null;
  return (
    <ModalBackdrop label="Notice" className="start-order-overlay" onClose={onClose}>
      <div className="start-order-modal" style={{ maxWidth: 480 }}>
        <div className="start-order-header">
          <span>{title || "Notice"}</span>
          <button type="button" onClick={onClose}>&#x2715;</button>
        </div>
        <div className="start-order-body" style={{ padding: 16, whiteSpace: "pre-wrap" }}>
          {message}
        </div>
        <div className="start-order-footer">
          <button type="button" className="start-order-btn save" onClick={onClose}>OK</button>
        </div>
      </div>
    </ModalBackdrop>
  );
}
