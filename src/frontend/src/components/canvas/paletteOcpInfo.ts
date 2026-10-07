/** Build the palette OPENSHIFT INFO flyout model from canvas nodes + cluster configs. */

import {
  type DeployedShowroomTopo,
  resolveShowroomUrl,
  withShowroomAccessToken,
} from "@/lib/routeUrl";
import {
  appProxyEipPublicHost,
  appProxyPublicHost,
  deriveAppsDomain,
  eipFromSslipHost,
  findShowroomRouteHostname,
  nsFromShowroomHostname,
} from "@/lib/showroomAppProxy";
import { clusterConsoleHosts } from "@/lib/showroomTabs";

export type PaletteOcpClusterInfo = {
  id: string;
  name: string;
  apiUrl: string;
  /** Prefer showroom public console when available; otherwise internal .local. */
  consoleUrl: string;
  /** True when consoleUrl is the public showroom app-proxy link. */
  consoleViaShowroom: boolean;
  kubeadminPassword: string;
  kubeconfig: string;
  kubeconfigVmName: string;
};

export type PaletteOcpInfo = {
  ocpPresent: boolean;
  showroomUrl: string | null;
  clusters: PaletteOcpClusterInfo[];
};

type NodeLike = {
  id: string;
  type?: string;
  data?: Record<string, unknown>;
};

type ClusterLike = {
  id: string;
  name?: string;
  baseDomain?: string;
};

export type PaletteOcpInfoOpts = {
  projectId?: string | null;
  deployed?: DeployedShowroomTopo | null;
};

function clusterFqdn(name: string, baseDomain: string): string {
  return `${name}.${baseDomain}`;
}

function showroomHasConsoleTab(
  showroom: NodeLike | undefined,
  clusterId: string,
  consoleHost: string,
): boolean {
  if (!showroom) return false;
  const tabs = showroom.data?.showroomTabs;
  if (!Array.isArray(tabs)) return false;
  return tabs.some((tab) => {
    if (!tab || typeof tab !== "object") return false;
    const t = tab as Record<string, unknown>;
    if (t.clusterId === clusterId) return true;
    const hosts = Array.isArray(t.proxyHosts)
      ? (t.proxyHosts as unknown[]).map(String)
      : [];
    if (hosts.includes(consoleHost)) return true;
    return typeof t.proxyHost === "string" && t.proxyHost === consoleHost;
  });
}

function publicConsoleUrl(opts: {
  projectId: string;
  consoleHost: string;
  showroomHostname: string | null;
  showroomUrl: string | null;
  accessToken: string | null;
}): string | null {
  const { projectId, consoleHost, showroomHostname, showroomUrl, accessToken } = opts;
  const eip =
    (showroomUrl && eipFromSslipHost(showroomUrl)) ||
    (showroomHostname && eipFromSslipHost(showroomHostname)) ||
    null;
  let host = "";
  if (eip) {
    host = appProxyEipPublicHost(projectId, consoleHost, eip);
  } else if (showroomHostname) {
    const appsDomain = deriveAppsDomain(showroomHostname);
    const ns = nsFromShowroomHostname(showroomHostname);
    if (appsDomain && ns) {
      host = appProxyPublicHost(projectId, consoleHost, appsDomain, ns);
    }
  }
  if (!host) return null;
  return withShowroomAccessToken(`https://${host}`, accessToken);
}

export function buildPaletteOcpInfo(
  nodes: NodeLike[],
  clusters: ClusterLike[],
  opts: PaletteOcpInfoOpts = {},
): PaletteOcpInfo {
  const clusterNodes = nodes.filter((n) => n.type === "clusterNode");
  if (!clusterNodes.length) {
    return { ocpPresent: false, showroomUrl: null, clusters: [] };
  }

  const showroom = nodes.find(
    (n) =>
      n.type === "containerNode" &&
      (!!(n.data as Record<string, unknown> | undefined)?.isShowroom ||
        (n.data as Record<string, unknown> | undefined)?.name === "showroom"),
  );
  const showroomUrl = showroom
    ? resolveShowroomUrl(nodes, opts.deployed)
    : null;
  const showroomHostname = showroom
    ? findShowroomRouteHostname(nodes, opts.deployed?.nodes)
    : null;
  const accessToken =
    (opts.deployed?._showroom_access_token || "").trim() || null;
  const projectId = (opts.projectId || "").trim();

  const byId = Object.fromEntries(clusters.map((c) => [c.id, c]));
  const members = nodes.filter((n) => n.type === "vmNode" && n.data?.clusterId);

  const rows: PaletteOcpClusterInfo[] = [];
  for (const cn of clusterNodes) {
    const d = (cn.data || {}) as Record<string, unknown>;
    const cid =
      (typeof d.clusterId === "string" && d.clusterId) ||
      cn.id.replace(/^cluster-/, "");
    const cfg = byId[cid];
    const name =
      (cfg?.name || (typeof d.name === "string" ? d.name : "") || cid).trim() ||
      cid;
    const baseDomain = (cfg?.baseDomain || "local").trim() || "local";
    const fqdn = clusterFqdn(name, baseDomain);
    const consoleHost = clusterConsoleHosts(name, baseDomain)[0];
    const internalConsole = `https://${consoleHost}`;

    const clusterMembers = members.filter(
      (m) => String(m.data?.clusterId || "") === cid,
    );
    let kubeadminPassword = "";
    let kubeconfig = "";
    let kubeconfigVmName = "";
    for (const m of clusterMembers) {
      const md = m.data || {};
      if (!kubeadminPassword && typeof md.ocpKubeadminPassword === "string") {
        kubeadminPassword = md.ocpKubeadminPassword;
      }
      if (!kubeconfig && typeof md.ocpKubeconfig === "string" && md.ocpKubeconfig) {
        kubeconfig = md.ocpKubeconfig;
        kubeconfigVmName =
          (typeof md.name === "string" && md.name) ||
          (typeof md.label === "string" && md.label) ||
          m.id.slice(0, 8);
      }
    }

    let consoleViaShowroom = false;
    let consoleUrl = internalConsole;
    if (
      showroom &&
      projectId &&
      showroomHasConsoleTab(showroom, cid, consoleHost)
    ) {
      const pub = publicConsoleUrl({
        projectId,
        consoleHost,
        showroomHostname,
        showroomUrl,
        accessToken,
      });
      if (pub) {
        consoleUrl = pub;
        consoleViaShowroom = true;
      }
    }

    rows.push({
      id: cid,
      name,
      apiUrl: `https://api.${fqdn}:6443`,
      consoleUrl,
      consoleViaShowroom,
      kubeadminPassword,
      kubeconfig,
      kubeconfigVmName,
    });
  }

  rows.sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
  return { ocpPresent: true, showroomUrl, clusters: rows };
}
