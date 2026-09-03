import { describe, it, expect } from "vitest";
import { remapClusterProxyTabs, type ShowroomTab } from "./showroomTabs";

const consoleTab = (name: string, hosts: string[]): ShowroomTab => ({
  id: name,
  name,
  type: "proxy",
  proxyHosts: hosts,
  proxyTls: true,
  proxyPort: 443,
});

describe("remapClusterProxyTabs", () => {
  it("rewrites console + oauth hosts to the new apps domain", () => {
    const tabs = [
      consoleTab("ocp Console", [
        "console-openshift-console.apps.ocp.local",
        "oauth-openshift.apps.ocp.local",
      ]),
    ];
    const out = remapClusterProxyTabs(tabs, ".apps.ocp.local", ".apps.ocp2.local");
    expect(out).not.toBeNull();
    expect(out![0].proxyHosts).toEqual([
      "console-openshift-console.apps.ocp2.local",
      "oauth-openshift.apps.ocp2.local",
    ]);
  });

  it("remaps custom app hosts under the cluster too", () => {
    const tabs = [consoleTab("Grafana", ["grafana.apps.ocp.local"])];
    const out = remapClusterProxyTabs(tabs, ".apps.ocp.local", ".apps.ocp2.example.com");
    expect(out![0].proxyHosts).toEqual(["grafana.apps.ocp2.example.com"]);
  });

  it("leaves unrelated hosts and other tab types untouched", () => {
    const tabs: ShowroomTab[] = [
      consoleTab("other cluster", ["console-openshift-console.apps.prod.local"]),
      { id: "t", name: "Terminal", type: "terminal" },
    ];
    expect(remapClusterProxyTabs(tabs, ".apps.ocp.local", ".apps.ocp2.local")).toBeNull();
  });

  it("returns null when nothing references the old suffix", () => {
    const tabs = [consoleTab("x", ["app.apps.other.local"])];
    expect(remapClusterProxyTabs(tabs, ".apps.ocp.local", ".apps.ocp2.local")).toBeNull();
  });

  it("returns null when old and new suffix are equal", () => {
    const tabs = [consoleTab("x", ["console-openshift-console.apps.ocp.local"])];
    expect(remapClusterProxyTabs(tabs, ".apps.ocp.local", ".apps.ocp.local")).toBeNull();
  });
});
