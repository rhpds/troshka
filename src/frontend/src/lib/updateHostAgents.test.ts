import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { agentUpdateMessage, updateHostAgents } from "./updateHostAgents";

const response = (data: unknown, ok = true) => ({ ok, json: async () => data });
const host = (id: string, version: string, status = "connected") => ({
  id, agent_version: version, agent_status: status,
});

describe("updateHostAgents", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

  it("waits through disconnects and old versions until every target is connected at its new version", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ version: "new" }))
      .mockResolvedValueOnce(response({ version: "new" }))
      .mockResolvedValueOnce(response([host("a", "old"), host("b", "old", "disconnected")]))
      .mockResolvedValueOnce(response([host("a", "new"), host("b", "old")]))
      .mockResolvedValueOnce(response([host("a", "new"), host("b", "new")]));
    vi.stubGlobal("fetch", fetchMock);
    let done = false;
    const result = updateHostAgents(["a", "b"]).then((ids) => { done = true; return ids; });
    await vi.advanceTimersByTimeAsync(6000);
    expect(done).toBe(false);
    await vi.advanceTimersByTimeAsync(3000);
    expect(await result).toEqual([]);
  });

  it("keeps waiting for accepted updates when another host request fails", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockRejectedValueOnce(new Error("Network failure"))
      .mockResolvedValueOnce(response({ version: "new" }))
      .mockResolvedValueOnce(response([host("b", "old")]))
      .mockResolvedValueOnce(response([host("b", "new")])));
    let done = false;
    const result = updateHostAgents(["a", "b"]).then((ids) => { done = true; return ids; });
    await vi.advanceTimersByTimeAsync(3000);
    expect(done).toBe(false);
    await vi.advanceTimersByTimeAsync(3000);
    expect(await result).toEqual(["a"]);
  });

  it("reports a timeout when a host never returns, including failed status polls", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(response({ version: "new" }))
      .mockRejectedValue(new Error("Unavailable")));
    const result = expect(updateHostAgents(["a"])).rejects.toThrow("timed out");
    await vi.advanceTimersByTimeAsync(180000);
    await result;
  });
});

describe("agentUpdateMessage", () => {
  it("keeps progress visible even when the stale-host poll becomes empty", () => {
    expect(agentUpdateMessage(true, null, 0)).toBe("Host agent update in progress…");
  });

  it("keeps failures visible when no stale hosts are reported", () => {
    expect(agentUpdateMessage(false, "Timed out", 0)).toBe("Host agent update needs attention.");
  });

  it("uses the host count when no operation or error is present", () => {
    expect(agentUpdateMessage(false, null, 1)).toBe("A host agent update is available.");
    expect(agentUpdateMessage(false, null, 2)).toBe("2 host agents need an update.");
  });
});
