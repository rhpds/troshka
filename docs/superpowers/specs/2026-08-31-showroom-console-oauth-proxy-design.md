# Showroom embedded OCP console + OAuth proxy (nested `.local` clusters)

**Date:** 2026-08-31
**Status:** Implemented (branch `showroom-console-oauth-proxy`), validated end-to-end
in a browser on a live deploy.

> This doc was refreshed to match what shipped ("Approach B" below). The original
> draft proposed deploy-time nginx injection with a per-deploy host→backend map;
> that was superseded by a fully baked, deterministic-hostname design that needs
> no agent change and no post-creation exec/reload.

## Problem

The showroom "OCP Console" tab proxied the nested cluster's console at an
internal, non-resolvable domain (`console-openshift-console.apps.ocp.ocp.local`).
Students reach the showroom only through a **public** URL from an **external
browser that cannot resolve `.local`**, and the console must render **embedded in
the split-view iframe**.

Two independent defects, both fixed:

1. **504 (SNAT):** the showroom pod on the project transit subnet couldn't reach
   the lab bridge; the nested SNO dropped its foreign-subnet source. Fixed in
   troshkad `_allow_infra_veth_forward` with a per-bridge masquerade.
2. **Console/OAuth embedding (this spec):** the single `location /console/`
   proxied only the console host, couldn't carry the console→`oauth-openshift`
   redirect, didn't strip `X-Frame-Options`, and exposed `.local` hosts the
   browser can't resolve.

## Hard constraints (locked with the user)

- External browser; **cannot resolve `.local`**. All browser-visible URLs must be
  public and TLS-valid.
- Console + OAuth must work **embedded in the iframe**.
- Nested cluster stays **untouched** (no OAuthClient / ingress / basePath edits).
- Internal cluster domain stays `*.apps.ocp.ocp.local`.
- Per-app mapping is acceptable (one public route per embedded app).

## Approach B — deterministic public hostnames + baked regex vhost

Give each embedded app a **deterministic, reversible public hostname** and bake a
generic nginx vhost at scaffold time. Deploy only has to create the routes and
fill the tab URL — no per-deploy nginx map, no `resolver`, no `conf.d` include,
no pod exec/reload, no troshkad change.

### Public hostname scheme

`troshka-pf-<pid8>-<internal-first-label>.<apps-domain>`, e.g.
`troshka-pf-6fcf0e3e-console-openshift-console.apps.ocpvdev01.dal13.infra.demo.redhat.com`.
Single DNS label under the cluster apps domain → covered by the existing
`*.apps.<cluster>` wildcard cert. `<pid8>` (project id prefix) gives per-project
uniqueness; the upstream is reconstructed from `<internal-first-label>`, not the pid.

### nginx (baked at scaffold time — `build_app_proxy_config`)

One `server` block per internal host, emitted inside the main nginx.conf by
`build_nginx_config` (no `conf.d`, no map, no resolver):

```nginx
server {
  listen 80;
  # quoted: bare {8} is nginx block syntax; capture can't be named 'pid' ($pid is builtin)
  server_name "~^troshka-pf-(?<troshka_pid>[0-9a-f]{8})-console-openshift-console\.(?<troshka_suffix>apps\..+)$";
  location / {
    proxy_pass https://console-openshift-console.apps.ocp.ocp.local;   # literal upstream => no resolver
    proxy_ssl_server_name on;
    proxy_ssl_name  console-openshift-console.apps.ocp.ocp.local;
    proxy_ssl_verify off;
    proxy_set_header Host console-openshift-console.apps.ocp.ocp.local; # Host-route at OCP ingress
    proxy_set_header X-Forwarded-Proto https;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_read_timeout 86400;
    proxy_hide_header X-Frame-Options;                                  # embed in iframe
    # rewrite redirect Location host .local -> public (pid+suffix from the request),
    # leaving the redirect_uri query param .local so the OAuthClient still validates
    proxy_redirect ~^https://(?<troshka_h>[^.]+)\.apps\.ocp\.ocp\.local(?<troshka_rest>.*)$
                   https://troshka-pf-$troshka_pid-$troshka_h.$troshka_suffix$troshka_rest;
    # body rewrite: SERVER_FLAGS host refs .local -> public; match "//<host>" so the
    # URL-encoded redirect_uri (https%3A%2F%2F...) is left .local
    proxy_set_header Accept-Encoding "";        # sub_filter needs an uncompressed body
    sub_filter_once off;
    sub_filter_types *;
    sub_filter "//console-openshift-console.apps.ocp.ocp.local" "//troshka-pf-$troshka_pid-console-openshift-console.$troshka_suffix";
    sub_filter "//oauth-openshift.apps.ocp.ocp.local"          "//troshka-pf-$troshka_pid-oauth-openshift.$troshka_suffix";
    proxy_cookie_domain .apps.ocp.ocp.local $host;
  }
}
```

