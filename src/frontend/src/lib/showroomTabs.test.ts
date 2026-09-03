import { describe, it, expect } from "vitest";
import {
  syncClusterProxyTabs,
  clusterConsoleHosts,
  clusterConsoleTabName,
  type ShowroomTab,
} from "./showroomTabs";

const consoleTab = (id: string, clusterId: string, name: string, hosts: string[]): ShowroomTab => ({
  id,
  name,
  type: "proxy",
  clusterId,
  proxyHosts: hosts,
  proxyTls: true,
  proxyPort: 443,
});

describe("clusterConsoleHosts / clusterConsoleTabName", () => {
  it("derives console + oauth hosts and the tab name", () => {
    expect(clusterConsoleHosts("ocp", "local")).toEqual([
      "console-openshift-console.apps.ocp.local",
      "oauth-openshift.apps.ocp.local",
    ]);
    expect(clusterConsoleTabName("ocp")).toBe("ocp Console");
  });
});

describe("syncClusterProxyTabs", () => {
  it("re-derives name + hosts for the managed tab on rename", () => {
    const tabs = [
      consoleTab("t1", "c1", "ocp Console", clusterConsoleHosts("ocp", "local")),
    ];
    const out = syncClusterProxyTabs(tabs, { id: "c1", name: "ocp2", baseDomain: "local" });
    expect(out).not.toBeNull();
    expect(out![0].name).toBe("ocp2 Console");
    expect(out![0].proxyHosts).toEqual([
      "console-openshift-console.apps.ocp2.local",
      "oauth-openshift.apps.ocp2.local",
    ]);
  });

  it("only touches tabs linked to the cluster", () => {
    const tabs = [
      consoleTab("t1", "c1", "ocp Console", clusterConsoleHosts("ocp", "local")),
      consoleTab("t2", "other", "other Console", clusterConsoleHosts("other", "local")),
      { id: "t3", name: "Terminal", type: "terminal" } as ShowroomTab,
    ];
    const out = syncClusterProxyTabs(tabs, { id: "c1", name: "ocp2", baseDomain: "local" });
    expect(out![1]).toBe(tabs[1]); // untouched (different cluster)
    expect(out![2]).toBe(tabs[2]); // untouched (terminal)
  });

  it("returns null when nothing changed", () => {
    const tabs = [
      consoleTab("t1", "c1", "ocp Console", clusterConsoleHosts("ocp", "local")),
    ];
    expect(syncClusterProxyTabs(tabs, { id: "c1", name: "ocp", baseDomain: "local" })).toBeNull();
  });

  it("returns null when the cluster lacks name/baseDomain", () => {
    const tabs = [consoleTab("t1", "c1", "x", ["a"])];
    expect(syncClusterProxyTabs(tabs, { id: "c1", name: "", baseDomain: "local" })).toBeNull();
  });
});
