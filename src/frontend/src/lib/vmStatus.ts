/** Human label for a VM's lifecycle status. `paused` and `hibernated` are
 * distinct, billable-but-off(ish) states and must never collapse to
 * "Stopped" — that's what pause/hibernate badges exist to avoid confusing
 * with a true power-off. */
export function vmStatusLabel(status?: string | null): string {
  switch (status) {
    case "running":
      return "Running";
    case "paused":
      return "Paused";
    case "hibernated":
      return "Hibernated";
    case "starting":
      return "Starting…";
    case "stopping":
      return "Stopping…";
    case "restarting":
      return "Restarting…";
    case "redeploying":
      return "Redeploying…";
    case "not_found":
      return "Not Found";
    default:
      return "Stopped";
  }
}
