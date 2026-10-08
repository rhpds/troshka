/**
 * Resolve the Troshka backend origin for CLI tools (troshka-oc, curl + API key).
 *
 * The browser UI often sits behind oauth-proxy on the ``troshka.`` Route host.
 * API keys must hit the dedicated ``troshka-api.`` Route (or local :8200), not
 * ``window.location.origin``.
 */
export function troshkaApiOriginForCli(uiOrigin: string): string {
  let u: URL;
  try {
    u = new URL(uiOrigin);
  } catch {
    return uiOrigin.replace(/\/$/, "");
  }

  const host = u.hostname;
  if (host === "localhost" || host === "127.0.0.1") {
    // Next.js UI (:3100) rewrites some paths; CLI must talk to the backend.
    if (u.port === "3100" || u.port === "") {
      return `${u.protocol}//${host}:8200`;
    }
    return u.origin;
  }

  // Helm backend-route: route.host | replace "troshka." "troshka-api."
  if (host.startsWith("troshka.") && !host.startsWith("troshka-api.")) {
    u.hostname = `troshka-api.${host.slice("troshka.".length)}`;
    return u.origin;
  }

  return u.origin;
}

/**
 * Resolve the dedicated tunnel service origin for ``troshka-oc`` WebSockets.
 *
 * Nested OCP API tunnels run on ``troshka-tunnel`` (not the API worker).
 * Locally that is ``:8201``; in prod Helm replaces ``troshka-api.`` /
 * ``troshka.`` with ``troshka-tunnel.``.
 */
export function troshkaTunnelOriginForCli(apiOrUiOrigin: string): string {
  const apiOrigin = troshkaApiOriginForCli(apiOrUiOrigin);
  let u: URL;
  try {
    u = new URL(apiOrigin);
  } catch {
    return apiOrigin.replace(/\/$/, "");
  }

  const host = u.hostname;
  if (host === "localhost" || host === "127.0.0.1") {
    if (u.port === "8200" || u.port === "3100" || u.port === "") {
      return `${u.protocol}//${host}:8201`;
    }
    return u.origin;
  }

  if (host.startsWith("troshka-api.")) {
    u.hostname = `troshka-tunnel.${host.slice("troshka-api.".length)}`;
    return u.origin;
  }
  if (host.startsWith("troshka.") && !host.startsWith("troshka-tunnel.")) {
    u.hostname = `troshka-tunnel.${host.slice("troshka.".length)}`;
    return u.origin;
  }
  return u.origin;
}
