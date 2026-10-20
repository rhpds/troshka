"use client";

import { useEffect, useRef, type CSSProperties, type ReactNode } from "react";

type Props = Readonly<{
  children: ReactNode;
  onClose: () => void;
  className?: string;
  style?: CSSProperties;
  label: string;
}>;

/** Native focus trapping and dismissal for the existing canvas modal layouts. */
export default function ModalBackdrop({ children, onClose, className = "", style, label }: Props) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
    return () => {
      if (typeof dialog.close === "function") dialog.close();
    };
  }, []);

  return (
    <dialog
      ref={ref}
      aria-label={label}
      className={`modal-backdrop ${className}`}
      style={{
        border: 0, padding: 0, margin: 0, width: "100%", height: "100%",
        maxWidth: "none", maxHeight: "none", color: "inherit", ...style,
      }}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <div className="modal-backdrop-content">{children}</div>
      <button
        type="button"
        className="modal-backdrop-dismiss"
        tabIndex={-1}
        aria-label="Dismiss dialog"
        onClick={onClose}
      />
    </dialog>
  );
}
