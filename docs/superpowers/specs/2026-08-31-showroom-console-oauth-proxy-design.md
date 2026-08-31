# Showroom embedded OCP console + OAuth proxy (nested `.local` clusters)

**Date:** 2026-08-31
**Status:** Design — awaiting review

## Problem

The showroom "OCP Console" tab (`type: proxy`) proxies the nested cluster's
console at an internal, non-resolvable domain (`console-openshift-console.apps.ocp.ocp.local`).
Students reach the showroom only through a **public** URL
(`https://troshka-pf-<pid>-showroom-443-<ns>.apps.<cluster>/...`) from an
**external browser that cannot resolve `.local` at all**, and the console must
render **embedded in the split-view iframe**.

Two independent defects were found while debugging a live 504:

1. **SNAT (already fixed, separate commit):** the showroom pod on the transit
   subnet couldn't reach the lab bridge; the nested SNO dropped its foreign-subnet
   source. Fixed by adding a per-bridge masquerade in troshkad
   `_allow_infra_veth_forward`. *Out of scope for this spec.*

2. **Console/OAuth proxying (this spec):** the current single
   `location /console/` block proxies only the console host and cannot carry the
   console → `oauth-openshift.apps.ocp.ocp.local` redirect, does not strip
   `X-Frame-Options: DENY` (so it can't be iframed), and exposes `.local`
   hostnames the browser can't resolve.

## Hard constraints (locked with user)

- External browser; **cannot resolve `.local`**. All browser-visible URLs must be
  public and TLS-valid.
- Console + OAuth must work **embedded in the iframe**.
- Nested cluster stays **untouched** (no OAuthClient / ingress / basePath changes).
- Internal cluster domain stays `*.apps.ocp.ocp.local`.
- Per-app mapping is acceptable (one public route per embedded app).

## Key correctness insight

The OpenShift console builds its OAuth `redirect_uri` from its **configured base
address** (`BRIDGE_BASE_ADDRESS` = the `.local` route), not from the incoming
request Host. So `redirect_uri` is always the registered
`https://console-openshift-console.apps.ocp.ocp.local/auth/callback`.

Therefore the proxy must:
- rewrite the **host** in redirect `Location` headers and `Set-Cookie` domains
  (`.local` → public), so the browser only ever sees resolvable public URLs; and
- **never rewrite the `redirect_uri` query parameter** — leaving it `.local` means
  OAuth's registered-URI validation still passes with the cluster untouched.

The final authorization-code → token exchange is console → oauth **server-side**
(internal `.local`), which the showroom pod can already reach (post-SNAT-fix).

## Design

### 1. Per-app public routes → one showroom nginx

Each embedded app needs a public route (OCP Routes are not wildcard). All routes
point at the **same showroom nginx** (the showroom infra IP, port 80); nginx
selects the backend by `Host`. For the console tab this is a minimum of **two**
apps: `console-openshift-console` and `oauth-openshift`.

Reuse the existing `_create_routes_for_gateway` / `create_route_access` machinery
(Service + Route + two-hop DNAT to the showroom infra IP). Each app proxy yields
one endpoint: `{ appHost: "<internal-fqdn>", publicHostname: "<result.hostname>" }`.

### 2. Showroom tab schema

Extend the `proxy` tab so it can declare the internal hosts to expose. The first
is the iframe target; the rest are companions that also get routes + vhosts:

```yaml
- name: OCP Console
  type: proxy
  proxy_hosts:                       # ordered; [0] = iframe target
    - console-openshift-console.apps.ocp.ocp.local
    - oauth-openshift.apps.ocp.ocp.local
  proxy_tls: true
  proxy_port: 443
```

Back-compat: an existing single `proxy_host` is treated as `proxy_hosts: [host]`
and, when the host is a `console-openshift-console.*`, `oauth-openshift.<same
suffix>` is auto-appended.

The rendered tab `url` is the **public** hostname of `proxy_hosts[0]` (filled at
deploy time, see §4).

### 3. nginx: host-mapped rewriting vhost

Because public hostnames are deploy-time facts and are **truncated/mangled** by
`create_route_access` (`<vm_name>[:20]`), nginx cannot derive the internal name
from the public one. So the config carries two explicit maps, generated at deploy
time (§4): forward (`public → internal`) for `proxy_pass`, and reverse
(`internal → public`) for `Location` rewriting. One server block handles all app
proxies:

```nginx
resolver 10.0.0.1 valid=30s;                       # project dnsmasq

# generated at deploy time, one entry per app proxy
map $host $troshka_backend {                        # public host -> internal host
    <public-console-host>   console-openshift-console.apps.ocp.ocp.local;
    <public-oauth-host>     oauth-openshift.apps.ocp.ocp.local;
}

server {
    listen 80;
    server_name <public-console-host> <public-oauth-host>;   # all app hosts
    location / {
        proxy_pass            https://$troshka_backend$request_uri;
        proxy_ssl_server_name on;
        proxy_ssl_name        $troshka_backend;
        proxy_ssl_verify      off;
        proxy_set_header      Host $troshka_backend;          # Host-route at OCP ingress
        proxy_set_header      X-Forwarded-Proto https;        # browser is on edge-TLS route
        proxy_http_version    1.1;
        proxy_set_header      Upgrade $http_upgrade;          # console websockets
        proxy_set_header      Connection $connection_upgrade;
        proxy_hide_header     X-Frame-Options;                # Defect C: allow iframe
        # Rewrite redirect Location host .local -> public. One proxy_redirect per
        # app (generated from the reverse map), so redirect_uri params are left
        # untouched (only the leading Location host is swapped):
        proxy_redirect        https://console-openshift-console.apps.ocp.ocp.local/  https://<public-console-host>/;
        proxy_redirect        https://oauth-openshift.apps.ocp.ocp.local/            https://<public-oauth-host>/;
        proxy_cookie_domain   .apps.ocp.ocp.local  $host;
    }
}
```

Using one **literal** `proxy_redirect` per app (rather than a single regex) keeps
the rewrite to the leading Location host only and leaves `redirect_uri` query
params as `.local` — the correctness requirement. The existing showroom
content/wetty server block (on the showroom's own host) is unchanged.

### 4. Deploy-time generation

The base nginx config (content + wetty + `resolver` + `map $http_upgrade`) stays
built at topology-build time in `showroom_scaffold.py`, and its `http {}` block
gains a single `include /showroom/nginx/conf.d/app-proxy.conf;` (over an empty
default so the base is valid before deploy). Only the app-proxy `map` + vhost +
tab urls are deploy-time.

Order in the deploy pipeline (after routes exist):
1. Create the per-app public routes (§1) → collect the `{internal → public}` map.
2. Render the app-proxy snippet (map + server block) from that map.
3. Write it into the showroom pod at `/showroom/nginx/conf.d/app-proxy.conf`
   (shared showroom volume) and `nginx -s reload` the proxy container.
4. Rewrite the console tab's `url` to the public host of `proxy_hosts[0]` in the
   rendered `ui-config.yml` and reload the content container.

## Components changed

- `src/backend/app/services/showroom_scaffold.py` — parse `proxy_hosts`; emit the
  app-proxy vhost template + `resolver`; render tab `url` as public host.
- `src/backend/app/services/deploy_service.py` — create one route per app proxy
  host (pointing at the showroom infra IP), build the internal→public map, inject
  the rendered vhost + urls into the showroom pod.
- `src/backend/app/services/providers/ocpvirt.py` (+ `kubevirt.py`) — allow a
  caller-supplied hostname/label for app-proxy routes so the public host is
  stable and un-collided.
- Frontend (`showroomTabs.ts`, `PropertiesPanel.tsx`) — `proxyHosts[]` editing +
  resolve preview.
- Template loader / schema — accept `proxy_hosts`.

## Testing

- Unit: `showroom_scaffold` emits a vhost with `proxy_hide_header X-Frame-Options`,
  `resolver`, host-mapped `proxy_pass`, and Location/cookie rewriting for each
  `proxy_hosts` entry; back-compat single `proxy_host` auto-appends oauth.
- Unit: deploy builds one route per app proxy and a correct internal→public map.
- Manual/e2e: on the live `pf-6fcf0e3e` deploy, the OCP Console tab loads the
  login page, completes OAuth in the iframe, and reaches the console dashboard —
  all under public hostnames.

## Out of scope / caveats

- SNAT fix (already committed separately).
- A few console sub-resources that hardcode `.local` links may 404 (cosmetic);
  `sub_filter` can patch specific cases later if needed.
- Non-console apps (ArgoCD, ACS, …) follow the same `proxy_hosts` pattern but are
  added per-lab; only console+oauth are in the first implementation.
- Single wildcard route + delegated wildcard cert (`*.<guid>.apps…`) is rejected
  for v1 (cluster does not issue such a cert by default).
