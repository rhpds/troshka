/** Deterministic showroom app-proxy public hosts (mirrors backend showroom_scaffold). */

const APP_PROXY_CODES: Record<string, string> = {
  "console-openshift-console": "con",
  "oauth-openshift": "oauth",
};

function appsClusterSlug(internalHost: string): string {
  const parts = (internalHost || "").trim().toLowerCase().split(".");
  if (parts.length < 3 || parts[1] !== "apps") return "";
  return parts[2].replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 16);
}

/** Stable short suffix for unknown hosts — not a secret. */
function shortHash(host: string): string {
  let h = 0;
  for (let i = 0; i < host.length; i++) {
    h = (Math.imul(31, h) + host.charCodeAt(i)) | 0;
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
  const slug = label.replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 8).replace(/^-+|-+$/g, "");
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

export function deriveAppsDomain(routeHostname: string): string {
  const host = routeHostname || "";
  const i = host.indexOf(".");
  // Mirror Python split(".", 1)[1] — keep everything after the first label.
  return i < 0 ? "" : host.slice(i + 1);
}

/** Extract project namespace from showroom route auto-generated host (mirrors deploy_service). */
export function nsFromShowroomHostname(hostname: string): string {
  const firstLabel = (hostname || "").split(".", 1)[0];
  if (!firstLabel) return "";

  let m = firstLabel.match(/^showroom-(.+)$/);
  if (m && !/^showroom-\d+-/.test(firstLabel)) return m[1];

  m = firstLabel.match(/^troshka-pf-[a-f0-9]+-showroom-(.+)$/);
  if (m && !/^troshka-pf-[a-f0-9]+-showroom-\d+-/.test(firstLabel)) return m[1];

  m = firstLabel.match(/-\d+-/);
  return m ? firstLabel.slice(m.index! + m[0].length) : "";
}

export function eipFromSslipHost(hostnameOrUrl: string): string | null {
  const m = (hostnameOrUrl || "").match(/(\d+\.\d+\.\d+\.\d+)\.sslip\.io/i);
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
