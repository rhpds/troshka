import { afterEach, expect, it, vi } from "vitest";
import { fetchNativeStorage, roundedGiB } from "./kubevirtStorage";
afterEach(() => vi.unstubAllGlobals());
it("retains exact integer API tokens above 2^53 and encodes the scope ID", async () => {
  const fetcher = vi.fn(async () => new Response('{"scope":"project","scope_id":"p/q","metric":"rbd_allocated_extent_bytes","status":"complete","allocated_bytes":9007199254740993,"known_allocated_bytes":9007199254740993,"provisioned_bytes":9223372036854775807,"observed_at":"2026-11-11T12:00:00Z","covered_projects":1,"expected_projects":1,"reason_codes":[],"enforcement_eligible":false}'));
  vi.stubGlobal("fetch", fetcher);
  const data = await fetchNativeStorage("project", "p/q");
  expect(fetcher).toHaveBeenCalledWith("/api/v1/quotas/project/p%2Fq/native-storage", undefined);
  expect(data.allocated_bytes).toBe("9007199254740993");
  expect(data.provisioned_bytes).toBe("9223372036854775807");
});
it("rounds readable GiB with integer arithmetic", () => {
  expect(roundedGiB(0)).toBe("0");
  expect(roundedGiB(1073741823)).toBe("1");
  expect(roundedGiB(4194304)).toBe("<0.01");
  expect(roundedGiB("9007199254740993")).toBe("8388608");
  expect(roundedGiB("9223372036854775807")).toBe("8589934592");
});
