/** Deterministic showroom app-proxy public hosts (mirrors backend showroom_scaffold). */
import { trimEdgeCharacter } from "./textBoundaries";

const APP_PROXY_CODES: Record<string, string> = {
  "console-openshift-console": "con",
  "oauth-openshift": "oauth",
};

function appsClusterSlug(internalHost: string): string {
  const parts = (internalHost || "").trim().toLowerCase().split(".");
  if (parts.length < 3 || parts[1] !== "apps") return "";
  return trimEdgeCharacter(parts[2].replaceAll(/[^a-z0-9-]+/g, "-"), "-").slice(0, 16);
}

/** Stable short suffix for unknown hosts — not a secret. */
function shortHash(host: string): string {
  let h = 0;
  for (let i = 0; i < host.length; i++) {
    // Route names use the existing UTF-16 code-unit hash and signed 32-bit overflow.
    h = Math.imul(1, Math.imul(31, h) + host.charCodeAt(i));
  }
  return (h >>> 0).toString(16).padStart(8, "0").slice(0, 6);
}

export function appProxyRouteCode(internalHost: string): string {
  const host = (internalHost || "").trim().toLowerCase();
  const label = host.split(".")[0] || "";
  const cluster = appsClusterSlug(host);
  if (label in APP_PROXY_CODES) {
    const base = APP_PROXY_CODES[label];
    return cluster ? `${base}-${cluster}` : base;
  }
  const slug = trimEdgeCharacter(trimEdgeCharacter(label.replaceAll(/[^a-z0-9]+/g, "-"), "-").slice(0, 8), "-");
  const generic = slug ? `${slug}-${shortHash(host)}` : shortHash(host);
  return cluster ? `${generic}-${cluster}` : generic;
}

export function appProxyRouteName(projectId: string, internalHost: string): string {
  return `tpf-${projectId.slice(0, 8)}-${appProxyRouteCode(internalHost)}`;
}

export function appProxyPublicHost(
  projectId: string,
  internalHost: string,
  appsDomain: string,
  namespace: string,
): string {
  return `${appProxyRouteName(projectId, internalHost)}-${namespace}.${appsDomain}`;
}

export function appProxyEipPublicHost(
  projectId: string,
  internalHost: string,
  eip: string,
  namespace = "lab",
): string {
  return `${appProxyRouteName(projectId, internalHost)}-${namespace}.${eip}.sslip.io`;
}

export function deriveAppsDomain(routeHostname = ""): string {
  const i = routeHostname.indexOf(".");
  // Mirror Python split(".", 1)[1] — keep everything after the first label.
  return i < 0 ? "" : routeHostname.slice(i + 1);
}

/** Extract project namespace from showroom route auto-generated host (mirrors deploy_service). */
export function nsFromShowroomHostname(hostname: string): string {
  const firstLabel = (hostname || "").split(".", 1)[0];
  if (!firstLabel) return "";

  let m = /^showroom-(.+)$/.exec(firstLabel);
  if (m && !/^showroom-\d+-/.test(firstLabel)) return m[1];

  m = /^troshka-pf-[a-f0-9]+-showroom-(.+)$/.exec(firstLabel);
  if (m && !/^troshka-pf-[a-f0-9]+-showroom-\d+-/.test(firstLabel)) return m[1];

  m = /-\d+-/.exec(firstLabel);
  return m ? firstLabel.slice(m.index! + m[0].length) : "";
}

export function eipFromSslipHost(hostnameOrUrl: string): string | null {
  const m = /(?<!\d)(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\.sslip\.io/i.exec(hostnameOrUrl || "");
  return m?.[1] || null;
}

export function findShowroomRouteHostname(
  nodes: Array<{ data?: Record<string, unknown> }>,
  deployedNodes?: Array<{ data?: Record<string, unknown> }>,
): string | null {
  const scan = (list: Array<{ data?: Record<string, unknown> }> | undefined) => {
    for (const n of list || []) {
      const data = n.data || {};
      if (data.subtype !== "gateway") continue;
      const eps = data.externalEndpoints;
      if (!Array.isArray(eps)) continue;
      for (const ep of eps) {
        if (!ep || typeof ep !== "object") continue;
        const row = ep as { vmName?: string; hostname?: string };
        if (row.vmName === "showroom" && row.hostname) return row.hostname;
      }
    }
    return null;
  };
  return scan(nodes) || scan(deployedNodes) || null;
}
