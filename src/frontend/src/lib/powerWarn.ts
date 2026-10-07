"use client";

import { appConfirm } from "@/lib/confirm";
import { useCanvasStore } from "@/stores/canvasStore";

/** Shared mileage-warning copy for pause/hibernate — freezing a guest skips a
 * clean service shutdown, so stateful workloads may not recover cleanly on
 * resume. Reused by the project Off control, per-VM actions, and multi-select
 * so the warning stays identical everywhere it appears. */
export const POWER_WARN_MESSAGE =
  "This freezes the guest without a clean service shutdown. Databases, clusters, and " +
  "network services may not recover cleanly when resumed. Mileage may vary.";
export const HIBERNATE_WARN_SUFFIX =
  "\n\nHost RAM is freed; a save image is kept on disk.";

/** Confirms a pause/hibernate action, skipping the dialog once the user has
 * dismissed it for this project (`power_warn_dismissed`, mirrored in
 * `canvasStore` so every entry point — project Off, VM card, context menu,
 * multi-select — agrees on dismissal within the session). Checking the
 * "don't show again" box PATCHes the flag back to the project. */
export async function confirmPowerWarn(
  action: "pause" | "hibernate",
  opts: { title?: string; confirmLabel?: string } = {},
): Promise<boolean> {
  const store = useCanvasStore.getState();
  if (store.powerWarnDismissed) return true;

  const message = action === "hibernate" ? POWER_WARN_MESSAGE + HIBERNATE_WARN_SUFFIX : POWER_WARN_MESSAGE;
  const checkboxRef = { current: false };
  const confirmed = await appConfirm({
    title: opts.title || (action === "hibernate" ? "Hibernate?" : "Pause?"),
    message,
    confirmLabel: opts.confirmLabel || (action === "hibernate" ? "Hibernate" : "Pause"),
    checkboxLabel: "Don't show this again for this project",
    checkboxRef,
  });

  if (confirmed && checkboxRef.current) {
    const projectId = store.currentProjectId;
    useCanvasStore.setState({ powerWarnDismissed: true });
    if (projectId) {
      fetch(`/api/v1/projects/${projectId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ power_warn_dismissed: true }),
      }).catch(() => {});
    }
  }
  return confirmed;
}
