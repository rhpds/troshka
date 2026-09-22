/** OCP Route ports created automatically at deploy time. */
export const OCP_ROUTE_PORTS = new Set(["80", "443", "6443"]);

export function isDeployInProgress(projectState: string): boolean {
  return ["deploying", "reconfiguring", "starting"].includes(projectState);
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

/** Build a browser URL for an OCP Route hostname + external port. */
export function formatOcpRouteUrl(hostname: string, port: string | number): string {
  const p = String(port);
  if (p === "443") return `https://${hostname}`;
  if (p === "80") return `http://${hostname}`;
  const scheme = p === "8443" || p === "6443" ? "https" : "http";
  return `${scheme}://${hostname}:${p}`;
}
