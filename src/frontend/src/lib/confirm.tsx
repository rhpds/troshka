"use client";

import { useEffect, useState } from "react";
import ConfirmModal from "@/components/ConfirmModal";

export interface AppConfirmOptions {
  message: string;
  title?: string;
  confirmLabel?: string;
  variant?: "danger" | "primary";
  /** Renders a checkbox below the message; its checked state is written to
   * `checkboxRef.current` on toggle so callers can read it after the
   * returned promise resolves `true` (the boolean contract stays unchanged). */
  checkboxLabel?: string;
  checkboxRef?: { current: boolean };
}

type Resolver = (value: boolean) => void;

let resolveCurrent: Resolver | null = null;
let setModalState: ((state: AppConfirmOptions | null) => void) | null = null;
// Kept outside React state (unlike `options`) so the checkbox handler can
// mutate `.current` without violating the state-immutability lint rule.
let activeCheckboxRef: { current: boolean } | null = null;

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
    activeCheckboxRef = options.checkboxRef ?? null;
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
    activeCheckboxRef = null;
    setOptions(null);
  };

  const onCheckboxChange = (checked: boolean) => {
    if (activeCheckboxRef) activeCheckboxRef.current = checked;
  };

  return (
    <ConfirmModal
      title={options.title}
      message={options.message}
      confirmLabel={options.confirmLabel}
      variant={options.variant}
      checkboxLabel={options.checkboxLabel}
      onCheckboxChange={options.checkboxLabel ? onCheckboxChange : undefined}
      onConfirm={() => close(true)}
      onCancel={() => close(false)}
    />
  );
}
