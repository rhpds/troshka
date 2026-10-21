import { describe, expect, it } from "vitest";
import type { Edge, Node } from "@xyflow/react";
import {
  applyShowroomTabsToNode, buildNginxConfig, buildUiConfigYaml, buildWettyContainers,
  getPodDisplayPorts, getShowroomExternalPort, getShowroomGatewayTargetPort,
  getVmNicIpOnNetwork, newShowroomTab, resolveShowroomTabs,
  type ResolvedShowroomTab, type ShowroomTab,
} from "./showroomTabs";
import { parseWettyCommand } from "./wettyContainer";

const node = (id: string, type: string, data: Record<string, unknown> = {}): Node => ({
  id, type, data, position: { x: 0, y: 0 },
});
const vm = node("vm", "vmNode", { name: "My VM", ciLoginUser: "admin", ciCloudUserPassword: "test-password", nics: [{ id: "nic", ip: "192.168.10.5" }] });
const network = node("net", "networkNode", { name: "lab" });
const connection: Edge = { id: "nic-net", source: "vm", target: "net", sourceHandle: "nic-nic-top" };
const terminal: ShowroomTab = { id: "shell", name: "Shell", type: "terminal", vmId: "vm", network: "lab" };
const proxy: ShowroomTab = { ...terminal, id: "web", name: "Web", type: "proxy" };
const gateway = (portForwards: Record<string, unknown>[]) => node("gw", "networkNode", { subtype: "gateway", portForwards });

describe("VM showroom tab resolution", () => {
  it("resolves terminal and TLS proxy tabs on a named network", () => {
    const result = resolveShowroomTabs([terminal, { ...proxy, proxyTls: true, proxyPort: 8443, proxyPath: "/app" }], [vm, network], [connection]);
    expect(result[0]).toMatchObject({ wettyPath: "/wetty_my_vm", wettyPort: 8001, wettyHost: "192.168.10.5" });
    expect(result[1]).toMatchObject({ proxyPath: "/app/", proxyTarget: "https://192.168.10.5:8443", proxyTls: true });
  });
  it("supports reverse NIC connections and network IDs", () => {
    const reverse = { ...connection, source: "net", target: "vm", sourceHandle: undefined, targetHandle: "nic-nic-bottom" };
    expect(getVmNicIpOnNetwork("vm", "net", [vm, network], [reverse])).toBe("192.168.10.5");
    expect(resolveShowroomTabs([{ ...proxy, network: undefined, networkId: "net" }], [vm, network], [reverse])[0])
      .toMatchObject({ proxyPath: "/my_vm/", proxyTarget: "http://192.168.10.5:80" });
  });
  it.each([
    [{ vmId: "missing" }, "Select a VM for this tab"],
    [{ vmId: "net" }, "Select a VM for this tab"],
    [{ network: undefined }, "Select a network for this tab"],
    [{ network: "missing" }, "Unknown network 'missing'"],
  ])("reports unresolved selection %s", (patch, warning) => {
    expect(resolveShowroomTabs([{ ...terminal, ...patch }], [vm, network], [connection])[0].warning).toBe(warning);
  });
  it("requires an assigned address on the selected NIC", () => {
    expect(getVmNicIpOnNetwork("missing", "net", [vm], [])).toBe("");
    expect(getVmNicIpOnNetwork("vm", "other", [vm], [connection])).toBe("");
    expect(getVmNicIpOnNetwork("vm", "net", [node("vm", "vmNode")], [connection])).toBe("");
    expect(resolveShowroomTabs([terminal], [vm, network], [])[0].warning).toBe("My VM has no IP on lab");
    expect(resolveShowroomTabs([{ ...terminal, networkId: "net" }], [vm, node("net", "networkNode", { label: "Management" })], [])[0].warning)
      .toBe("My VM has no IP on Management");
  });
  it("does not resolve a router as a lab network", () => {
    expect(resolveShowroomTabs([terminal], [vm, node("net", "networkNode", { name: "lab", subtype: "router" })], [connection])[0].warning)
      .toBe("Unknown network 'lab'");
  });
  it("resolves hostname proxies independently of VM selection", () => {
    const [result] = resolveShowroomTabs([{ ...proxy, vmId: undefined, proxyHost: "service.lab", proxyTls: true }], [], []);
    expect(result).toMatchObject({ proxyPath: "/service_lab/", proxyTarget: "https://service.lab:80", proxyHost: "service.lab" });
    expect(resolveShowroomTabs([{ ...proxy, proxyHost: "service.lab", proxyPath: "/app/", proxyPort: 8080 }], [], [])[0].proxyPath).toBe("/app/");
  });
  it("keeps external and app-proxy tabs without allocating terminal ports", () => {
    const external: ShowroomTab = { id: "docs", name: "Docs", type: "external", url: "https://example.com" };
    const app = { ...proxy, proxyHosts: ["console.lab", "oauth.lab"] };
    const result = resolveShowroomTabs([external, app, terminal, { ...terminal, id: "cluster", target: "clusters" }], [vm, network], [connection]);
    expect(result[0]).toEqual({ tab: external });
    expect(result[1]).toEqual({ tab: app, appProxyHosts: app.proxyHosts });
    expect(result[2].wettyPort).toBe(8001);
    expect(result[3]).toMatchObject({ wettyPort: 8002, ocTerminal: true });
  });
});

