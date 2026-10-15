import { describe, expect, it } from "vitest";
import { summarizeDeployProgress } from "../deployProgress";

describe("project deployment summary", () => {
  it("keeps VM counts without flattening the disk log into the project row", () => {
    expect(summarizeDeployProgress({
      step: "transferring disks",
      detail: "Starting VMs: 2/4 started\nsource: done\ndest: importing image",
    })).toBe("transferring disks · 2/4 VMs started");
  });

  it("keeps a short single-line detail", () => {
    expect(summarizeDeployProgress({ step: "starting vms", detail: "3/4 started" }))
      .toBe("starting vms · 3/4 started");
  });

  it("uses the stage for multiline disk details without VM counts", () => {
    expect(summarizeDeployProgress({ step: "images", detail: "source: done\ndest: downloading 60%" }))
      .toBe("images");
  });

  it("bounds long legacy details", () => {
    expect(summarizeDeployProgress({ detail: "x".repeat(300) }).length).toBeLessThanOrEqual(80);
  });
});
