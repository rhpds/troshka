import { describe, it, expect } from "vitest";
import {
  resolveShowroomTabs,
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
  it("re-derives hosts on rename but preserves the user-owned name", () => {
    const tabs = [
      consoleTab("t1", "c1", "ocp Console", clusterConsoleHosts("ocp", "local")),
    ];
    const out = syncClusterProxyTabs(tabs, { id: "c1", name: "ocp2", baseDomain: "local" });
    expect(out).not.toBeNull();
    // Name is user-editable now — sync must NOT overwrite it.
    expect(out![0].name).toBe("ocp Console");
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

describe("resolveShowroomTabs cluster terminal", () => {
  it("resolves a cluster terminal to a local oc shell with no VM/network warning", () => {
    const tabs: ShowroomTab[] = [
      { id: "t1", name: "Terminal", type: "terminal", target: "clusters" },
    ];
    const [r] = resolveShowroomTabs(tabs, [], []);
    expect(r.ocTerminal).toBe(true);
    expect(r.wettyPath).toBe("/wetty_clusters");
    expect(r.wettyPort).toBeGreaterThan(0);
    expect(r.wettyHost).toBeUndefined(); // local shell, not SSH-to-VM
    expect(r.warning).toBeUndefined();
  });

  it("still warns for a classic terminal tab with no VM", () => {
    const tabs: ShowroomTab[] = [{ id: "t2", name: "Shell", type: "terminal" }];
    const [r] = resolveShowroomTabs(tabs, [], []);
    expect(r.warning).toBe("Select a VM for this tab");
  });
});
