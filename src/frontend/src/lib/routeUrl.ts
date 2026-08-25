/** OCP Route ports created automatically at deploy time. */
export const OCP_ROUTE_PORTS = new Set(["80", "443", "6443"]);

export function isDeployInProgress(projectState: string): boolean {
  return ["deploying", "reconfiguring", "starting"].includes(projectState);
}

export function isOcpRoutablePort(port: string | number): boolean {
  return OCP_ROUTE_PORTS.has(String(port));
}

/** Build a browser URL for an OCP Route hostname + external port. */
export function formatOcpRouteUrl(hostname: string, port: string | number): string {
  const p = String(port);
  if (p === "443") return `https://${hostname}`;
  if (p === "80") return `http://${hostname}`;
  const scheme = p === "8443" || p === "6443" ? "https" : "http";
  return `${scheme}://${hostname}:${p}`;
}
