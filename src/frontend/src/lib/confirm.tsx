"use client";

import { useEffect, useState } from "react";
import ConfirmModal from "@/components/ConfirmModal";

export interface AppConfirmOptions {
  message: string;
  title?: string;
  confirmLabel?: string;
  variant?: "danger" | "primary";
}

type Resolver = (value: boolean) => void;

let resolveCurrent: Resolver | null = null;
let setModalState: ((state: AppConfirmOptions | null) => void) | null = null;

/** Promise-based confirm dialog. Falls back to window.confirm before ConfirmHost mounts. */
export function appConfirm(options: AppConfirmOptions): Promise<boolean> {
  const show = setModalState;
  if (!show) {
    return Promise.resolve(window.confirm(options.message));
  }
  return new Promise((resolve) => {
    if (resolveCurrent) {
      resolveCurrent(false);
    }
    resolveCurrent = resolve;
    show(options);
  });
}

export function ConfirmHost() {
  const [options, setOptions] = useState<AppConfirmOptions | null>(null);

  useEffect(() => {
    setModalState = setOptions;
    return () => {
      setModalState = null;
      if (resolveCurrent) {
        resolveCurrent(false);
        resolveCurrent = null;
      }
    };
  }, []);

  if (!options) return null;

  const close = (result: boolean) => {
    if (resolveCurrent) {
      resolveCurrent(result);
      resolveCurrent = null;
    }
    setOptions(null);
  };

  return (
    <ConfirmModal
      title={options.title}
      message={options.message}
      confirmLabel={options.confirmLabel}
      variant={options.variant}
      onConfirm={() => close(true)}
      onCancel={() => close(false)}
    />
  );
}
