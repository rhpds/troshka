/** Project states that must not mutate topology (canvas, palette, properties, autosave). */
export const TOPOLOGY_LOCKED_STATES = [
  "deploying",
  "reconfiguring",
  "starting",
  "stopping",
  "deleting",
  "cleanup_pending",
] as const;

export function isTopologyLocked(projectState: string): boolean {
  return (TOPOLOGY_LOCKED_STATES as readonly string[]).includes(projectState);
}