describe("showroom gateway ports", () => {
  it("prefers the forward targeting the showroom IP", () => {
    const nodes = [network, gateway([
      { intIp: "other", intPort: "80", extPort: "443" },
      { intIp: "192.168.10.4", intPort: "8080", extPort: "8443" },
    ])];
    expect(getShowroomExternalPort(nodes, "192.168.10.4")).toBe(8443);
  });
  it("resolves explicit target ports and rejects unusable external ports", () => {
    const nodes = [gateway([
      { intIp: "192.168.10.4", intPort: "0", extPort: "bad" },
      { intIp: "192.168.10.4", intPort: "8080", extPort: "8443" },
    ])];
    expect(getShowroomGatewayTargetPort(nodes, "192.168.10.4")).toBe(8080);
    expect(getShowroomExternalPort(nodes, "192.168.10.4")).toBe(8443);
  });
  it.each(["80", "443"])("falls back to the standard web forward %s", (extPort) => {
    const nodes = [network, gateway([{ intPort: "80", extPort }])];
    expect(getShowroomExternalPort(nodes, "")).toBe(Number(extPort));
    expect(getShowroomGatewayTargetPort(nodes, "")).toBe(80);
  });
  it("uses defaults when no standard web mapping exists", () => {
    for (const nodes of [[], [network], [gateway([])], [gateway([{ intPort: "8080", extPort: "8080" }])]]) {
      expect(getShowroomExternalPort(nodes, "")).toBe(443);
      expect(getShowroomGatewayTargetPort(nodes, "")).toBe(80);
    }
  });
  it("shows pod host ports with direct-container fallback", () => {
    expect(getPodDisplayPorts({}, [{ ports: [{ hostPort: 8080 }, { hostPort: null }, { hostPort: 0 }] }, {}], [], false)).toEqual([8080]);
    expect(getPodDisplayPorts({ ports: [{ hostPort: 22 }, { hostPort: -1 }] }, [], [], false)).toEqual([22]);
    expect(getPodDisplayPorts({}, [], [], false)).toEqual([]);
    expect(getPodDisplayPorts({}, [], [], true)).toEqual([80]);
    expect(getPodDisplayPorts({ nics: [{ ip: "192.168.10.4" }] }, [], [gateway([{ intIp: "192.168.10.4", intPort: "8080" }])], true)).toEqual([8080]);
  });
});

