import { describe, expect, it } from "vitest";
import { isTopologyLocked, TOPOLOGY_LOCKED_STATES } from "./canvasTopologyLock";

describe("isTopologyLocked", () => {
  it.each([...TOPOLOGY_LOCKED_STATES])("locks topology while %s", (state) => {
    expect(isTopologyLocked(state)).toBe(true);
  });

  it.each(["draft", "active", "stopped", "error"])("allows edits while %s", (state) => {
    expect(isTopologyLocked(state)).toBe(false);
  });
});
