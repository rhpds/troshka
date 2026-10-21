/** Compact project-row text; retain the full detail in the progress view. */
export function summarizeDeployProgress(progress: { step?: string; detail?: string }): string {
  const step = progress.step || "";
  const detail = progress.detail || "";
  const vmCount = /(?:Starting VMs|Waiting for VMs):\s*(\d+\/\d+)\s+started/i.exec(detail);
  if (vmCount) return `${step || "deploying"} · ${vmCount[1]} VMs started`;
  if (detail.includes("\n")) return step || "deploying";
  const summary = [step, detail].filter(Boolean).join(" · ");
  return summary.length > 80 ? `${summary.slice(0, 79)}…` : summary;
}