describe("showroom generated configuration", () => {
  const resolved = () => resolveShowroomTabs([terminal, { ...proxy, proxyHost: "service.lab", proxyTls: true }], [vm, network], [connection]);
  it("generates UI entries only for usable tabs", () => {
    const external: ResolvedShowroomTab = { tab: { id: "docs", name: "Docs", type: "external", url: "https://example.com" } };
    const yaml = buildUiConfigYaml([...resolved(), external, { tab: terminal }], 8443);
    expect(yaml).toContain("path: /wetty_my_vm\n    port: 8443");
    expect(yaml).toContain("url: '/service_lab/'");
    expect(yaml).toContain("url: https://example.com\n    external: true");
    expect(yaml.match(/name: Shell/g)).toHaveLength(1);
  });
  it("preserves WebSocket upgrades and hostname TLS SNI in proxy configuration", () => {
    const nginx = buildNginxConfig(resolved());
    expect(nginx).toContain("proxy_pass http://127.0.0.1:8001/wetty_my_vm;");
    expect(nginx).toContain("proxy_set_header Upgrade $http_upgrade;");
    expect(nginx).toContain("proxy_set_header Host service.lab;");
    expect(nginx).toContain("proxy_ssl_server_name on;");
    expect(nginx).toContain("proxy_ssl_name service.lab;");
    expect(buildNginxConfig([{ tab: proxy, proxyPath: "/app", proxyTarget: "http://192.168.10.5:80" }]))
      .toContain("location /app/ {");
    expect(buildNginxConfig([])).not.toContain("proxy_ssl");
  });
  it("uses tab SSH overrides or VM defaults without shell interpolation", () => {
    const defaults = buildWettyContainers(resolved(), [terminal, proxy], [vm, network], "disk");
    expect(defaults).toHaveLength(1);
    expect(parseWettyCommand(defaults[0].command)).toMatchObject({ sshUser: "admin", sshPass: "test-password", sshPort: 22, sshHost: "192.168.10.5" });
    const override = { ...terminal, sshUser: "custom", sshPass: "p'ass", sshPort: 2222 };
    const containers = buildWettyContainers(resolved(), [override], [vm], "disk");
    expect(parseWettyCommand(containers[0].command)).toMatchObject({ sshUser: "custom", sshPass: "p'ass", sshPort: 2222 });
    expect(containers[0]).toMatchObject({ name: "wetty-my_vm", cpus: 1, memory: 256, ports: [{ containerPort: 8001, hostPort: null, protocol: "tcp" }] });
  });
  it("replaces generated terminal containers and encodes updated config without mutating the source", () => {
    const content = { name: "content", image: "content" };
    const clone = { name: "git-cloner" };
    const data = { mounts: [{ diskNodeId: "disk" }], nics: [{ ip: "192.168.10.4" }],
      podContainers: [content, { name: "wetty-old" }], initContainers: [clone, { name: "nginx-config", envVars: [] }] };
    const result = applyShowroomTabsToNode(data, [terminal], [vm, network], [connection]);
    expect(result.showroomTabs).toEqual([terminal]);
    const containers = result.podContainers as { name: string }[];
    expect(containers.map((item) => item.name)).toEqual(["content", "wetty-my_vm"]);
    expect(containers[0]).toBe(content);
    const init = result.initContainers as { name: string; envVars: { key: string; value: string }[] }[];
    expect(init[0]).toBe(clone);
    expect(atob(init[1].envVars[0].value)).toContain("/wetty_my_vm");
    expect(atob(init[1].envVars[1].value)).toContain("port: 443");
    expect(data.podContainers[1].name).toBe("wetty-old");
  });
  it("retains tab data when no config disk exists", () => {
    expect(applyShowroomTabsToNode({ name: "showroom" }, [terminal], [], []))
      .toEqual({ name: "showroom", showroomTabs: [terminal] });
    expect(applyShowroomTabsToNode({ mounts: [{ diskNodeId: "disk" }] }, [], [], []))
      .toMatchObject({ initContainers: [], podContainers: [] });
  });
  it.each(["terminal", "proxy", "external"] as const)("initializes defaults and distinct IDs for %s tabs", (type) => {
    const first = newShowroomTab(type, "New tab");
    expect(first.id).not.toBe(newShowroomTab(type, "New tab").id);
    expect(first).toMatchObject({ name: "New tab", type, proxyTls: false });
    expect(first.proxyPort).toBe(type === "proxy" ? 80 : undefined);
    expect(first.url).toBe(type === "external" ? "https://example.com" : undefined);
  });
});
