/** Ports that may appear as Route keys on OpenShift-ingress providers.
 *  Secondary cluster APIs use 6444+ listen keys (intPort still 6443). */
export const OCP_ROUTE_PORTS = new Set(["80", "443", "6443"]);

export function isDeployInProgress(projectState: string): boolean {
  return ["deploying", "reconfiguring", "starting"].includes(projectState);
}

/** True when this forward is served by an OpenShift Route (mirror backend). */
export function isOcpRoutableForward(pf: {
  extPort?: string | number;
  intPort?: string | number;
}): boolean {
  const ext = String(pf.extPort ?? "").trim();
  if (ext === "80" || ext === "443" || ext === "6443") return true;
  return String(pf.intPort ?? "").trim() === "6443";
}

export function isOcpRoutablePort(port: string | number): boolean {
  return OCP_ROUTE_PORTS.has(String(port));
}

export type RouteEndpoint = {
  hostname?: string;
  port?: number;
  vmIp?: string;
  vmName?: string;
  type?: string;
};

/**
 * Match a port-forward to its Route endpoint. Prefer matching on BOTH external
 * port AND internal IP (endpoints carry vmIp), so two forwards sharing an
 * external port — e.g. the cluster ingress 443 and the showroom 443 — resolve
 * to their own routes instead of both collapsing onto the first 443 endpoint.
 * Falls back to port-only for legacy endpoints that predate vmIp.
 */
export function findRouteForForward<T extends RouteEndpoint>(
  routes: T[],
  pf: { extPort?: string | number; intIp?: string },
): T | undefined {
  const port = String(pf.extPort);
  const intIp = (pf.intIp || "").trim();
  return (
    routes.find(
      (ep) => String(ep.port) === port && (ep.vmIp || "").trim() === intIp,
    ) || routes.find((ep) => String(ep.port) === port)
  );
}

/** Build a browser URL for an OCP Route hostname + external port key.
 *  OpenShift Routes are always reached via the router on 80/443 — the
 *  gateway listen key (6443, 6444, …) is not part of the public URL. */
export function formatOcpRouteUrl(hostname: string, port: string | number): string {
  const p = String(port);
  if (p === "80") return `http://${hostname}`;
  return `https://${hostname}`;
}

export type DeployedShowroomTopo = {
  _showroom_url?: string;
  _showroom_access_token?: string;
  nodes?: Array<{ data?: Record<string, unknown> }>;
};

/** Append ``?token=`` (or ``&token=``) when missing; replace an existing token. */
export function withShowroomAccessToken(
  url: string,
  token: string | null | undefined,
): string {
  const base = (url || "").trim();
  const t = (token || "").trim();
  if (!base || !t) return base;
  try {
    const u = new URL(base);
    u.searchParams.set("token", t);
    return u.toString();
  } catch {
    if (/[?&]token=/.test(base)) {
      return base.replace(/([?&])token=[^&]*/, `$1token=${encodeURIComponent(t)}`);
    }
    return base.includes("?")
      ? `${base}&token=${encodeURIComponent(t)}`
      : `${base}?token=${encodeURIComponent(t)}`;
  }
}

/** Prefer stamped showroom URL; else base URL + access token from deployed topo. */
export function showroomPublicUrl(
  baseUrl: string | null | undefined,
  deployed?: DeployedShowroomTopo | null,
): string | null {
  const stamped = (deployed?._showroom_url || "").trim();
  if (stamped) {
    if (stamped.includes("token=") || !deployed?._showroom_access_token) return stamped;
    return withShowroomAccessToken(stamped, deployed._showroom_access_token);
  }
  const base = (baseUrl || "").trim();
  if (!base) return null;
  return withShowroomAccessToken(base, deployed?._showroom_access_token) || base;
}

/** Resolve the project showroom URL from deployed topology or gateway endpoints. */
export function resolveShowroomUrl(
  nodes: Array<{ data?: Record<string, unknown> }>,
  deployed?: DeployedShowroomTopo | null,
): string | null {
  const stamped = showroomPublicUrl(null, deployed);
  if (stamped) return stamped;

  const scan = (list: Array<{ data?: Record<string, unknown> }> | undefined) => {
    for (const n of list || []) {
      const eps = n.data?.externalEndpoints;
      if (!Array.isArray(eps)) continue;
      for (const ep of eps) {
        if (!ep || typeof ep !== "object") continue;
        const row = ep as RouteEndpoint;
        if (row.vmName === "showroom" && row.hostname) {
          return showroomPublicUrl(
            formatOcpRouteUrl(row.hostname, row.port ?? 443),
            deployed,
          );
        }
      }
    }
    return null;
  };

  return scan(nodes) || scan(deployed?.nodes) || null;
}

/** Build a browser URL for an EIP-bound web forward (cloud showroom :443 etc.).
 *  Returns null for non-browser ports (e.g. API 6443) so callers keep ip:port text.
 *
 *  Showroom forwards use ``showroom.<eip>.sslip.io`` (LE HTTP-01) so External
 *  Access matches the trusted hostname; prefer ``showroomUrl`` from
 *  ``deployed_topology._showroom_url`` when deploy already stamped one.
 */
export function formatEipAccessUrl(
  ip: string,
  port: string | number,
  opts?: { showroom?: boolean; showroomUrl?: string | null },
): string | null {
  const p = String(port).trim();
  if (opts?.showroom && (p === "443" || p === "80")) {
    const stamped = (opts.showroomUrl || "").trim();
    if (stamped) return stamped;
    return p === "80"
      ? `http://showroom.${ip}.sslip.io`
      : `https://showroom.${ip}.sslip.io`;
  }
  if (p === "80") return `http://${ip}`;
  if (p === "443") return `https://${ip}`;
  return null;
}