### Correctness invariants

- **Redirects, cookies, and `SERVER_FLAGS` host refs** are rewritten `.local`→public
  (via `proxy_redirect`, `proxy_cookie_domain`, `sub_filter`) so the browser only
  ever sees resolvable public URLs.
- **The OAuth `redirect_uri` stays `.local`** — the console builds it from its
  configured base address (not the request Host), so it always matches the
  untouched `console` OAuthClient. `proxy_redirect` only rewrites the Location host;
  `sub_filter` matches `//<host>` so the URL-encoded `redirect_uri` is untouched.
- **Same-site cookies work embedded** because all public hosts share the cluster's
  registrable domain, so the cross-subdomain iframe is same-site.

### Tab schema

Proxy tabs take `proxy_hosts: []` ([0] = iframe target, rest = companions such as
oauth). The rendered `ui-config` tab `url` is a `__TROSHKA_APP_PROXY__<internal>__`
placeholder, substituted at deploy time with the public host.

### Deploy automation (`deploy_service._create_routes_for_gateway`)

Runs before container creation, so hostnames are known before the pod is baked:
1. Create the showroom route (existing behavior); capture its route name + hostname.
2. `derive_apps_domain(hostname)`; for each `app_proxy_internal_hosts(tabs)`, compute
   `app_proxy_public_host(...)` and call `driver.create_app_proxy_route(...)` — clones
   the showroom route with an explicit `spec.host`. Works for **ocpvirt and kubevirt**.
3. `fill_app_proxy_tab_urls(...)` substitutes the placeholder in the showroom pod's
   baked `UI_CONFIG_B64` init env. No exec/reload.

## Components changed

- `src/troshkad/troshkad.py` — per-bridge masquerade (Defect 1, separate commit).
- `src/backend/app/services/showroom_scaffold.py` — `proxy_hosts` parsing,
  `build_app_proxy_config`, inline app-proxy blocks in `build_nginx_config`,
  `app_proxy_internal_hosts`/`app_proxy_public_host`/`derive_apps_domain`/
  `fill_app_proxy_tab_urls`, ui-config placeholder.
- `src/backend/app/services/providers/{ocpvirt,kubevirt}.py` — `create_app_proxy_route`.
- `src/backend/app/services/deploy_service.py` — `_create_routes_for_gateway`
  captures the showroom route, creates app-proxy routes, fills the tab URL.
- `src/backend/templates/ocp-{sno,compact,standard}.yaml` — console tab → `proxy_hosts`.
- `src/frontend/src/lib/showroomTabs.ts` + `components/canvas/PropertiesPanel.tsx` —
  `proxyHosts[]` editor with an "OCP console" preset.

## Testing

- Unit (`test_showroom_scaffold.py`): app-proxy vhost shape (regex server_name,
  literal upstream, `proxy_redirect`, `sub_filter`, XFO strip), `proxy_hosts`
  parsing, host derivation/public-host/url-fill helpers.
- End-to-end (Playwright, live deploy): console embeds (XFO stripped) → public oauth
  login → kubeadmin login → cluster Overview dashboard, cluster untouched.

## Out of scope / caveats

- Only `console` + `oauth` are wired by default. `SERVER_FLAGS`' `api.ocp.ocp.local`
  and monitoring hosts (`thanos-querier…`) stay `.local`; add them to `proxy_hosts`
  per-lab if metrics/monitoring need to work in-browser. ("Copy login command" works
  — it's oauth-served, which is proxied.)
- Wildcard delegated cert (`*.<guid>.apps…`) rejected — cluster doesn't issue it.
