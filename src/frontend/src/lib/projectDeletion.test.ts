import { afterEach, describe, expect, it, vi } from "vitest";
import { isAsyncProjectDeletion, requestProjectDeletion } from "./projectDeletion";

afterEach(() => vi.unstubAllGlobals());

describe("project deletion responses", () => {
  it.each(["deleting", "cleanup_pending"])("marks %s as in-flight deletion", (status) => {
    expect(isAsyncProjectDeletion(status)).toBe(true);
  });
  it("does not treat settled states as in-flight deletion", () => {
    expect(isAsyncProjectDeletion("active")).toBe(false);
    expect(isAsyncProjectDeletion(undefined)).toBe(false);
  });
  it.each(["deleting", "cleanup_pending"])("preserves asynchronous %s state", async status => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ status })));
    expect(await requestProjectDeletion("/project")).toBe(status);
  });
  it("accepts immediate deletion without a response body", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 204 })));
    expect(await requestProjectDeletion("/project")).toBeUndefined();
  });
  it("reports non-JSON server failures", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("Bad Gateway", { status: 502 })));
    await expect(requestProjectDeletion("/project")).rejects.toThrow("Delete failed (HTTP 502)");
  });
  it("propagates connection failures", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    await expect(requestProjectDeletion("/project")).rejects.toThrow("offline");
  });
});
